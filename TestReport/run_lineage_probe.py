"""Akivili 端到端链路可观测性探针（关联键打通）。

回归阶段一三个关联键：
  P1-1 run_queue.task_run_id：run 执行后回填其产生的 task_runs.id（打通两表，
       此前只能靠 task+slug+时间就近猜配对——task82 事故根因之一）。
  P1-2 messages.run_id：assistant 产出归因到具体执行 run。
  P1-3 run_queue.source_run_id/source_message_id：@ 触发的因果链（谁拉起了我）。

用假 runner.execute_dispatch（不调真实 CLI/LLM），隔离库，测完清理。
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
            ("链路探针项目", str(paths["project"]), "lineage probe"))
        pid = cur.lastrowid
        for slug, name in [("qa-backend-developer", "后端"), ("qa-frontend-developer", "前端")]:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) "
                "VALUES (?,?,?,?,?,0)", (pid, slug, name, "🤖", f"你是{name}。"))
        c = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, "任务A"))
        cid = c.lastrowid
        t = await db.execute(
            "INSERT INTO tasks (project_id, title, status, conversation_id) VALUES (?,?,?,?)",
            (pid, "任务A", "in_progress", cid))
        task_id = t.lastrowid
        await db.commit()
        return pid, task_id, cid
    finally:
        await db.close()


async def _rq(run_id):
    from database import get_connection
    db = await get_connection()
    try:
        r = await (await db.execute("SELECT * FROM run_queue WHERE id=?", (run_id,))).fetchone()
        return dict(r) if r else None
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

    # 假 dispatch：产出 system(run_id) + 一段 text（api 后端会落 assistant 消息，带 run_id）
    seq = {"n": 100}

    async def fake_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        seq["n"] += 1
        # 真建一个 task_runs 行（模拟 execute_dispatch 内部行为），yield 其 id
        db = await get_connection()
        try:
            c = await db.execute(
                "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) "
                "VALUES (?,?,?,?, 'succeeded')",
                (task_obj["id"], task_obj["conversation_id"], agent_obj["slug"], "p"))
            tr_id = c.lastrowid
            await db.commit()
        finally:
            await db.close()
        yield ExecEvent("system", "", {"run_id": tr_id})
        yield ExecEvent("text", f"{agent_obj['slug']} 完成分析。")
        yield ExecEvent("done")
    runner.execute_dispatch = fake_dispatch

    # ---- P1-1: run_queue.task_run_id 回填 = 该 run 产生的 task_runs.id ----
    q1 = await collab.enqueue_run(task_id, "qa-backend-developer", "", "assign", is_leader=False)
    item = await collab._claim_one()
    collab._running.add(item["id"])
    await collab._process_one(item)
    row = await _rq(q1)
    # 该 run_queue 项对应的 task_run（同 task+slug 的 succeeded run）
    db = await get_connection()
    try:
        tr = await (await db.execute(
            "SELECT id FROM task_runs WHERE task_id=? AND agent_slug='qa-backend-developer' "
            "ORDER BY id DESC LIMIT 1", (task_id,))).fetchone()
    finally:
        await db.close()
    probe.check("P1-1 run_queue.task_run_id 已回填",
                row and row["task_run_id"] is not None, f"task_run_id={row['task_run_id'] if row else None}")
    probe.check("P1-1 回填值 = 实际产生的 task_runs.id（两表打通）",
                row and tr and row["task_run_id"] == tr["id"],
                f"queue.task_run_id={row['task_run_id'] if row else None} vs task_runs.id={tr['id'] if tr else None}")

    # ---- P1-2: messages.run_id 归因到 run（_save_assistant 落的 assistant 消息带 run_id）----
    # 直接验证 _save_assistant 的 run_id 落库（真实 execute_dispatch 的 465 行即此调用；
    # 本探针 fake 了 dispatch，故直接测底层落库函数，不依赖桩重放该分支）。
    await runner._save_assistant(cid, "后端的最终结论。", author_slug="qa-backend-developer", run_id=tr["id"])
    db = await get_connection()
    try:
        m = await (await db.execute(
            "SELECT run_id, content FROM messages WHERE conversation_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1", (cid,))).fetchone()
    finally:
        await db.close()
    probe.check("P1-2 assistant 消息带 run_id（产出归因到执行）",
                m and m["run_id"] == tr["id"], f"run_id={m['run_id'] if m else None} vs {tr['id'] if tr else None}")

    # ---- P1-3: 因果链——被 @ 出的下游 run 记录 source ----
    # 模拟成员发言 @前端，经 parse_and_enqueue_mentions 带 source
    src_run, src_msg = 777, 888
    triggered = await collab.parse_and_enqueue_mentions(
        task_id, pid, "请 @前端 跟进", "qa-backend-developer", "",
        source_run_id=src_run, source_message_id=src_msg)
    db = await get_connection()
    try:
        drow = await (await db.execute(
            "SELECT source_run_id, source_message_id FROM run_queue "
            "WHERE task_id=? AND agent_slug='qa-frontend-developer' ORDER BY id DESC LIMIT 1",
            (task_id,))).fetchone()
    finally:
        await db.close()
    probe.check("P1-3 @ 触发的下游 run 记录 source_run_id（因果链）",
                drow and drow["source_run_id"] == src_run, f"source_run_id={drow['source_run_id'] if drow else None}")
    probe.check("P1-3 下游 run 记录 source_message_id",
                drow and drow["source_message_id"] == src_msg,
                f"source_message_id={drow['source_message_id'] if drow else None}")
    probe.check("P1-3 人工/系统直接发起的 run source 留空（不强记）",
                row and row["source_run_id"] is None,
                f"assign 入队 source_run_id={row['source_run_id'] if row else None}")

    # ---- P3-2/P3-1: 端到端链路下钻接口一次拼出链路 + 耗时聚合 ----
    from routes.runs import get_lineage
    lin = await get_lineage(task_id)
    probe.check("P3-2 链路接口拼出该任务的 run 链",
                lin["run_count"] >= 1 and len(lin["chain"]) == lin["run_count"],
                f"run_count={lin['run_count']}")
    # 链路项应带上关联键（task_run_id）与调度流水（events）
    first = lin["chain"][0]
    probe.check("P3-2 链路项含 task_run_id 关联 + run_events 流水",
                first["task_run_id"] is not None and isinstance(first["events"], list) and first["events"],
                f"task_run_id={first['task_run_id']} events={len(first['events'])}条")
    probe.check("P3-1 链路耗时聚合字段存在",
                "total_run_seconds" in lin and isinstance(lin["total_run_seconds"], (int, float)),
                f"total_run_seconds={lin['total_run_seconds']}")

    # ---- P3-3: 前端时间线视图依赖的字段契约（Runtime.vue 纯渲染 lineage 载荷）----
    # 视图直接消费这些键，缺任一都会导致时间线渲染异常，故锁契约
    summary_keys = {"task_id", "task_count", "run_count", "total_run_seconds", "failed_runs", "chain"}
    probe.check("P3-3 汇总载荷含视图所需全部字段",
                summary_keys.issubset(lin.keys()) and isinstance(lin["failed_runs"], list),
                f"缺失={summary_keys - set(lin.keys())}")
    item_keys = {
        "run_queue_id", "task_id", "agent_slug", "trigger", "is_leader",
        "queue_status", "attempts", "enqueued_at", "task_run_id", "run_status",
        "fail_reason", "started_at", "ended_at", "duration_seconds",
        "source_run_id", "source_message_id", "events",
    }
    missing = item_keys - set(first.keys())
    probe.check("P3-3 链路项含时间线视图所需全部字段（run 头/详情/耗时/因果）",
                not missing, f"缺失字段={missing or '无'}")
    # 每条 event 需含 event/detail/ts（视图按此渲染调度流水行）
    ev = first["events"][0]
    probe.check("P3-3 调度流水项含 event/detail/ts（视图流水行渲染契约）",
                {"event", "detail", "ts"}.issubset(ev.keys()),
                f"event键={sorted(ev.keys())}")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-lineage-"))
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
    print(f"\n=== lineage probe: {passed}/{total} ===")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
