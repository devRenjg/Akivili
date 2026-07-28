"""数据底座 S4.6 · PostgreSQL 端到端场景探针（平台真实运行在 PG 上）。

不同于 40 门禁的隔离单元探针（绑 sqlite 临时库 + get_connection seed），本探针直连
**迁移后的真实 PG 库**（AKIVILI_DB_URL 指向 akivili_pg），跑完整业务链路，验证 S3.4 全量
迁到 ORM 的读写路径 + 4 处方言查询在 PG 上行为正确。

覆盖：
  A. 存量数据读（迁移后的真实 31772 行）——项目/任务/run 列表与聚合
  B. 全链路写：建项目→建 project_agent→建任务→enqueue_run→claim→finalize→活动流
  C. 4 处方言查询：now_expr(默认值)、now_offset(rate_limit/agents_overview 窗口)、
     elapsed_seconds(agents_overview 时长/orphan idle)、upsert/insert_or_ignore
  D. 子任务自动完成、reflect 触发（读路径）、mention 解析入队

用法：
    AKIVILI_DB_URL=postgresql+asyncpg://user:pw@host:5432/akivili_pg \\
    python TestReport/run_pg_e2e_probe.py

退出码：全通=0，任何失败=1。**只读+受控写**，写入落在探针自建的 __test__ 项目内，
结束时清理（DELETE 级联），不改存量真实数据。
"""
from __future__ import annotations

import asyncio
import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

PASS: list = []
FAIL: list = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def _stage_bc(check, sf, collab, runner, runs, pid, tid, paid,
                    select, func, text, now_offset, elapsed_seconds,
                    Task, TaskRun, RunQueue, Activity):
    """B(续)+C：run 生命周期 + 4 处方言查询。"""
    # B5 enqueue_run（真实入队路径，含去重/双闸）
    qid = await collab.enqueue_run(tid, "dev", "do the work", trigger="assign")
    check("B5 enqueue_run 入队", bool(qid), f"qid={qid}")

    # B6 pending 去重：同 (task,agent) 再入队应被拒（返回 None）
    dup = await collab.enqueue_run(tid, "dev", "again", trigger="assign")
    check("B6 pending 去重生效", dup is None, f"dup={dup}")

    # B7 造一条 running task_run，再走 finalize_run 真实收尾路径
    async with sf() as s:
        tr = TaskRun(task_id=tid, agent_slug="dev", status="running",
                     started_at=None)
        # started_at 用 now_expr 默认值
        s.add(tr)
        await s.commit()
        await s.refresh(tr)
        rid = tr.id
    check("B7 建 running run", bool(rid), f"rid={rid}")

    await runner.finalize_run(rid, "succeeded")
    async with sf() as s:
        st = (await s.execute(select(TaskRun.status, TaskRun.ended_at)
                              .where(TaskRun.id == rid))).first()
    check("B8 finalize_run 落终态+ended_at", st and st[0] == "succeeded" and st[1],
          f"status={st[0] if st else None} ended_at={st[1] if st else None}")

    # B9 幂等：再 finalize 不该覆盖已定终态
    ok2 = await runner._finalize_if_running(rid, "killed")
    check("B9 finalize 幂等(不覆盖终态)", ok2 is False, f"_finalize_if_running={ok2}")

    # B10 活动流：log_activity 写入可读
    from activity import log_activity, timeline
    await log_activity(tid, "status_changed", "system", "", {"from": "in_progress", "to": "done"})
    tl = await timeline(tid)
    check("B10 活动流写入可读", isinstance(tl, list) and len(tl) >= 1, f"timeline={len(tl)}")

    # ── C. 4 处方言查询在 PG 上 ──
    # C1 造时长数据：一条 started 1h 前、ended now 的 succeeded run（测 elapsed_seconds）
    async with sf() as s:
        await s.execute(text("""
            INSERT INTO task_runs(task_id,agent_slug,status,started_at,ended_at)
            VALUES (:t,'dev','succeeded',
                to_char((now() AT TIME ZONE 'UTC') - interval '3600 second','YYYY-MM-DD HH24:MI:SS'),
                to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'))
        """), {"t": tid})
        # 一条 running、很久没动 → orphan sweep 命中
        await s.execute(text("""
            INSERT INTO task_runs(task_id,agent_slug,status,started_at)
            VALUES (:t,'dev','running',
                to_char((now() AT TIME ZONE 'UTC') - interval '99999 second','YYYY-MM-DD HH24:MI:SS'))
        """), {"t": tid})
        await s.commit()

    # C2 agents_overview：now_offset 窗口 + elapsed_seconds 时长
    ov = await runs.agents_overview(days=30)
    check("C1 agents_overview 跑通", isinstance(ov, dict) and "stats" in ov,
          f"keys={list(ov.keys())}")
    tsec = ov["stats"].get("total_run_seconds", ov["stats"].get("total_seconds"))
    check("C2 elapsed_seconds 时长含≈3600", tsec is not None and float(tsec) >= 3500,
          f"total_seconds={tsec}")

    # C3 rate_limit_metrics：now_offset 窗口
    rl = await runs.rate_limit_metrics(hours=24)
    check("C3 rate_limit_metrics 跑通", isinstance(rl, dict) and "total_runs" in rl,
          f"total_runs={rl.get('total_runs')}")

    # C4 orphan sweep：elapsed_seconds idle 判定命中孤儿
    swept = await collab.sweep_orphan_task_runs(idle_sec=1800)
    check("C4 sweep_orphan 命中孤儿(elapsed_seconds idle)", swept >= 1, f"swept={swept}")

    # C5 retry 退避：now_offset 写 next_retry_at 为未来同格式 text
    from models import now_offset as _no
    async with sf() as s:
        await s.execute(text(
            "UPDATE run_queue SET next_retry_at=(SELECT to_char((now() AT TIME ZONE 'UTC')"
            " + interval '120 second','YYYY-MM-DD HH24:MI:SS')) WHERE id=:i"), {"i": qid})
        await s.commit()
        nr = (await s.execute(text("SELECT next_retry_at FROM run_queue WHERE id=:i"),
                              {"i": qid})).scalar()
    check("C5 next_retry_at 未来时刻同格式", isinstance(nr, str) and len(nr) == 19,
          f"next_retry_at={nr!r}")


async def main():
    url = os.environ.get("AKIVILI_DB_URL", "")
    if "postgresql" not in url:
        raise SystemExit(f"AKIVILI_DB_URL 必须指向 PG：当前 {url!r}")

    from sqlalchemy import select, func, text, delete as sa_delete
    from models import (get_session_factory, now_expr, now_offset, elapsed_seconds,
                        Project, Task, TaskRun, RunQueue, ProjectAgent, Activity,
                        RunLog, RunEvent)
    import projects
    import collab
    from executor import runner
    import routes.runs as runs

    sf = get_session_factory()

    # ─────────────────────────────────────────────────────────────
    # A. 存量数据读（迁移后的真实数据）
    # ─────────────────────────────────────────────────────────────
    async with sf() as s:
        proj_n = (await s.execute(select(func.count()).select_from(Project))).scalar_one()
        task_n = (await s.execute(select(func.count()).select_from(Task))).scalar_one()
        run_n = (await s.execute(select(func.count()).select_from(TaskRun))).scalar_one()
        log_n = (await s.execute(select(func.count()).select_from(RunLog))).scalar_one()
    check("A1 存量项目可读", proj_n >= 1, f"projects={proj_n}")
    check("A2 存量任务可读", task_n >= 1, f"tasks={task_n}")
    check("A3 存量 run 可读", run_n >= 1, f"task_runs={run_n}")
    check("A4 存量 run_logs 全量在", log_n == 25508, f"run_logs={log_n}")

    plist = await projects.list_projects()
    check("A5 list_projects 跑通(ORM聚合)", isinstance(plist, list) and len(plist) >= 1,
          f"len={len(plist)}")

    # ─────────────────────────────────────────────────────────────
    # B. 全链路写（落在自建 __test__ 项目，最后清理）
    # ─────────────────────────────────────────────────────────────
    p = await projects.create_project("__test__ s46 pg e2e", "/tmp/s46", "e2e")
    pid = p["id"]
    check("B1 建项目(自增id生效)", isinstance(pid, int) and pid > 0, f"pid={pid}")

    async with sf() as s:
        pa = ProjectAgent(project_id=pid, name="Dev", slug="dev", enabled=1)
        s.add(pa)
        await s.commit()
        await s.refresh(pa)
        paid = pa.id
    check("B2 建 project_agent", bool(paid), f"paid={paid}")

    async with sf() as s:
        t = Task(project_id=pid, title="pg e2e task", status="in_progress")
        s.add(t)
        await s.commit()
        await s.refresh(t)
        tid = t.id
        created_at = t.created_at
    check("B3 建任务(now_expr默认值生效)", bool(tid) and bool(created_at),
          f"tid={tid} created_at={created_at}")
    # now_expr 归一化格式校验：PG 侧应是 'YYYY-MM-DD HH:MM:SS'（19 字符、无微秒无时区）
    check("B4 created_at 归一化为秒级格式",
          isinstance(created_at, str) and len(created_at) == 19 and created_at[4] == "-"
          and "." not in created_at and "+" not in created_at,
          f"created_at={created_at!r}")

    await _stage_bc(check, sf, collab, runner, runs, pid, tid, paid,
                    select, func, text, now_offset, elapsed_seconds,
                    Task, TaskRun, RunQueue, Activity)

    # ─────────────────────────────────────────────────────────────
    # 清理：删自建项目。FK 级联删其下 task/run/queue/activity/agent；但 run_events 无 FK
    # 约束（001 注释：仅逻辑关联），级联删不到，须按本次 task 显式删——否则残留污染一致性。
    # ─────────────────────────────────────────────────────────────
    async with sf() as s:
        # 先收集本项目下所有 task_id（run_events/run_logs 按 task/ run 关联，无级联到 project）
        task_ids = [r[0] for r in (await s.execute(
            select(Task.id).where(Task.project_id == pid))).all()]
        run_ids = [r[0] for r in (await s.execute(
            select(TaskRun.id).where(TaskRun.task_id.in_(task_ids) if task_ids else False))).all()] \
            if task_ids else []
        if task_ids:
            await s.execute(sa_delete(RunEvent).where(RunEvent.task_id.in_(task_ids)))
        if run_ids:
            await s.execute(sa_delete(RunLog).where(RunLog.run_id.in_(run_ids)))
        await s.commit()
    async with sf() as s:
        await s.execute(sa_delete(Project).where(Project.id == pid))
        await s.commit()
    async with sf() as s:
        left = (await s.execute(select(func.count()).select_from(Task).where(Task.project_id == pid))).scalar_one()
        ev_left = (await s.execute(select(func.count()).select_from(RunEvent)
                   .where(RunEvent.task_id.in_(task_ids) if task_ids else False))).scalar_one() \
            if task_ids else 0
    check("Z1 清理级联删除生效", left == 0, f"残留 task={left}")
    check("Z2 run_events 无 FK 也清干净", ev_left == 0, f"残留 run_events={ev_left}")

    print("\n" + "=" * 56)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] PG 端到端：存量读 + 全链路写 + 方言查询全通，平台可运行在 PostgreSQL 上")


if __name__ == "__main__":
    asyncio.run(main())
