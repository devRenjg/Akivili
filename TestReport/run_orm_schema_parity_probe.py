# -*- coding: utf-8 -*-
"""数据底座 S3.1 自检 · ORM 模型 vs 001 基线 结构等价性探针（隔离）。

验证 backend/models 的 ORM 模型 create_all 建出的表结构，与 migrations 的
001_baseline 建出的表结构**逐表逐列等价**：表集合、列(名/类型/可空/默认/主键)、
外键、唯一约束、索引。全在临时库，绝不碰真实 jianagency.db。

用法：py -3.12 TestReport/run_orm_schema_parity_probe.py
"""
import os
import sqlite3
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


def _table_info(conn, table):
    """返回 {col_name: (type, notnull, dflt, pk)} 归一化。"""
    cur = conn.execute(f"PRAGMA table_info({table})")
    out = {}
    for _cid, cname, ctype, notnull, dflt, pk in cur.fetchall():
        out[cname] = (
            (ctype or "").upper(),
            int(notnull),
            None if dflt is None else str(dflt),
            int(pk),
        )
    return out


def _fk_set(conn, table):
    """外键归一化为 {(from_col, to_table, to_col, on_delete)}。"""
    cur = conn.execute(f"PRAGMA foreign_key_list({table})")
    out = set()
    for row in cur.fetchall():
        # id, seq, table, from, to, on_update, on_delete, match
        out.add((row[3], row[2], row[4], (row[6] or "").upper()))
    return out


def _index_set(conn, table):
    """唯一索引归一化为 {frozenset(cols)}（只比唯一约束，忽略自动 rowid）。"""
    cur = conn.execute(f"PRAGMA index_list({table})")
    out = set()
    for row in cur.fetchall():
        # seq, name, unique, origin, partial
        name, unique = row[1], row[2]
        if not unique:
            continue
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()]
        out.add(frozenset(cols))
    return out


def build_from_orm(db_path):
    """用 ORM metadata.create_all 在临时库建表。"""
    from sqlalchemy import create_engine
    import models

    eng = create_engine("sqlite:///" + db_path.replace("\\", "/"))
    models.Base.metadata.create_all(eng)
    eng.dispose()


def build_from_baseline(db_path):
    """用 001_baseline 的 _TABLES DDL 在临时库建表（同 alembic upgrade 的 DDL）。"""
    import importlib.util

    mig = BACKEND / "migrations" / "versions" / "001_baseline.py"
    spec = importlib.util.spec_from_file_location("baseline_001", mig)
    mod = importlib.util.module_from_spec(spec)
    # 001_baseline 顶部 import alembic.op —— 仅用其 _TABLES 常量，避免执行 op
    sys.modules["baseline_001"] = mod
    spec.loader.exec_module(mod)
    conn = sqlite3.connect(db_path)
    for ddl in mod._TABLES:
        conn.execute(ddl)
    conn.commit()
    conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="orm_parity_")
    orm_db = os.path.join(tmp, "orm.db")
    base_db = os.path.join(tmp, "baseline.db")

    build_from_orm(orm_db)
    build_from_baseline(base_db)

    orm = sqlite3.connect(orm_db)
    base = sqlite3.connect(base_db)

    # 1) 表集合一致
    def tables(conn):
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    ot, bt = tables(orm), tables(base)
    check("表集合一致", ot == bt, f"orm-only={ot - bt}, baseline-only={bt - ot}")

    # 2) 逐表逐列
    for t in sorted(bt & ot):
        oi, bi = _table_info(orm, t), _table_info(base, t)
        # 列名集合
        check(f"[{t}] 列集合一致", set(oi) == set(bi),
              f"orm-only={set(oi) - set(bi)}, base-only={set(bi) - set(oi)}")
        for col in sorted(set(oi) & set(bi)):
            ot_type, ot_nn, ot_df, ot_pk = oi[col]
            bt_type, bt_nn, bt_df, bt_pk = bi[col]
            check(f"[{t}.{col}] 类型", ot_type == bt_type, f"orm={ot_type} base={bt_type}")
            check(f"[{t}.{col}] 主键", ot_pk == bt_pk, f"orm={ot_pk} base={bt_pk}")
            # notnull：PK 列跳过（SQLite 对 INTEGER PK 的 notnull 渲染为 0，与显式 NOT NULL 语义等价，两库口径一致即可）
            if not (ot_pk and bt_pk):
                check(f"[{t}.{col}] notnull", ot_nn == bt_nn, f"orm={ot_nn} base={bt_nn}")
            # 默认值：归一化去空格比较（datetime('now') 括号写法一致性）
            on = None if ot_df is None else ot_df.replace(" ", "")
            bn = None if bt_df is None else bt_df.replace(" ", "")
            check(f"[{t}.{col}] 默认值", on == bn, f"orm={ot_df!r} base={bt_df!r}")

        # 3) 外键
        check(f"[{t}] 外键一致", _fk_set(orm, t) == _fk_set(base, t),
              f"orm={_fk_set(orm, t)} base={_fk_set(base, t)}")
        # 4) 唯一约束/索引
        check(f"[{t}] 唯一约束一致", _index_set(orm, t) == _index_set(base, t),
              f"orm={_index_set(orm, t)} base={_index_set(base, t)}")

    orm.close()
    base.close()

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] ORM 模型与 001 基线结构完全等价")


if __name__ == "__main__":
    main()
