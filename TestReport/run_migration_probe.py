"""Akivili Alembic migration regression probe (foundation-db S2 / S5 · PG 单引擎).

Guards the migration contract so later data-layer work and any future schema
migration cannot silently regress it. **S5：PostgreSQL 单引擎**——不再有 SQLite，
故不再做「逐字节对齐 baseline_schema.sql」「PRAGMA journal_mode=wal」这类 sqlite 专属校验；
保留迁移的真实价值，改在 PG 上验证：

  1. 空的隔离 PG 库 `run_migrations()` 返回 action=='upgrade'；之后恰好 19 张基线表
     （不含 alembic_version），且 alembic_version.version_num=='007'（head）。
  2. 幂等：第二次 `run_migrations()` 返回 action=='noop'；仍 19 张表。
  3. 存量库（已有表、无 alembic_version）→ `run_migrations()` 返回 action=='stamp'，
     不重建/不删表、保留既有数据，并 stamp 到 '007'。
  4. 002 数据规整：只 upgrade 到 '001'（建表）→ 插 planning/archived/in_progress 三态任务
     → upgrade 到 '002'（跑规整）→ 断言 planning→backlog、archived→done、in_progress 不动。
     （003 run_queue 部分唯一索引 / 004 task_runs.kill_requested_at / 005 task_runs.session 列
     / 006 run_queue.session 列 / 007 agent_sessions 表 均与数据规整无关，本子检仍只测到 002。）
  5. 往返：upgrade head → downgrade base（0 张基线表、alembic_version 清空）→ upgrade head
     重建 19 张表。（比对**表数量**，不比对逐字节 schema——PG 无 baseline dump。）

每个场景各建一个隔离 PG 库（run_qa_suite.isolated_pg_db_url()，进程退出自动删），写一份临时
config.json 指向它、改 config.CONFIG_FILE，再跑 run_migrations()/alembic 命令。
config.load_settings() 每次现读 CONFIG_FILE（无 lru_cache/全局缓存），故切库只需改 CONFIG_FILE。
No CLI/LLM. Cleans up temp dir unless --keep.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 导入 run_qa_suite 即设置 AKIVILI_TEST_NULLPOOL=1，并暴露 BACKEND / isolated_pg_db_url。
from run_qa_suite import BACKEND, isolated_pg_db_url  # noqa: E402

# 让 backend/ 可 import（db_migrate/config/alembic env）。
sys.path.insert(0, str(BACKEND))

# 期望的基线业务表数量（models/tables.py 的 19 张，不含 alembic_version）。
EXPECTED_TABLES = 19   # 005/006 加列不加表；007 加 agent_sessions 表，18→19
HEAD_REV = "007"

# 统计基线表数：public schema 下的 BASE TABLE，排除 alembic_version。
_COUNT_SQL = (
    "SELECT count(*) FROM information_schema.tables "
    "WHERE table_schema='public' AND table_type='BASE TABLE' "
    "AND table_name <> 'alembic_version'"
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


def _pg_dsn(run_url: str) -> str:
    """从运行期 asyncpg URL 得到 psycopg（同步）可用的 DSN。

    postgresql+asyncpg://user:pw@host:port/db → postgresql://user:pw@host:port/db
    不硬编码任何凭证——纯粹从隔离库 URL 去掉 +asyncpg driver 段。
    """
    return run_url.replace("+asyncpg", "", 1)


def _table_count(run_url: str) -> int:
    import psycopg  # noqa: PLC0415
    with psycopg.connect(_pg_dsn(run_url)) as conn:
        return int(conn.execute(_COUNT_SQL).fetchone()[0])


def _version(run_url: str) -> str | None:
    """读 alembic_version.version_num；表不存在 → None。"""
    import psycopg  # noqa: PLC0415
    with psycopg.connect(_pg_dsn(run_url)) as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.alembic_version')"
        ).fetchone()[0]
        if not exists:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None


def _use_db(tmp: Path, run_url: str, tag: str) -> None:
    """把 config.CONFIG_FILE 指向一份 db_url=<隔离库> 的临时 config.json。

    load_settings() 每次现读 CONFIG_FILE（无缓存），故此后所有 run_migrations()/alembic
    命令都通过 migration_db_url() 落到该隔离库。
    """
    import config  # noqa: PLC0415
    cfg = {"db_url": run_url, "providers": [], "default_provider_id": ""}
    cfg_path = tmp / f"config_{tag}.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    config.CONFIG_FILE = cfg_path


def run_probe(tmp: Path) -> Probe:
    probe = Probe()
    import db_migrate  # noqa: PLC0415
    from alembic import command  # noqa: PLC0415

    # --- Check 1+2: 空的隔离库 upgrade → 18 表 + 版本 stamp + 幂等 ---
    empty_url = isolated_pg_db_url()
    _use_db(tmp, empty_url, "empty")
    action1 = db_migrate.run_migrations()
    probe.check("empty DB run_migrations action=upgrade", action1 == "upgrade", f"got {action1}")
    probe.check(f"empty DB table count == {EXPECTED_TABLES}",
                _table_count(empty_url) == EXPECTED_TABLES, f"{_table_count(empty_url)} tables")
    probe.check(f"alembic_version stamped to head({HEAD_REV})",
                _version(empty_url) == HEAD_REV, f"v={_version(empty_url)}")
    action2 = db_migrate.run_migrations()
    probe.check("second run is idempotent (action=noop)", action2 == "noop", f"got {action2}")
    probe.check(f"still {EXPECTED_TABLES} tables after 2nd run",
                _table_count(empty_url) == EXPECTED_TABLES, f"{_table_count(empty_url)} tables")

    # --- Check 3: 存量库(有表无 version) → 自动 stamp、不重建、不丢数据 ---
    legacy_url = isolated_pg_db_url()
    import psycopg  # noqa: PLC0415
    with psycopg.connect(_pg_dsn(legacy_url), autocommit=True) as conn:
        conn.execute("CREATE TABLE projects (id SERIAL PRIMARY KEY, title TEXT)")
        conn.execute("CREATE TABLE tasks (id SERIAL PRIMARY KEY, project_id INTEGER)")
        conn.execute("INSERT INTO projects (title) VALUES ('legacy-keep')")
    _use_db(tmp, legacy_url, "legacy")
    action3 = db_migrate.run_migrations()
    probe.check("legacy DB run_migrations action=stamp", action3 == "stamp", f"got {action3}")
    probe.check("legacy DB NOT recreated (still 2 tables)", _table_count(legacy_url) == 2,
                f"{_table_count(legacy_url)} tables")
    probe.check(f"legacy DB stamped to head({HEAD_REV})",
                _version(legacy_url) == HEAD_REV, f"v={_version(legacy_url)}")
    with psycopg.connect(_pg_dsn(legacy_url)) as conn:
        kept = conn.execute("SELECT title FROM projects").fetchone()
    probe.check("legacy DB data preserved", bool(kept) and kept[0] == "legacy-keep",
                f"projects.title={kept[0] if kept else None}")

    # --- Check 4: 002 数据规整——planning→backlog / archived→done / 其它不动 ---
    # 走 upgrade 链精确验证 002.upgrade 的数据逻辑：upgrade 到 001(仅建表) → 插废弃状态行
    # → upgrade 到 002(跑数据规整) → 校验。（stamp 只打版本、不执行迁移体，故不能靠 stamp 验。）
    norm_url = isolated_pg_db_url()
    _use_db(tmp, norm_url, "normalize")
    cfg_n = db_migrate._alembic_config()
    command.upgrade(cfg_n, "001")   # 只到 001（建表，未跑 002）
    # PG 强制外键：tasks.project_id → projects.id NOT NULL，故先建一个 project 再插 task。
    with psycopg.connect(_pg_dsn(norm_url), autocommit=True) as conn:
        conn.execute("INSERT INTO projects (id, title, local_path) VALUES (1, 'p', '/tmp')")
        conn.execute("INSERT INTO tasks (project_id, title, status) VALUES (1,'旧规划态','planning')")
        conn.execute("INSERT INTO tasks (project_id, title, status) VALUES (1,'旧归档态','archived')")
        conn.execute("INSERT INTO tasks (project_id, title, status) VALUES (1,'正常态','in_progress')")
    command.upgrade(cfg_n, "002")   # 跑 002 数据规整
    with psycopg.connect(_pg_dsn(norm_url)) as conn:
        st = {t: s for t, s in conn.execute("SELECT title, status FROM tasks").fetchall()}
    probe.check("002: planning→backlog", st.get("旧规划态") == "backlog", f"got {st.get('旧规划态')}")
    probe.check("002: archived→done", st.get("旧归档态") == "done", f"got {st.get('旧归档态')}")
    probe.check("002: 其它状态不动", st.get("正常态") == "in_progress", f"got {st.get('正常态')}")

    # --- Check 5: 往返 upgrade → downgrade base → upgrade（比表数量，非逐字节） ---
    rt_url = isolated_pg_db_url()
    _use_db(tmp, rt_url, "roundtrip")
    db_migrate.run_migrations()  # upgrade → 18
    cfg = db_migrate._alembic_config()
    try:
        command.downgrade(cfg, "base")
        down_err = ""
    except Exception as exc:  # noqa: BLE001
        # 不吞成功、不伪造：downgrade 抛错时如实记为 FAIL，附首行错误，避免整探针崩在此处不打计数。
        down_err = str(exc).splitlines()[0]
    probe.check("downgrade base drops all base tables",
                not down_err and _table_count(rt_url) == 0,
                down_err or f"{_table_count(rt_url)} tables")
    probe.check("downgrade clears version", not down_err and _version(rt_url) is None,
                down_err or f"v={_version(rt_url)}")
    if not down_err:
        command.upgrade(cfg, "head")
    probe.check(f"re-upgrade rebuilds {EXPECTED_TABLES} tables",
                not down_err and _table_count(rt_url) == EXPECTED_TABLES,
                down_err or f"{_table_count(rt_url)} tables")

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
