"""Akivili rate-limit probe — 429/限流命中识别 + 归因 + 命中率聚合接口。

在隔离环境（临时 config/DB/workspace，绝不碰真实 jianagency.db）验证限流观测点：
  1. _is_rate_limit_error 命中 429/rate limit/overloaded/quota 等信号，且不误伤普通错误。
  2. run 的 error 事件文本含限流信号且无产出 → fail_reason 归为 rate_limited（非 error_no_output）。
  3. 普通错误文本 → 仍归 error_no_output（不误报限流）。
  4. /runs/rate-limit-metrics 正确聚合窗口内 total/failed/rate_limited + 命中率 + 失败归因分布。

不触发真实 CLI/LLM（monkeypatch execute_dispatch 发 error 事件；metrics 直接造 task_runs）。
跑完清理临时目录（除非 --keep）。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import setup_isolated_config, bootstrap_backend  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def seed(paths: dict) -> tuple[int, int]:
    """建隔离项目 + 一名 agent + 一个任务，返回 (pid, task_id)。"""
    import agents as agents_mod  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__ratelimit_probe__", str(paths["project"]), "rate limit probe"))
        pid = cur.lastrowid
        await db.execute(
            "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) "
            "VALUES (?,?,?,?,?,0)", (pid, "rl-agent", "限流测试员", "🧪", "测试助手。"))
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority) "
            "VALUES (?,?,?,?,?)", (pid, "__rl_task__", "", "in_progress", "none"))
        task_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    return pid, task_id


def _fake_dispatch_error(error_text: str):
    """execute_dispatch 替身：建一条真实 task_run（供 fail_reason 落库）、发 system(run_id) +
    带指定文本的 error 事件、无 text 产出。模拟真实 dispatch 的事件序列。"""
    from executor.base import ExecEvent
    from database import get_connection

    async def _gen(task, agent, prompt, persist_user_msg=True):
        db = await get_connection()
        try:
            cur = await db.execute(
                "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) "
                "VALUES (?,?,?,?, 'running')",
                (task.get("id") if isinstance(task, dict) else task["id"],
                 None, agent.get("slug", ""), ""))
            run_id = cur.lastrowid
            await db.commit()
        finally:
            await db.close()
        yield ExecEvent("system", "", {"run_id": run_id})
        yield ExecEvent("error", error_text)
        yield ExecEvent("done")
    return _gen


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    await bootstrap_backend(paths)
    import collab  # noqa: PLC0415
    from executor import runner  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415

    # ---- Test 1: _is_rate_limit_error 识别 ----
    hits = ["HTTP 429 Too Many Requests", "rate limit exceeded", "Error: overloaded_error",
            "you have exceeded your quota", "retry-after: 30", "API capacity reached"]
    miss = ["file not found", "syntax error near unexpected token", "exit code 1",
            "connection refused", ""]
    all_hit = all(collab._is_rate_limit_error(t) for t in hits)
    no_false = not any(collab._is_rate_limit_error(t) for t in miss)
    probe.check("限流信号全部命中（429/rate limit/overloaded/quota/retry-after/capacity）",
                all_hit, f"hits={all_hit}")
    probe.check("普通错误不误报限流", no_false, f"no_false_positive={no_false}")

    # ---- Test 2: 限流 error 文本 + 无产出 → fail_reason=rate_limited ----
    pid, tid = await seed(paths)
    orig = runner.execute_dispatch
    runner.execute_dispatch = _fake_dispatch_error("Error 429: rate_limit_error, please retry-after 60s")
    try:
        rq = await collab.enqueue_run(tid, "rl-agent", "触发限流", "assign", is_leader=False)
        await collab._process_one(await _claim_by_id(rq))
    finally:
        runner.execute_dispatch = orig
    fr = await _last_fail_reason(tid)
    probe.check("限流 error 无产出 → fail_reason=rate_limited", fr == "rate_limited", f"fail_reason={fr}")

    # ---- Test 3: 普通 error 文本 → error_no_output（不误报）----
    pid2, tid2 = await seed(paths)
    runner.execute_dispatch = _fake_dispatch_error("bash: command not found")
    try:
        rq2 = await collab.enqueue_run(tid2, "rl-agent", "普通错误", "assign", is_leader=False)
        await collab._process_one(await _claim_by_id(rq2))
    finally:
        runner.execute_dispatch = orig
    fr2 = await _last_fail_reason(tid2)
    probe.check("普通 error → error_no_output（不误报限流）", fr2 == "error_no_output", f"fail_reason={fr2}")

    # ---- Test 4: /runs/rate-limit-metrics 聚合 ----
    # 直接造窗口内 task_runs：2 条 rate_limited、1 条 error_no_output、1 条成功
    db = await get_connection()
    try:
        for frv, st in [("rate_limited", "failed"), ("rate_limited", "failed"),
                        ("error_no_output", "failed"), ("", "succeeded")]:
            await db.execute(
                "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status, "
                "fail_reason, started_at, ended_at) "
                "VALUES (?,?,?,?,?,?, datetime('now','-5 minutes'), datetime('now'))",
                (tid, None, "rl-agent", "", st, frv))
        await db.commit()
    finally:
        await db.close()
    from routes.runs import rate_limit_metrics
    m = await rate_limit_metrics(hours=24)
    probe.check("metrics 统计到 rate_limited 计数", m["rate_limited_runs"] >= 2,
                f"rate_limited_runs={m['rate_limited_runs']}")
    probe.check("metrics 命中率字段计算正确（rate_limited/total 在 0~1）",
                0 < m["rate_limit_hit_rate"] <= 1, f"hit_rate={m['rate_limit_hit_rate']}")
    probe.check("metrics 失败归因分布含 rate_limited",
                m["by_fail_reason"].get("rate_limited", 0) >= 2, f"by_fail_reason={m['by_fail_reason']}")
    probe.check("metrics 命中率 = rate_limited / total 一致",
                m["total_runs"] > 0 and abs(m["rate_limit_hit_rate"] - m["rate_limited_runs"] / m["total_runs"]) < 1e-6,
                f"hit_rate={m['rate_limit_hit_rate']} total={m['total_runs']}")

    return probe


async def _claim_by_id(rq_id: int) -> dict:
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET status='running' WHERE id=?", (rq_id,))
        await db.commit()
        r = await (await db.execute("SELECT * FROM run_queue WHERE id=?", (rq_id,))).fetchone()
        return dict(r)
    finally:
        await db.close()


async def _last_fail_reason(task_id: int) -> str:
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        r = await (await db.execute(
            "SELECT fail_reason FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,))).fetchone()
        return r["fail_reason"] if r else "(no task_run)"
    finally:
        await db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-ratelimit-"))
    paths = setup_isolated_config(tmp)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")
    total = len(probe.results)
    passed = sum(1 for r in probe.results if r[1])
    print(f"\n=== rate limit probe: {passed}/{total} ===")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
