"""Akivili concurrency & hang-protection probe.

Verifies the collab.py concurrency pool + stuck-agent timeout in isolation:
  1. A hung agent is killed at the timeout and does NOT block the queue.
  2. The pool runs up to MAX_CONCURRENCY agents in parallel (not serial).
  3. A slow agent does not starve faster peers (fast ones finish first).

Uses a temporary config/DB/workspace under C:\\tmp (never the real jianagency.db).
Monkeypatches runner.execute_dispatch / runner.kill_run with in-memory fakes,
so no real CLI/LLM is invoked. Cleans up temp dir unless --keep.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

# Reuse isolation helpers from the main suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import BACKEND, setup_isolated_config, bootstrap_backend  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


async def seed_project_and_team(paths: dict) -> tuple[int, str, dict]:
    """Create an isolated project + import a small team; return (pid, leader_slug, {slug: name})."""
    import agents as agents_mod  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415

    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__conc_probe_project__", str(paths["project"]), "concurrency probe"),
        )
        pid = cur.lastrowid
        # Seed 4 members: leader + three workers, so MAX_CONCURRENCY=3 is exercised.
        members = [
            ("specialized-project-owner", "项目负责人", "🧭", 1),
            ("qa-backend-developer", "后端开发者", "🛠️", 0),
            ("qa-frontend-developer", "前端开发者", "🎨", 0),
            ("qa-tester", "测试专员", "✅", 0),
        ]
        name_by_slug = {}
        for slug, name, emoji, is_leader in members:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) "
                "VALUES (?,?,?,?,?,?)",
                (pid, slug, name, emoji, f"你是{name}。", is_leader),
            )
            name_by_slug[slug] = name
        await db.commit()
    finally:
        await db.close()
    return pid, "specialized-project-owner", name_by_slug


async def make_task(pid: int, title: str) -> int:
    from database import get_connection  # noqa: PLC0415

    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority) "
            "VALUES (?,?,?,?,?)",
            (pid, title, "", "in_progress", "none"),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    await bootstrap_backend(paths)

    import collab  # noqa: PLC0415
    from executor import runner  # noqa: PLC0415
    from executor.base import ExecEvent  # noqa: PLC0415

    pid, leader_slug, _ = await seed_project_and_team(paths)

    # ---- Fakes: replace real CLI/LLM execution with controllable in-memory behavior ----
    original_dispatch = runner.execute_dispatch
    original_kill = runner.kill_run

    active = {"count": 0, "peak": 0}          # live concurrency tracker
    killed: list[str] = []                    # run_ids passed to kill_run
    events: list[tuple[str, float]] = []      # (slug, finish_time) for finished agents
    run_id_seq = {"n": 0}

    def fake_kill(run_id: str) -> None:
        killed.append(run_id)

    # Per-slug behavior: hang forever, or sleep N seconds then emit text.
    behavior = {"hang_slugs": set(), "sleep_sec": 0.0}

    async def fake_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        slug = agent_obj["slug"]
        run_id_seq["n"] += 1
        rid = f"run-{run_id_seq['n']}-{slug}"
        yield ExecEvent("system", "", {"run_id": rid})
        active["count"] += 1
        active["peak"] = max(active["peak"], active["count"])
        try:
            if slug in behavior["hang_slugs"]:
                await asyncio.sleep(3600)      # simulate a stuck agent (until timeout kills us)
            else:
                await asyncio.sleep(behavior["sleep_sec"])
            events.append((slug, time.perf_counter()))
            yield ExecEvent("text", f"{slug} 完成。")
            yield ExecEvent("done")
        finally:
            active["count"] -= 1

    runner.execute_dispatch = fake_dispatch
    runner.kill_run = fake_kill
    # Shrink idle-timeout + grace so the hang test is fast (production idle=900s, grace=90s).
    # 新超时策略：静默(idle)超时 + 宽限(grace)保成果。假 hang agent 不产事件→触发 idle 超时；
    # 宽限内无交付→kill+failed。把两者都调到极小值让测试秒级完成。
    original_idle = collab.IDLE_TIMEOUT_SEC
    original_grace = collab.GRACE_SEC
    collab.IDLE_TIMEOUT_SEC = 1
    collab.GRACE_SEC = 1

    try:
        # ---------- Test 1: hung agent is killed at timeout, queue survives ----------
        behavior["hang_slugs"] = {"qa-backend-developer"}
        behavior["sleep_sec"] = 0.0
        t_hang = await make_task(pid, "__conc_hang__")
        await collab.enqueue_run(t_hang, "qa-backend-developer", "hang please", "qa", is_leader=False)

        item = await collab._claim_one()
        collab._running.add(item["id"])
        t0 = time.perf_counter()
        await collab._process_one(item)      # _run_one has the wait_for(RUN_TIMEOUT_SEC) inside
        hang_elapsed = time.perf_counter() - t0

        probe.check("卡死 Agent 在超时后被 kill",
                    len(killed) >= 1, f"kill_run 调用={killed}")
        probe.check("卡死执行在超时附近返回（未无限阻塞）",
                    hang_elapsed < 30, f"elapsed={hang_elapsed:.2f}s (idle={collab.IDLE_TIMEOUT_SEC}s+grace={collab.GRACE_SEC}s)")

        from database import get_connection  # noqa: PLC0415
        db = await get_connection()
        try:
            row = await (await db.execute(
                "SELECT status FROM run_queue WHERE id=?", (item["id"],))).fetchone()
            fail_note = await (await db.execute(
                "SELECT COUNT(*) c FROM activities WHERE task_id=? AND action='task_failed'",
                (t_hang,))).fetchone()
        finally:
            await db.close()
        collab._running.discard(item["id"])
        probe.check("卡死 run 标记为 done 并释放并发槽（不占死队列）",
                    row is not None and item["id"] not in collab._running,
                    f"status={row['status'] if row else None}")
        probe.check("卡死执行记录 task_failed 活动",
                    fail_note and fail_note["c"] >= 1, f"task_failed 活动数={fail_note['c'] if fail_note else 0}")

        # ---------- Test 2: pool runs up to MAX_CONCURRENCY in parallel ----------
        behavior["hang_slugs"] = set()
        behavior["sleep_sec"] = 0.4
        collab.IDLE_TIMEOUT_SEC = 30
        active["count"] = 0
        active["peak"] = 0
        events.clear()

        t_par = await make_task(pid, "__conc_parallel__")
        # Enqueue 3 distinct workers (dedupe is per task+agent, so distinct slugs coexist).
        for slug in ("qa-backend-developer", "qa-frontend-developer", "qa-tester"):
            await collab.enqueue_run(t_par, slug, "work", "qa", is_leader=False)

        # Drive the real background loop briefly.
        loop_task = asyncio.create_task(collab._loop())
        par_start = time.perf_counter()
        # Wait until all three finish (or safety cap).
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(events) >= 3:
                break
        par_elapsed = time.perf_counter() - par_start
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        probe.check("并发池峰值达到 MAX_CONCURRENCY（3 个并行）",
                    active["peak"] >= min(3, collab.MAX_CONCURRENCY),
                    f"peak={active['peak']}, MAX_CONCURRENCY={collab.MAX_CONCURRENCY}")
        # 3 tasks * 0.4s serial = 1.2s; parallel should be well under.
        probe.check("3 个并行执行明显快于串行（<0.9s）",
                    len(events) >= 3 and par_elapsed < 0.9,
                    f"elapsed={par_elapsed:.2f}s, finished={len(events)}")

        # ---------- Test 3: slow agent doesn't starve fast peers ----------
        active["peak"] = 0
        events.clear()
        t_mix = await make_task(pid, "__conc_mixed__")

        # Custom per-run delays: backend slow, others fast.
        delays = {"qa-backend-developer": 0.8, "qa-frontend-developer": 0.05, "qa-tester": 0.05}

        async def mixed_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
            slug = agent_obj["slug"]
            run_id_seq["n"] += 1
            yield ExecEvent("system", "", {"run_id": f"run-{run_id_seq['n']}-{slug}"})
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
            try:
                await asyncio.sleep(delays.get(slug, 0.1))
                events.append((slug, time.perf_counter()))
                yield ExecEvent("text", f"{slug} done")
                yield ExecEvent("done")
            finally:
                active["count"] -= 1

        runner.execute_dispatch = mixed_dispatch
        for slug in ("qa-backend-developer", "qa-frontend-developer", "qa-tester"):
            await collab.enqueue_run(t_mix, slug, "work", "qa", is_leader=False)

        loop_task = asyncio.create_task(collab._loop())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(events) >= 3:
                break
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        finish_order = [e[0] for e in events]
        probe.check("慢 Agent 不阻塞快 Agent（快的先完成）",
                    len(finish_order) >= 3 and finish_order[0] != "qa-backend-developer"
                    and finish_order[-1] == "qa-backend-developer",
                    f"finish_order={finish_order}")
    finally:
        runner.execute_dispatch = original_dispatch
        runner.kill_run = original_kill
        collab.IDLE_TIMEOUT_SEC = original_idle
        collab.GRACE_SEC = original_grace

    return probe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep temp dir for inspection")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili-conc-"))
    paths = setup_isolated_config(tmp)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")

    passed = sum(1 for _, ok, _ in probe.results if ok)
    total = len(probe.results)
    print(f"\nConcurrency probe: {passed}/{total} passed")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
