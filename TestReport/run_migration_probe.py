"""Akivili Alembic migration regression probe (foundation-db S2).

Guards the S2 outcome so later data-layer work (S3 ORM / S4-S5 PG) and any future
schema migration cannot silently regress the baseline contract:
  1. Empty DB `alembic upgrade head` rebuilds exactly the S0.1 baseline schema
     (18 tables, byte-for-byte identical to baseline_schema.sql).
  2. Version stamping works: after upgrade, alembic_version == head; a second
     upgrade is idempotent (no re-create, no "table already exists").
  3. Legacy DB (has tables, no alembic_version) → run_migrations() auto-stamps
     (action='stamp'), does NOT recreate tables, does NOT lose data.
  4. Round-trip: upgrade → downgrade base (drops all 18) → upgrade rebuilds
     byte-identical (down/up both correct).
  5. WAL is applied by the migration path (journal_mode=wal after upgrade).

Uses isolated temp DBs via ALEMBIC_DB_PATH (never the real jianagency.db).
No CLI/LLM. Cleans up temp dir unless --keep.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import BACKEND  # noqa: E402

# 让 backend/ 可 import（db_migrate/config/alembic env）——run_qa_suite 只在
# setup_isolated_config 内插入，本 probe 不走那条路径，故显式加入。
sys.path.insert(0, str(BACKEND))

BASELINE_SQL = (
    BACKEND.parent
    / "openspec" / "changes" / "2026-07-24-foundation-db-alignment" / "baseline_schema.sql"
)


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


def _export_schema(db_path: str) -> str:
    """Dump table DDL the same way S0.1 did (exclude alembic_version + sqlite internals)."""
    db = sqlite3.connect(db_path)
    rows = db.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"
    ).fetchall()
    db.close()
    return "\n".join(f"-- [{t}] {n}\n{s.strip()};\n" for t, n, s in rows)


def _table_count(db_path: str) -> int:
    db = sqlite3.connect(db_path)
    n = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name != 'alembic_version' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    db.close()
    return n


def _version(db_path: str) -> str | None:
    db = sqlite3.connect(db_path)
    try:
        r = db.execute("SELECT version_num FROM alembic_version").fetchone()
        return r[0] if r else None
    except sqlite3.OperationalError:
        return None
    finally:
        db.close()


def run_probe(tmp: Path) -> Probe:
    probe = Probe()
    # 迁移接入模块与 alembic 均从 backend/ 解析；ALEMBIC_DB_PATH 指向隔离临时库。
    import db_migrate  # noqa: PLC0415

    baseline = BASELINE_SQL.read_text(encoding="utf-8")

    # --- Check 1+2+5: 空库 upgrade → 结构对齐基线 + 版本 stamp + 幂等 + WAL ---
    empty_db = str(tmp / "empty.db")
    os.environ["ALEMBIC_DB_PATH"] = empty_db
    action1 = db_migrate.run_migrations()
    probe.check("empty DB run_migrations action=upgrade", action1 == "upgrade", f"got {action1}")
    probe.check("empty DB rebuilt == baseline (byte-identical)",
                _export_schema(empty_db) == baseline,
                "18 tables byte-for-byte" if _export_schema(empty_db) == baseline else "SCHEMA DIFF")
    probe.check("empty DB table count == 18", _table_count(empty_db) == 18, f"{_table_count(empty_db)} tables")
    probe.check("alembic_version stamped to head(002)", _version(empty_db) == "002", f"v={_version(empty_db)}")
    jm = sqlite3.connect(empty_db).execute("PRAGMA journal_mode").fetchone()[0]
    probe.check("migration path leaves DB in WAL", str(jm).lower() == "wal", f"journal_mode={jm}")
    action2 = db_migrate.run_migrations()
    probe.check("second run is idempotent (action=noop)", action2 == "noop", f"got {action2}")
    probe.check("still 18 tables after 2nd run", _table_count(empty_db) == 18, f"{_table_count(empty_db)} tables")

    # --- Check 3: 存量库(有表无 version) → 自动 stamp、不重建、不丢数据 ---
    legacy_db = str(tmp / "legacy.db")
    ldb = sqlite3.connect(legacy_db)
    ldb.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, title TEXT)")
    ldb.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER)")
    ldb.execute("INSERT INTO projects (title) VALUES ('legacy-keep')")
    ldb.commit()
    ldb.close()
    os.environ["ALEMBIC_DB_PATH"] = legacy_db
    action3 = db_migrate.run_migrations()
    probe.check("legacy DB run_migrations action=stamp", action3 == "stamp", f"got {action3}")
    probe.check("legacy DB NOT recreated (still 2 tables)", _table_count(legacy_db) == 2,
                f"{_table_count(legacy_db)} tables")
    probe.check("legacy DB stamped to head(002)", _version(legacy_db) == "002", f"v={_version(legacy_db)}")
    kept = sqlite3.connect(legacy_db).execute("SELECT title FROM projects").fetchone()
    probe.check("legacy DB data preserved", bool(kept) and kept[0] == "legacy-keep",
                f"projects.title={kept[0] if kept else None}")

    # --- Check 6 (S3.6): 002 数据规整——planning→backlog / archived→done ---
    # 走 upgrade 链精确验证 002.upgrade 的数据逻辑：upgrade 到 001(仅建表) → 插废弃状态行
    # → upgrade 到 002(跑数据规整) → 校验。（stamp 只打版本、不执行迁移体，故不能靠 stamp 验。）
    from alembic import command as _command  # noqa: PLC0415
    norm_db = str(tmp / "normalize.db")
    os.environ["ALEMBIC_DB_PATH"] = norm_db
    cfg_n = db_migrate._alembic_config()
    _command.upgrade(cfg_n, "001")   # 只到 001（建表，未跑 002）
    ndb = sqlite3.connect(norm_db)
    ndb.execute("INSERT INTO tasks (project_id, title, status) VALUES (1,'旧规划态','planning')")
    ndb.execute("INSERT INTO tasks (project_id, title, status) VALUES (1,'旧归档态','archived')")
    ndb.execute("INSERT INTO tasks (project_id, title, status) VALUES (1,'正常态','in_progress')")
    ndb.commit()
    ndb.close()
    _command.upgrade(cfg_n, "002")   # 跑 002 数据规整
    ndb = sqlite3.connect(norm_db)
    st = {t: s for t, s in ndb.execute("SELECT title, status FROM tasks").fetchall()}
    ndb.close()
    probe.check("002: planning→backlog", st.get("旧规划态") == "backlog", f"got {st.get('旧规划态')}")
    probe.check("002: archived→done", st.get("旧归档态") == "done", f"got {st.get('旧归档态')}")
    probe.check("002: 其它状态不动", st.get("正常态") == "in_progress", f"got {st.get('正常态')}")

    # --- Check 4: 往返 upgrade → downgrade base → upgrade 无损 ---
    from alembic import command  # noqa: PLC0415
    rt_db = str(tmp / "roundtrip.db")
    os.environ["ALEMBIC_DB_PATH"] = rt_db
    db_migrate.run_migrations()  # upgrade → 18
    cfg = db_migrate._alembic_config()
    command.downgrade(cfg, "base")
    probe.check("downgrade base drops all tables", _table_count(rt_db) == 0, f"{_table_count(rt_db)} tables")
    probe.check("downgrade clears version", _version(rt_db) is None, f"v={_version(rt_db)}")
    command.upgrade(cfg, "head")
    probe.check("re-upgrade rebuilds 18 tables", _table_count(rt_db) == 18, f"{_table_count(rt_db)} tables")
    probe.check("round-trip schema == baseline", _export_schema(rt_db) == baseline,
                "byte-identical" if _export_schema(rt_db) == baseline else "SCHEMA DIFF")

    os.environ.pop("ALEMBIC_DB_PATH", None)
    return probe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep temp dir for inspection")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili-migration-"))
    try:
        probe = run_probe(tmp)
    finally:
        if not args.keep:
            import shutil  # noqa: PLC0415
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")

    passed = sum(1 for _, ok, _ in probe.results if ok)
    total = len(probe.results)
    print(f"\nMigration probe: {passed}/{total} passed")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
