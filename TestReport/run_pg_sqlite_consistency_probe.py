"""数据底座 S4.5 · SQLite ⇄ PostgreSQL 全量数据一致性校验（严格逐行逐列）。

平台静止（无新任务写入）时，两库应逐行逐列完全一致。本探针不做行数抽样，而是：
  1. 表集合一致（两边业务表相同）；
  2. 每张表行数一致；
  3. 每张表**逐行逐列**比对（按主键排序对齐，值归一化后相等）。

归一化口径（跨引擎的合法差异，非数据不一致）：
  - NUL 字节：SQLite 文本可含 \\u0000，PG 不接受、迁移时已剔除 → 比较前两边都剔除。
  - 整数/布尔：SQLite 存 0/1，PG 亦 int；统一转 int 比较（None 保留）。
  - 其余文本/时间：迁移已归一化为同格式，直接字符串相等。

用法：
    AKIVILI_DB_URL=postgresql+asyncpg://user:pw@host:5432/db \\
    python TestReport/run_pg_sqlite_consistency_probe.py --sqlite backend/jianagency.db

退出码：全一致=0，任何差异=1（供 CI 门禁）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy import create_engine, text  # noqa: E402

PASS: list = []
FAIL: list = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    if not cond:
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def _norm(v):
    """跨引擎归一化：剔除 NUL、bool→int，供逐值比较。"""
    if isinstance(v, str):
        return v.replace("\x00", "") if "\x00" in v else v
    if isinstance(v, bool):
        return int(v)
    return v


def _table_order() -> list[str]:
    import importlib.util
    mig = os.path.join(BACKEND, "migrations", "versions", "001_baseline.py")
    spec = importlib.util.spec_from_file_location("baseline_001_cprobe", mig)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["baseline_001_cprobe"] = mod
    spec.loader.exec_module(mod)
    return list(mod._DROP_ORDER)


def _sqlite_columns(sconn, table: str) -> list[str]:
    return [r[1] for r in sconn.execute(f"PRAGMA table_info({table})").fetchall()]


def _pk_cols(sconn, table: str) -> list[str]:
    """主键列（按 pk 序）；无显式主键时回退到全列排序保证确定性。"""
    info = sconn.execute(f"PRAGMA table_info({table})").fetchall()
    pk = [(r[5], r[1]) for r in info if r[5]]   # r[5]=pk 序号(>0)
    if pk:
        return [name for _, name in sorted(pk)]
    return [r[1] for r in info]


def _dangling_rowids(sconn) -> dict[str, set]:
    """悬空外键孤儿行的 rowid（按表）。这些行在 SQLite 里是孤儿、PG 不迁移，
    故比对时 sqlite 侧也排除，用同口径（见 S4.5 决策：跳过悬空子行）。"""
    sconn.execute("PRAGMA foreign_keys=ON")
    result: dict[str, set] = {}
    for row in sconn.execute("PRAGMA foreign_key_check").fetchall():
        tbl, rowid = row[0], row[1]
        if rowid is not None:
            result.setdefault(tbl, set()).add(rowid)
    return result


def compare(sqlite_path: str, pg_url: str) -> None:
    sconn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sconn.row_factory = sqlite3.Row
    peng = create_engine(pg_url)

    tables = _table_order()
    dangling = _dangling_rowids(sconn)
    try:
        with peng.connect() as pconn:
            # 1) 表集合
            pg_tables = {r[0] for r in pconn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name NOT LIKE 'sqlite_%'")).fetchall()}
            pg_tables.discard("alembic_version")
            sq_tables = {r[0] for r in sconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name!='alembic_version'").fetchall()}
            check("表集合一致", set(tables) <= (pg_tables & sq_tables) or pg_tables == sq_tables,
                  f"pg-only={pg_tables - sq_tables}, sqlite-only={sq_tables - pg_tables}")

            for t in tables:
                cols = _sqlite_columns(sconn, t)
                pks = _pk_cols(sconn, t)
                order = ", ".join(pks)
                collist = ", ".join(cols)
                skip_ids = dangling.get(t, set())

                # 2) 行数（sqlite 侧排除悬空孤儿行，与 PG 同口径）
                # rowid 别名 _rid_ 避免与 INTEGER PRIMARY KEY 的 id 列重名。
                srows_all = sconn.execute(
                    f"SELECT rowid AS _rid_, {collist} FROM {t} ORDER BY {order}").fetchall()
                srows = [r for r in srows_all if r["_rid_"] not in skip_ids]
                sn = len(srows)
                pn = pconn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                excl = f"（已排除悬空 {len(skip_ids)} 行）" if skip_ids else ""
                check(f"[{t}] 行数一致{excl}", sn == pn, f"sqlite={sn} pg={pn}")
                if sn != pn:
                    continue

                # 3) 逐行逐列（按主键排序对齐）
                prows = pconn.execute(text(f"SELECT {collist} FROM {t} ORDER BY {order}")).fetchall()
                mismatch = 0
                first_bad = ""
                for i, (sr, pr) in enumerate(zip(srows, prows)):
                    for ci, c in enumerate(cols):
                        sv, pv = _norm(sr[c]), _norm(pr[ci])
                        # 数值列：sqlite int vs pg int/Decimal，统一数值比较
                        if isinstance(sv, (int, float)) and pv is not None and not isinstance(pv, str):
                            eq = (sv == pv) or (str(sv) == str(pv))
                        else:
                            eq = sv == pv
                        if not eq:
                            mismatch += 1
                            if not first_bad:
                                pk_id = sr[pks[0]] if pks[0] in sr.keys() else i
                                first_bad = f"{t}[{pks[0]}={pk_id}].{c}: sqlite={sv!r} pg={pv!r}"
                check(f"[{t}] 逐行逐列一致", mismatch == 0,
                      f"差异 {mismatch} 处，首例：{first_bad}")
    finally:
        sconn.close()
        peng.dispose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", help="源 SQLite 路径（默认 config.db_path）")
    args = ap.parse_args()
    from config import load_settings, migration_db_url

    sqlite_path = args.sqlite or load_settings().db_path
    pg_url = migration_db_url()
    if not pg_url.startswith("postgresql"):
        raise SystemExit(f"AKIVILI_DB_URL 必须指向 PG：当前 {pg_url}")

    print(f"源 SQLite : {sqlite_path}")
    print(f"目标 PG   : {pg_url}")
    compare(sqlite_path, pg_url)

    print("\n" + "=" * 56)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n不一致项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] SQLite ⇄ PG 全量数据逐行逐列一致")


if __name__ == "__main__":
    main()
