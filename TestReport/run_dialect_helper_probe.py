# -*- coding: utf-8 -*-
"""数据底座 S5 自检 · 方言 helper 探针（PostgreSQL 单引擎，隔离）。

验证 models.dialect 的收敛 helper 在 **PostgreSQL** 上行为正确、且产出的时间文本与全系统
逐字节兼容（S5：产品单跑 PG，sqlite 分支将在后续 cleanup 删除，故本探针只测 PG 分支）：

- now_expr()：PG 下编译为 to_char 归一化式；ORM 写入的时间与规范格式一致、能被 timeutil 解析。
- now_offset(-5)：PG 下编译为 to_char + interval 位移式。
- elapsed_seconds(end,start)：PG 下走 EXTRACT(EPOCH ...)，算出的秒差正确。
- elapsed_seconds_sql(...,"postgresql")：返回 EXTRACT(EPOCH 片段。
- insert_or_ignore()：PG 下走 on_conflict_do_nothing()，冲突忽略、不报错。

全在独立 PG 隔离库 + 隔离 config 里跑，绝不碰真实 akivili 库。

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

# import run_qa_suite 会 setdefault AKIVILI_TEST_NULLPOOL=1（跨事件循环的 asyncpg 必需），
# 并提供 isolated_pg_db_url（建唯一 PG 隔离库、进程退出自动删）。
from run_qa_suite import isolated_pg_db_url  # noqa: E402

PASS, FAIL = [], []
_FMT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _isolate(tmp):
    cfg = {"db_url": isolated_pg_db_url(), "providers": [], "default_provider_id": ""}
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)   # load_settings() 每次现读 CONFIG_FILE，无缓存需清


async def _run():
    import models
    import timeutil
    from sqlalchemy import insert, select, text
    from sqlalchemy.dialects import postgresql
    from db_migrate import run_migrations

    run_migrations()   # 建库唯一走 Alembic（PG 分支走 metadata.create_all）

    # 1) now_expr() 在 PG 编译为 to_char 归一化式（秒级 UTC text）
    compiled = str(models.now_expr().compile(dialect=postgresql.dialect()))
    check("now_expr() 在 PG 编译为 to_char 归一化式",
          "to_char" in compiled and "AT TIME ZONE 'UTC'" in compiled
          and "YYYY-MM-DD HH24:MI:SS" in compiled, f"compiled={compiled!r}")

    # 2) 运行期：用 now_expr() 作 created_at 写一行 projects，读回值须匹配规范格式
    #    （证明 PG 的 to_char 归一化产出与旧 sqlite datetime('now') 逐字节同形）
    async with models.get_session_factory()() as session:
        await session.execute(insert(models.Project).values(
            title="dialect-now", local_path="/tmp/x", created_at=models.now_expr()))
        await session.commit()
        via_expr = (await session.execute(
            select(models.Project.created_at).where(models.Project.title == "dialect-now")
        )).scalar_one()
    check("now_expr() 写入值匹配 YYYY-MM-DD HH:MM:SS", bool(_FMT.match(via_expr)), via_expr)

    # 3) timeutil.to_beijing 能解析 now_expr 写入的值（+8 小时、格式不变）
    bj = timeutil.to_beijing(via_expr)
    check("timeutil.to_beijing 可解析 now_expr 值", bool(_FMT.match(bj)) and bj != via_expr,
          f"utc={via_expr} bj={bj}")

    # 4) elapsed_seconds(end, start) 在 PG：造 started/ended 相差 5 秒，算出 ≈5。
    #    task_runs.task_id 有 FK→tasks（PG 强制 FK），先建父 project+task 再插 run。
    #    started/ended 用 now_offset/now_expr 元素写入 → @compiles postgresql 分支真正跑到。
    async with models.get_session_factory()() as session:
        proj = models.Project(title="elapsed-proj", local_path="/tmp/e")
        session.add(proj)
        await session.flush()
        task = models.Task(project_id=proj.id, title="elapsed-task")
        session.add(task)
        await session.flush()
        await session.execute(insert(models.TaskRun).values(
            task_id=task.id, started_at=models.now_offset(-5), ended_at=models.now_expr()))
        await session.commit()
        # SELECT 用 ORM 列表达式构造 → elapsed_seconds 的 postgresql @compiles 分支执行
        stmt = select(models.elapsed_seconds(models.TaskRun.ended_at, models.TaskRun.started_at)
                      ).where(models.TaskRun.task_id == task.id)
        secs = (await session.execute(stmt)).scalar_one()
    check("elapsed_seconds(PG) 算出≈5秒", 4.0 <= float(secs) <= 6.0, f"secs={secs}")

    # 5) elapsed_seconds_sql（PG）：字符串片段含 EXTRACT(EPOCH
    pg_frag = models.elapsed_seconds_sql("a", "b", "postgresql")
    check("elapsed_seconds_sql(PG) 返回 EXTRACT EPOCH 片段", "EXTRACT(EPOCH" in pg_frag, pg_frag)

    # 6) insert_or_ignore（PG）：agent_skills 复合主键，插两次同键 → on_conflict_do_nothing，
    #    第二次被忽略、不报错、仅 1 行。（按 engine 方言选 postgresql insert 构造器）
    async with models.get_session_factory()() as session:
        for _ in range(2):
            await session.execute(models.insert_or_ignore(models.AgentSkill).values(
                agent_slug="a1", skill_slug="s1"))
        await session.commit()
        cnt = (await session.execute(
            text("SELECT COUNT(*) FROM agent_skills WHERE agent_slug='a1'")
        )).scalar_one()
    check("insert_or_ignore(PG) 冲突忽略（插2次得1行）", cnt == 1, f"count={cnt}")

    # 7) now_offset(-5) 在 PG 编译为 to_char + interval 位移式
    off = str(models.now_offset(-5).compile(dialect=postgresql.dialect()))
    check("now_offset(-5) 在 PG 编译为 to_char interval 位移式",
          "to_char" in off and "interval '1 second'" in off, f"compiled={off!r}")

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
    print("[OK] 方言 helper 在 PostgreSQL 上行为正确、时间文本全系统兼容")


if __name__ == "__main__":
    main()
