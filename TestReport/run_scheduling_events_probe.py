"""Akivili 调度可观测性探针（阶段二：run_events 流水 + fail_reason 结构化）。

回归：
  P2-1 run_events：入队(enqueued)/领取(claimed)/重试(retry)/终态(succeeded|failed) 落调度流水表，
       与面向用户的 activities 分开（不污染详情页时间线）。
  P2-2 task_runs.fail_reason：失败结构化归因——exception / timeout_idle 等落 task_runs。

用假 runner.execute_dispatch，隔离库，测完清理。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import setup_isolated_config, bootstrap_backend  # noqa: E402


class Probe:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(r[1] for r in self.results)


async def seed(paths):
    import agents as agents_mod
    from database import get_connection
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("调度事件探针", str(paths["project"]), "sched events probe"))
        pid = cur.lastrowid
        await db.execute(
            "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) "
            "VALUES (?,?,?,?,?,0)", (pid, "qa-backend-developer", "后端", "🤖", "你是后端。"))
        c = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, "任务A"))
        cid = c.lastrowid
        t = await db.execute(
            "INSERT INTO tasks (project_id, title, status, conversation_id) VALUES (?,?,?,?)",
            (pid, "任务A", "in_progress", cid))
        tid = t.lastrowid
        await db.commit()
        return pid, tid, cid
    finally:
        await db.close()


async def _events(run_queue_id):
    from database import get_connection
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT event, detail FROM run_events WHERE run_queue_id=? ORDER BY id",
            (run_queue_id,))).fetchall()
        return [(r["event"], r["detail"]) for r in rows]
    finally:
        await db.close()


async def _task_run_fail_reason(task_id, slug):
    from database import get_connection
    db = await get_connection()
    try:
        r = await (await db.execute(
            "SELECT fail_reason FROM task_runs WHERE task_id=? AND agent_slug=? ORDER BY id DESC LIMIT 1",
            (task_id, slug))).fetchone()
        return r["fail_reason"] if r else None
    finally:
        await db.close()


async def run_probe(paths, keep):
    probe = Probe()
    await bootstrap_backend(paths)
    import collab
    from executor import runner
    from executor.base import ExecEvent
    from database import get_connection

    pid, task_id, cid = await seed(paths)

    # ---- Test 1: 成功 run → enqueued + claimed + succeeded 事件 ----
    async def ok_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        db = await get_connection()
        try:
            c = await db.execute(
                "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) "
                "VALUES (?,?,?,?, 'succeeded')",
                (task_obj["id"], task_obj["conversation_id"], agent_obj["slug"], "p"))
            trid = c.lastrowid
            await db.commit()
        finally:
            await db.close()
        yield ExecEvent("system", "", {"run_id": trid})
        yield ExecEvent("text", "完成。")
        yield ExecEvent("done")
    runner.execute_dispatch = ok_dispatch

    q1 = await collab.enqueue_run(task_id, "qa-backend-developer", "", "assign", is_leader=False)
    item = await collab._claim_one()
    collab._running.add(item["id"])
    await collab._process_one(item)
    evs = [e for e, _ in await _events(q1)]
    # 终态事件用 run_queue 语义（done/failed），与 run_events 主键 run_queue_id 一致
    probe.check("成功 run 事件序列含 enqueued/claimed/done",
                "enqueued" in evs and "claimed" in evs and "done" in evs, f"events={evs}")

    # ---- Test 2: 异常 run → retry 事件 + 终态 failed + fail_reason=exception ----
    collab.MAX_RETRY = 1
    async def boom_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        raise RuntimeError("boom")
        yield  # noqa
    runner.execute_dispatch = boom_dispatch

    q2 = await collab.enqueue_run(task_id, "qa-backend-developer", "", "assign", is_leader=False)
    # 第1次：异常→retry
    it = await collab._claim_one()
    collab._running.add(it["id"])
    await collab._process_one(it)
    # 清退避，第2次：异常→达上限 failed
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET next_retry_at=NULL WHERE id=?", (q2,))
        await db.commit()
    finally:
        await db.close()
    it = await collab._claim_one()
    collab._running.add(it["id"])
    await collab._process_one(it)
    evs2 = [e for e, _ in await _events(q2)]
    probe.check("异常 run 含 retry 事件", "retry" in evs2, f"events={evs2}")
    probe.check("异常 run 达上限落 failed 事件", "failed" in evs2, f"events={evs2}")
    import json
    failed_detail = next((json.loads(d) for e, d in await _events(q2) if e == "failed"), {})
    probe.check("failed 事件 detail 带 fail_reason=exception",
                failed_detail.get("fail_reason") == "exception", f"detail={failed_detail}")

    # ---- Test 3: run_events 独立于 activities（调度噪声不进用户时间线）----
    db = await get_connection()
    try:
        act_events = await (await db.execute(
            "SELECT DISTINCT action FROM activities WHERE task_id=?", (task_id,))).fetchall()
        re_count = (await (await db.execute(
            "SELECT COUNT(*) c FROM run_events WHERE task_id=?", (task_id,))).fetchone())["c"]
    finally:
        await db.close()
    acts = {r["action"] for r in act_events}
    probe.check("调度事件不写 activities（enqueued/claimed/retry 不在 activities.action）",
                not ({"enqueued", "claimed", "retry"} & acts), f"activities.action={acts}")
    probe.check("run_events 有调度流水记录", re_count > 0, f"run_events 行数={re_count}")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-schedev-"))
    paths = setup_isolated_config(tmp)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")
    total = len(probe.results)
    passed = sum(1 for r in probe.results if r[1])
    print(f"\n=== scheduling events probe: {passed}/{total} ===")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
