# -*- coding: utf-8 -*-
"""数据底座 S3.2 自检 · ORM async engine/session 调优对齐探针（隔离）。

核心验证：ORM engine 每条连接的 PRAGMA（journal_mode/busy_timeout/foreign_keys）
与 S1 的 database.get_connection() **逐条一致**；session 能连库、能读写、能收口。
全在临时库 + 隔离 config，绝不碰真实 jianagency.db。

用法：py -3.12 TestReport/run_orm_engine_probe.py
"""
import asyncio
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


def _isolate(tmp):
    """建隔离 config：db_path 指向临时库，busy_timeout 设一个非默认值以验证确实读了 config。"""
    db_path = os.path.join(tmp, "engine_probe.db")
    cfg = {
        "db_path": db_path,
        "db_busy_timeout_ms": 7777,  # 非默认 5000，验证 engine 确实按 config 生效
        "providers": [],
        "default_provider_id": "",
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)
    return db_path


async def _pragmas_via_get_connection():
    """S1 路径：database.get_connection() 读出三条 PRAGMA 实际值。"""
    import database
    db = await database.get_connection()
    try:
        jm = (await (await db.execute("PRAGMA journal_mode")).fetchone())[0]
        bt = (await (await db.execute("PRAGMA busy_timeout")).fetchone())[0]
        fk = (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0]
        return str(jm).lower(), int(bt), int(fk)
    finally:
        await db.close()


async def _pragmas_via_orm_engine():
    """S3.2 路径：ORM engine 一条 session 读出三条 PRAGMA 实际值。"""
    from sqlalchemy import text
    import models
    async with models.get_session_factory()() as session:
        jm = (await session.execute(text("PRAGMA journal_mode"))).scalar_one()
        bt = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
        fk = (await session.execute(text("PRAGMA foreign_keys"))).scalar_one()
        return str(jm).lower(), int(bt), int(fk)


async def _run():
    import database
    import models
    from db_migrate import run_migrations

    # 建库唯一走 Alembic（S3.6 已下线 init_db；env.py 已置 WAL），engine 连同一个库
    run_migrations()

    # 1) 连通性：ping 返回 1
    val = await models.ping()
    check("engine.ping() == 1", val == 1, f"got {val}")

    # 2) PRAGMA 逐条对齐 S1
    s1_jm, s1_bt, s1_fk = await _pragmas_via_get_connection()
    orm_jm, orm_bt, orm_fk = await _pragmas_via_orm_engine()

    check("journal_mode 对齐 S1", orm_jm == s1_jm == "wal", f"orm={orm_jm} s1={s1_jm}")
    check("busy_timeout 对齐 S1", orm_bt == s1_bt == 7777,
          f"orm={orm_bt} s1={s1_bt}（config=7777）")
    check("foreign_keys 对齐 S1", orm_fk == s1_fk == 1, f"orm={orm_fk} s1={s1_fk}")

    # 3) session 能读到 S1 路径写入的数据（同一个库、engine 池化生效）
    db = await database.get_connection()
    try:
        await db.execute(
            "INSERT INTO projects (title, local_path) VALUES (?, ?)",
            ("engine-probe-proj", "/tmp/x"),
        )
        await db.commit()
    finally:
        await db.close()

    from sqlalchemy import text
    async with models.get_session_factory()() as session:
        cnt = (await session.execute(
            text("SELECT COUNT(*) FROM projects WHERE title='engine-probe-proj'")
        )).scalar_one()
    check("ORM session 读到 S1 写入的行", cnt == 1, f"count={cnt}")

    # 4) 外键强制生效（foreign_keys=ON 的行为验证）：插一条违反 FK 的 skill_downloads 应报错
    from sqlalchemy.exc import IntegrityError
    fk_enforced = False
    try:
        async with models.get_session_factory()() as session:
            await session.execute(
                text("INSERT INTO skill_downloads (skill_id, ip) VALUES (999999, 'x')")
            )
            await session.commit()
    except IntegrityError:
        fk_enforced = True
    check("外键约束实际生效（违反 FK 被拒）", fk_enforced,
          "插入不存在 skill_id 应触发 FOREIGN KEY 约束")

    # 5) engine 单例：两次 get_engine 同一实例
    check("get_engine 单例", models.get_engine() is models.get_engine())

    # 6) dispose 后可重建（不崩）
    await models.dispose_engine()
    val2 = await models.ping()
    check("dispose 后 ping 仍 == 1（可重建）", val2 == 1, f"got {val2}")

    await models.dispose_engine()


def main():
    tmp = tempfile.mkdtemp(prefix="orm_engine_")
    _isolate(tmp)
    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] ORM engine 调优与 S1 get_connection 完全对齐")


if __name__ == "__main__":
    main()
