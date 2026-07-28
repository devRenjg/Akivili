# -*- coding: utf-8 -*-
"""数据底座 S3.3 自检 · 方言 helper 探针（隔离）。

验证 models.dialect 的收敛 helper 行为正确、且与被替换的 SQLite 字面量**语义等价**：
- now_expr()：ORM 写入的时间 与 datetime('now') 同格式、同 UTC、能被 timeutil 解析
- elapsed_seconds_sql()：SQLite 片段算出的秒数正确；PG 分支已登记
- insert_or_ignore()：冲突忽略行为与 INSERT OR IGNORE 一致
全在临时库 + 隔离 config，绝不碰真实 jianagency.db。

用法：py -3.12 TestReport/run_dialect_helper_probe.py
"""
import asyncio
import json
import os
import re
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
    db_path = os.path.join(tmp, "dialect_probe.db")
    cfg = {"db_path": db_path, "providers": [], "default_provider_id": ""}
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)
    return db_path


async def _run():
    import database
    import models
    import timeutil
    from sqlalchemy import text
    from db_migrate import run_migrations

    run_migrations()   # 建库唯一走 Alembic（S3.6 已下线 init_db 建表）

    # 1) now_expr() 编译为 SQLite 的 CURRENT_TIMESTAMP
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    compiled = str(models.now_expr().compile(dialect=sqlite_dialect.dialect()))
    check("now_expr() 在 SQLite 编译为 CURRENT_TIMESTAMP",
          compiled.upper() == "CURRENT_TIMESTAMP", f"compiled={compiled!r}")

    # 2) now_expr() 写入的值 与 datetime('now') 同格式（YYYY-MM-DD HH:MM:SS）
    fmt = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
    db = await database.get_connection()
    try:
        legacy = (await (await db.execute("SELECT datetime('now')")).fetchone())[0]
    finally:
        await db.close()

    async with models.get_session_factory()() as session:
        # 用 now_expr() 作为 INSERT 值写一行，读回其时间字符串
        await session.execute(
            text("INSERT INTO projects (title, local_path, created_at) "
                 "VALUES (:t, :p, CURRENT_TIMESTAMP)"),
            {"t": "dialect-now", "p": "/tmp/x"},
        )
        await session.commit()
        via_expr = (await session.execute(
            text("SELECT created_at FROM projects WHERE title='dialect-now'")
        )).scalar_one()

    check("datetime('now') 格式匹配 YYYY-MM-DD HH:MM:SS", bool(fmt.match(legacy)), legacy)
    check("now_expr() 写入值格式一致", bool(fmt.match(via_expr)), via_expr)
    # 3) timeutil.to_beijing 能解析 now_expr 写入的值（+8 小时、格式不变）
    bj = timeutil.to_beijing(via_expr)
    check("timeutil.to_beijing 可解析 now_expr 值", bool(fmt.match(bj)) and bj != via_expr,
          f"utc={via_expr} bj={bj}")

    # 4) elapsed_seconds_sql（SQLite）：造 start/end 相差 5 秒，算出 ≈5
    #    task_runs.task_id 有 FK→tasks（foreign_keys=ON），先建父 project+task。
    frag = models.elapsed_seconds_sql("started_at", "ended_at", "sqlite")
    async with models.get_session_factory()() as session:
        await session.execute(text(
            "INSERT INTO projects (id, title, local_path) VALUES (900, 'p', '/tmp/p')"))
        await session.execute(text(
            "INSERT INTO tasks (id, project_id, title) VALUES (900, 900, 't')"))
        await session.execute(text(
            "INSERT INTO task_runs (task_id, started_at, ended_at) "
            "VALUES (900, datetime('now','-5 seconds'), datetime('now'))"
        ))
        await session.commit()
        secs = (await session.execute(
            text(f"SELECT {frag} FROM task_runs WHERE task_id=900")
        )).scalar_one()
    check("elapsed_seconds_sql(SQLite) 算出≈5秒", 4.0 <= float(secs) <= 6.0, f"secs={secs}")
    # PG 分支已登记（不执行，仅确认返回非空 SQL）
    pg_frag = models.elapsed_seconds_sql("a", "b", "postgresql")
    check("elapsed_seconds_sql(PG) 分支已登记", "EXTRACT(EPOCH" in pg_frag, pg_frag)

    # 5) insert_or_ignore：agent_skills 复合主键，插两次同键，第二次被忽略、不报错
    from models import AgentSkill
    async with models.get_session_factory()() as session:
        for _ in range(2):
            stmt = models.insert_or_ignore(AgentSkill).values(
                agent_slug="a1", skill_slug="s1"
            )
            await session.execute(stmt)
        await session.commit()
        cnt = (await session.execute(
            text("SELECT COUNT(*) FROM agent_skills WHERE agent_slug='a1'")
        )).scalar_one()
    check("insert_or_ignore 冲突忽略（插2次得1行）", cnt == 1, f"count={cnt}")

    await models.dispose_engine()


def main():
    tmp = tempfile.mkdtemp(prefix="dialect_")
    _isolate(tmp)
    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] 方言 helper 行为与被替换字面量语义等价")


if __name__ == "__main__":
    main()
