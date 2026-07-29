# -*- coding: utf-8 -*-
"""数据底座 S5 · ORM 模型 vs 迁移后 PostgreSQL 实际 schema 等价性探针（隔离 PG 库）。

**为何改口径（S5）**：PG 上 001_baseline.upgrade() 直接委托 `Base.metadata.create_all`
（见 migrations/versions/001_baseline.py 的 `if bind.dialect.name == "postgresql"` 分支），
所以「ORM create_all」与「001 基线建 PG 表」是**同一段代码**——再互比即同义反复。

**本探针新职责**：验证整条 Alembic 迁移链（run_migrations() = 001 create_all + 002）
在真实 PostgreSQL 上建出的**活体 schema**，与 ORM 模型（models.Base.metadata）的声明
一致。**ORM 模型 = 基准；PG catalog = 实际**。以此守护漂移：有人手改迁移、手改模型，
或 002 意外改了结构，都会被本探针逐表逐项抓出。

逐表校验（models.Base.metadata 里全部 18 张 ORM 表）：
  1. 表集合：ORM 声明的表都在 PG public schema，且无意外多余基表（排除 alembic_version）。
  2. 列名集合：PG 每表列名集合 == ORM 模型列名集合。
  3. 主键：PG 主键列集合 == ORM 主键列集合。
  4. 外键：归一化为 {(本列, 引用表, 引用列, on_delete)} 集合比对。
  5. 唯一约束：PG 与 ORM 的唯一约束/索引列集合（frozenset(cols) 的集合）一致。

ORM 基准用 SQLAlchemy 内省 metadata 直接取声明真相（不建第二个库）；PG 实际在
run_migrations() 建好隔离库后用 sqlalchemy.inspect() 方言感知内省。全在临时 PG 隔离库，
绝不碰真实 akivili 库。

用法：py -3.12 TestReport/run_orm_schema_parity_probe.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _orm_reference():
    """从 models.Base.metadata 内省 ORM 声明真相（不建库）。

    返回 {table_name: {"cols", "pk", "fks", "uniques"}}：
      cols     = 列名集合
      pk       = 主键列名集合
      fks      = {(本列, 引用表, 引用列, on_delete)} 集合
      uniques  = {frozenset(唯一约束/唯一索引 列名)} 集合（列级 unique=True + 表级
                 UniqueConstraint + unique Index 三源合并，frozenset 天然去重）
    """
    from sqlalchemy import UniqueConstraint
    import models

    ref = {}
    for name, table in models.Base.metadata.tables.items():
        cols = {c.name for c in table.columns}
        pk = {c.name for c in table.primary_key.columns}

        fks = set()
        for fk in table.foreign_keys:
            od = (fk.ondelete or "").upper()
            fks.add((fk.parent.name, fk.column.table.name, fk.column.name, od))

        uniques = set()
        for c in table.columns:                       # 列级 unique=True
            if c.unique:
                uniques.add(frozenset([c.name]))
        for con in table.constraints:                 # 表级 UniqueConstraint
            if isinstance(con, UniqueConstraint):
                names = [c.name for c in con.columns]
                if names:
                    uniques.add(frozenset(names))
        for idx in table.indexes:                     # unique Index
            if idx.unique:
                uniques.add(frozenset(c.name for c in idx.columns))

        ref[name] = {"cols": cols, "pk": pk, "fks": fks, "uniques": uniques}
    return ref


def _pg_actual(sync_url):
    """在迁移后的隔离 PG 库上用 sqlalchemy.inspect() 内省实际 schema（方言感知）。

    返回结构同 _orm_reference()，外加顶层 "_base_tables"（public schema 全部基表名，
    排除 alembic_version）供表集合比对。inspector 走同步 psycopg URL（迁移期同一 driver）。
    """
    from sqlalchemy import create_engine, inspect

    eng = create_engine(sync_url)
    try:
        insp = inspect(eng)
        base_tables = {
            t for t in insp.get_table_names(schema="public") if t != "alembic_version"
        }
        actual = {"_base_tables": base_tables}
        for t in base_tables:
            cols = {c["name"] for c in insp.get_columns(t, schema="public")}
            pk = set(insp.get_pk_constraint(t, schema="public").get("constrained_columns") or [])

            fks = set()
            for fk in insp.get_foreign_keys(t, schema="public"):
                ref_table = fk["referred_table"]
                loc = fk["constrained_columns"]
                rem = fk["referred_columns"]
                od = ((fk.get("options") or {}).get("ondelete") or "").upper()
                for lc, rc in zip(loc, rem):
                    fks.add((lc, ref_table, rc, od))

            uniques = set()
            for uc in insp.get_unique_constraints(t, schema="public"):
                names = uc.get("column_names") or []
                if names:
                    uniques.add(frozenset(names))
            for idx in insp.get_indexes(t, schema="public"):
                if idx.get("unique"):
                    names = [c for c in (idx.get("column_names") or []) if c]
                    if names:
                        uniques.add(frozenset(names))

            actual[t] = {"cols": cols, "pk": pk, "fks": fks, "uniques": uniques}
        return actual
    finally:
        eng.dispose()


def _isolate(tmp):
    """建隔离 PG 库 + 写临时 config.json，指向 config.CONFIG_FILE。返回同步迁移 URL。"""
    from run_qa_suite import isolated_pg_db_url  # noqa: PLC0415 —— import 即设 NULLPOOL

    db_url = isolated_pg_db_url()
    cfg = {"db_url": db_url, "providers": [], "default_provider_id": ""}
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)   # load_settings() 每次现读该文件，无缓存需清
    return config.migration_db_url()      # 运行期 asyncpg URL → 同步 psycopg URL（供 inspect）


def main():
    tmp = tempfile.mkdtemp(prefix="orm_parity_pg_")
    sync_url = _isolate(tmp)

    # 整条迁移链（001 create_all + 002）在隔离 PG 库落地
    from db_migrate import run_migrations
    action = run_migrations()
    check("run_migrations 建库(空库→upgrade)", action == "upgrade", f"action={action}")

    ref = _orm_reference()          # ORM 声明真相（基准）
    actual = _pg_actual(sync_url)   # 迁移后 PG 活体 schema（实际）

    # 1) 表集合：ORM 声明表都在 PG，且无意外多余基表
    orm_tables = set(ref)
    pg_tables = actual["_base_tables"]
    check("表数量=18(ORM 声明全量)", len(orm_tables) == 18, f"orm={len(orm_tables)}")
    check("表集合一致(ORM==PG，排除 alembic_version)", orm_tables == pg_tables,
          f"orm-only={sorted(orm_tables - pg_tables)}, pg-only={sorted(pg_tables - orm_tables)}")

    # 2~5) 逐表比对（只比两边都有的表，缺失/多余已在表集合检查中暴露）
    for t in sorted(orm_tables & pg_tables):
        r, a = ref[t], actual[t]
        check(f"[{t}] 列集合一致", r["cols"] == a["cols"],
              f"orm-only={sorted(r['cols'] - a['cols'])}, pg-only={sorted(a['cols'] - r['cols'])}")
        check(f"[{t}] 主键一致", r["pk"] == a["pk"], f"orm={sorted(r['pk'])} pg={sorted(a['pk'])}")
        check(f"[{t}] 外键一致", r["fks"] == a["fks"],
              f"orm={sorted(r['fks'])} pg={sorted(a['fks'])}")
        check(f"[{t}] 唯一约束一致", r["uniques"] == a["uniques"],
              f"orm={sorted(map(sorted, r['uniques']))} pg={sorted(map(sorted, a['uniques']))}")

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] ORM 模型声明与迁移后 PostgreSQL 实际 schema 完全一致")


if __name__ == "__main__":
    main()

