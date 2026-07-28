# -*- coding: utf-8 -*-
"""数据底座 S3.4 第七批自检 · progress.py ORM 迁移等价性探针（隔离）。

验证父子任务进度聚合与联动：blocking_subtasks(EXISTS)、task_progress(聚合+
running/queued 分组+summarized)、_has_pending_run(动态 IN+exclude)、_set_reviewing/
_set_done(幂等+状态门)、on_execution_complete(子任务 done→父 reviewing 联动)、
maybe_advance_parent。用 Alembic 建库。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch7_probe.py
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


def _isolate(tmp):
    cfg = {
        "db_path": os.path.join(tmp, "batch7.db"),
        "agent_library_dir": os.path.join(tmp, "agents"),
        "skills_dir": os.path.join(tmp, "skills"),
        "memory_dir": os.path.join(tmp, "mem"),
        "providers": [], "default_provider_id": "",
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)


async def _mk_task(session, tid, title, status="in_progress", parent=None, assignee=""):
    from sqlalchemy import text
    await session.execute(text(
        "INSERT INTO tasks (id,project_id,title,status,parent_task_id,assignee_slug) "
        "VALUES (:i,80,:t,:s,:p,:a)"),
        {"i": tid, "t": title, "s": status, "p": parent, "a": assignee})


async def _mk_run(session, tid, status, agent="dev", is_leader=0, trigger="mention"):
    from sqlalchemy import text
    await session.execute(text(
        "INSERT INTO run_queue (task_id,agent_slug,status,is_leader,trigger) "
        "VALUES (:t,:a,:s,:l,:g)"),
        {"t": tid, "a": agent, "s": status, "l": is_leader, "g": trigger})


async def _run():
    import models
    import progress
    from db_migrate import run_migrations
    from sqlalchemy import text
    run_migrations()

    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (80,'进度项目','/p')"))
        # 父任务 900（负责人 lead）+ 2 子任务 901/902
        await _mk_task(s, 900, "父任务", "in_progress", None, "lead")
        await _mk_task(s, 901, "子1", "in_progress", 900, "dev")
        await _mk_task(s, 902, "子2", "in_progress", 900, "dev2")
        # 901 有个 running run（阻塞），902 无
        await _mk_run(s, 901, "running", "dev")
        await s.commit()

    # blocking_subtasks：901 在执行中 → 应返回 [901]
    blk = await progress.blocking_subtasks(900)
    check("blocking_subtasks 返回执行中子任务",
          len(blk) == 1 and blk[0]["id"] == 901, str(blk))
    check("blocking_subtasks dict 键", set(blk[0]) == {"id", "title", "assignee_slug", "status"}, str(set(blk[0])))

    # task_progress：sub_total=2, sub_done=0, running 含 901
    tp = await progress.task_progress(900)
    check("task_progress sub_total=2", tp["sub_total"] == 2, str(tp))
    check("task_progress running 含子901",
          any(x["task_id"] == 901 and x["is_sub"] for x in tp["running"]), str(tp["running"]))
    check("task_progress active=True", tp["active"] is True, str(tp))
    check("task_progress summarized=False(无收尾run)", tp["summarized"] is False, str(tp))

    # _has_pending_run：901 有 running → True；exclude 掉那个 run → False
    async with models.get_session_factory()() as s:
        rq = (await s.execute(text("SELECT id FROM run_queue WHERE task_id=901"))).scalar_one()
    check("_has_pending_run 有待跑=True", await progress._has_pending_run([901]) is True)
    check("_has_pending_run exclude 后=False",
          await progress._has_pending_run([901], exclude_run_id=rq) is False)

    # _set_reviewing 幂等：in_progress→reviewing 改动；再调不改
    ch1 = await progress._set_reviewing(902, "test")
    ch2 = await progress._set_reviewing(902, "test")
    check("_set_reviewing 首次改动", ch1 is True)
    check("_set_reviewing 幂等(reviewing 不再动)", ch2 is False)
    async with models.get_session_factory()() as s:
        st = (await s.execute(text("SELECT status FROM tasks WHERE id=902"))).scalar_one()
    check("_set_reviewing 状态确为 reviewing", st == "reviewing", st)

    # _set_done 幂等 + 状态门
    ch = await progress._set_done(902, "test")
    check("_set_done reviewing→done 改动", ch is True)
    ch = await progress._set_done(902, "test")
    check("_set_done 幂等(done 不再动)", ch is False)

    # on_execution_complete：把 901 的 run 标 done，再触发 901 完成
    # → 901 置 done；901、902 都 done 且无 pending run → 父 900 进 reviewing + 唤醒 lead
    async with models.get_session_factory()() as s:
        await s.execute(text("UPDATE run_queue SET status='done' WHERE task_id=901"))
        await s.commit()
    await progress.on_execution_complete(901)
    async with models.get_session_factory()() as s:
        s901 = (await s.execute(text("SELECT status FROM tasks WHERE id=901"))).scalar_one()
        s900 = (await s.execute(text("SELECT status FROM tasks WHERE id=900"))).scalar_one()
        # 父任务 reviewing 后应唤醒 lead 的 collaborate run
        lead_run = (await s.execute(text(
            "SELECT COUNT(*) FROM run_queue WHERE task_id=900 AND agent_slug='lead' "
            "AND trigger='collaborate' AND is_leader=1"))).scalar_one()
    check("on_execution_complete 子任务→done", s901 == "done", s901)
    check("on_execution_complete 全子done→父 reviewing", s900 == "reviewing", s900)
    check("on_execution_complete 唤醒负责人 collaborate run", lead_run == 1, f"cnt={lead_run}")

    # 独立顶层任务（无子任务）：执行完 → reviewing
    async with models.get_session_factory()() as s:
        await _mk_task(s, 950, "独立任务", "in_progress", None, "solo")
        await s.commit()
    await progress.on_execution_complete(950)
    async with models.get_session_factory()() as s:
        s950 = (await s.execute(text("SELECT status FROM tasks WHERE id=950"))).scalar_one()
    check("on_execution_complete 独立任务→reviewing", s950 == "reviewing", s950)

    await models.dispose_engine()


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch7_")
    _isolate(tmp)
    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] progress.py ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
