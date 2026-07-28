"""数据底座 S4.5 · SQLite → PostgreSQL 全量数据迁移。

只读 SQLite 源库（file:...?mode=ro），把每张业务表的数据逐行搬到 PG 目标库，然后重置
PG 自增序列到 max(id)+1。**不改 SQLite 源**。schema 由 Alembic 在 PG 上先建好（本脚本
只搬数据，不建表）。

用法：
    # 目标 PG 由 AKIVILI_DB_URL 指定；源 SQLite 由 --sqlite 指定（默认 config.db_path）
    AKIVILI_DB_URL=postgresql+asyncpg://user:pw@host:5432/db \\
    python migrate_sqlite_to_pg.py --sqlite backend/jianagency.db [--truncate]

关键约束（见 openspec S4.5 决策）：
- **保留原始 id**：id 不连续（有历史删除），迁移必须原样保留，迁移后重置 sequence。
- **剔除 NUL 字节**：PG text 不接受 \\u0000（SQLite 能存）；迁移时从所有文本字段剔除
  （日志里的 NUL 是子进程输出乱码脏数据，无业务损失）。一致性校验用同口径。
- **依赖顺序**：按 001 的 _DROP_ORDER（被引用表在前）插入，满足外键约束。
- **幂等**：--truncate 先清空目标表（TRUNCATE ... CASCADE + RESTART IDENTITY），可重跑。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from sqlalchemy import create_engine, text


def _strip_nul(v):
    """PG text 不接受 NUL 字节；从字符串字段剔除（非字符串原样返回）。"""
    if isinstance(v, str) and "\x00" in v:
        return v.replace("\x00", "")
    return v


def _table_order() -> list[str]:
    """按 001 基线的依赖顺序（被引用表在前）返回业务表名。"""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    mig = os.path.join(here, "migrations", "versions", "001_baseline.py")
    spec = importlib.util.spec_from_file_location("baseline_001_forder", mig)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["baseline_001_forder"] = mod
    spec.loader.exec_module(mod)
    return list(mod._DROP_ORDER)


def _sqlite_columns(sconn, table: str) -> list[str]:
    """按物理列序返回列名（PRAGMA table_info 的 cid 顺序）。"""
    return [r[1] for r in sconn.execute(f"PRAGMA table_info({table})").fetchall()]


def _dangling_rowids(sconn) -> dict[str, set]:
    """用 PRAGMA foreign_key_check 找出所有悬空外键行的 rowid，按表分组。

    SQLite 外键默认关闭，历史上积累了指向已删父行的孤儿子行（如 run_queue/activities
    指向已删 task）。PG 强制外键会拒绝，故迁移时跳过这些孤儿行（见 openspec S4.5 决策：
    跳过悬空子行）。返回 {表名: {rowid,...}}。"""
    sconn.execute("PRAGMA foreign_keys=ON")
    result: dict[str, set] = {}
    for row in sconn.execute("PRAGMA foreign_key_check").fetchall():
        # (table, rowid, referenced_table, fkid)
        tbl, rowid = row[0], row[1]
        if rowid is not None:
            result.setdefault(tbl, set()).add(rowid)
    return result


def _pg_seq_tables(pconn) -> dict[str, str]:
    """返回 {表名: 自增列名}，用于迁移后重置 sequence（仅 id 自增列）。"""
    rows = pconn.execute(text("""
        SELECT c.relname AS tbl, a.attname AS col
        FROM pg_class c
        JOIN pg_attribute a ON a.attrelid = c.oid
        JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE c.relkind = 'r'
          AND pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval%'
    """)).fetchall()
    return {r[0]: r[1] for r in rows}


def migrate(sqlite_path: str, pg_url: str, truncate: bool) -> dict:
    sconn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sconn.row_factory = sqlite3.Row
    peng = create_engine(pg_url)
    stats: dict[str, int] = {}
    skipped: dict[str, int] = {}
    tables = _table_order()
    dangling = _dangling_rowids(sconn)

    try:
        with peng.begin() as pconn:
            if truncate:
                # 反依赖序 TRUNCATE，CASCADE 兜底外键；RESTART IDENTITY 清序列。
                for t in reversed(tables):
                    pconn.execute(text(f"TRUNCATE TABLE {t} RESTART IDENTITY CASCADE"))

            for t in tables:
                cols = _sqlite_columns(sconn, t)
                collist = ", ".join(cols)
                params = ", ".join(f":{c}" for c in cols)
                ins = text(f"INSERT INTO {t} ({collist}) VALUES ({params})")
                skip_ids = dangling.get(t, set())
                # 带 rowid 取出（别名 _rid_ 避免与 INTEGER PRIMARY KEY 的 id 列名冲突——
                # 那种表 rowid 即 id，裸 SELECT rowid,id 会重名），用于跳过悬空外键孤儿行。
                rows = sconn.execute(f"SELECT rowid AS _rid_, {collist} FROM {t}").fetchall()
                n = 0
                sk = 0
                for r in rows:
                    if r["_rid_"] in skip_ids:
                        sk += 1
                        continue
                    payload = {c: _strip_nul(r[c]) for c in cols}
                    pconn.execute(ins, payload)
                    n += 1
                stats[t] = n
                if sk:
                    skipped[t] = sk

            # 重置自增序列到 max(id)+1，避免后续 INSERT 主键冲突。
            seqcols = _pg_seq_tables(pconn)
            for t, col in seqcols.items():
                pconn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{t}', '{col}'), "
                    f"COALESCE((SELECT max({col}) FROM {t}), 1), "
                    f"(SELECT max({col}) IS NOT NULL FROM {t}))"))
    finally:
        sconn.close()
        peng.dispose()
    return {"stats": stats, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 全量数据迁移")
    ap.add_argument("--sqlite", help="源 SQLite 路径（默认取 config.db_path）")
    ap.add_argument("--truncate", action="store_true", help="迁移前清空目标表（幂等重跑）")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from config import load_settings, migration_db_url

    sqlite_path = args.sqlite or load_settings().db_path
    pg_url = migration_db_url()   # 同步 psycopg URL（AKIVILI_DB_URL 指向 PG 时）
    if not pg_url.startswith("postgresql"):
        raise SystemExit(f"目标必须是 PostgreSQL（AKIVILI_DB_URL 未指向 PG）：当前 {pg_url}")

    print(f"源 SQLite : {sqlite_path}")
    print(f"目标 PG   : {pg_url}")
    print(f"truncate  : {args.truncate}")
    result = migrate(sqlite_path, pg_url, args.truncate)
    stats, skipped = result["stats"], result["skipped"]
    total = sum(stats.values())
    print("\n迁移完成，各表行数：")
    for t, n in stats.items():
        mark = f"  (跳过悬空 {skipped[t]} 行)" if t in skipped else ""
        print(f"  {t:22} {n:>7}{mark}")
    print(f"总计迁移: {total} 行")
    if skipped:
        print(f"跳过悬空外键孤儿: {sum(skipped.values())} 行（{skipped}）")


if __name__ == "__main__":
    main()
