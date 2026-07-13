"""Akivili scheduling probe — 并发度可配置 / 优先级排序 / 失败重试。

在隔离环境（临时 config/DB/workspace，绝不碰真实 jianagency.db）验证 collab.py 调度改造：
  1. MAX_CONCURRENCY / MAX_RETRY 从 Settings（config.json + 环境变量）读取生效。
  2. _claim_one 按任务优先级 high>medium>none 领取，同优先级 FIFO（id 升序）。
  3. 退避：失败重试的 run 在 next_retry_at 到点前不被领取。
  4. 异常型失败（execute_dispatch 抛异常）自动重试，attempts 累加，达上限后终落 failed。
  5. 判定型失败（_run_one 正常 return failed，如超时无交付）**不重试**，直接 failed。

Monkeypatch runner.execute_dispatch，不触发真实 CLI/LLM。跑完清理临时目录（除非 --keep）。
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
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


async def seed_project(paths: dict) -> tuple[int, dict]:
    """建隔离项目 + 4 名成员（1 负责人 + 3 worker）。返回 (pid, {slug:name})。"""
    import agents as agents_mod  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415

    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__sched_probe_project__", str(paths["project"]), "scheduling probe"))
        pid = cur.lastrowid
        members = [
            ("specialized-project-owner", "项目负责人", 1),
            ("qa-backend-developer", "后端开发者", 0),
            ("qa-frontend-developer", "前端开发者", 0),
            ("qa-tester", "测试专员", 0),
        ]
        name_by_slug = {}
        for slug, name, is_leader in members:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) "
                "VALUES (?,?,?,?,?,?)", (pid, slug, name, "🧩", f"你是{name}。", is_leader))
            name_by_slug[slug] = name
        await db.commit()
    finally:
        await db.close()
    return pid, name_by_slug


async def make_task(pid: int, title: str, priority: str = "none") -> int:
    from database import get_connection  # noqa: PLC0415

    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority) "
            "VALUES (?,?,?,?,?)", (pid, title, "", "in_progress", priority))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _rq_row(run_id: int) -> dict | None:
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        r = await (await db.execute("SELECT * FROM run_queue WHERE id=?", (run_id,))).fetchone()
        return dict(r) if r else None
    finally:
        await db.close()


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    await bootstrap_backend(paths)

    import collab  # noqa: PLC0415
    from executor import runner  # noqa: PLC0415
    from executor.base import ExecEvent  # noqa: PLC0415

    pid, _ = await seed_project(paths)

    # ---- Test A: 并发度 / 重试上限从 Settings 读取 ----
    # monkeypatch load_settings，验证 _apply_settings 把配置读进模块级 MAX_*（避开 CONFIG_FILE 文件竞态）
    import config as config_mod  # noqa: PLC0415
    orig_load = config_mod.load_settings

    class _FakeSettings:
        max_concurrency = 5
        max_retry = 1

    config_mod.load_settings = lambda: _FakeSettings()
    try:
        collab._apply_settings()
        probe.check("MAX_CONCURRENCY 从 Settings 生效", collab.MAX_CONCURRENCY == 5,
                    f"MAX_CONCURRENCY={collab.MAX_CONCURRENCY}（期望 5）")
        probe.check("MAX_RETRY 从 Settings 生效", collab.MAX_RETRY == 1,
                    f"MAX_RETRY={collab.MAX_RETRY}（期望 1）")
    finally:
        config_mod.load_settings = orig_load

    # ---- Test B: _claim_one 按优先级 high>medium>none，同级 FIFO ----
    # 反序入队（none 先、high 后），验证领取顺序由 priority 而非 id 决定。
    t_none = await make_task(pid, "__p_none__", "none")
    t_med = await make_task(pid, "__p_med__", "medium")
    t_high = await make_task(pid, "__p_high__", "high")
    q_none = await collab.enqueue_run(t_none, "qa-backend-developer", "", "assign", is_leader=False)
    q_med = await collab.enqueue_run(t_med, "qa-frontend-developer", "", "assign", is_leader=False)
    q_high = await collab.enqueue_run(t_high, "qa-tester", "", "assign", is_leader=False)
    order = []
    for _ in range(3):
        it = await collab._claim_one()
        order.append(it["id"])
    probe.check("优先级领取顺序 high>medium>none",
                order == [q_high, q_med, q_none], f"实际领取顺序={order}")
    # 同优先级 FIFO：两个 high，先入队的先领
    t_h1 = await make_task(pid, "__h1__", "high")
    t_h2 = await make_task(pid, "__h2__", "high")
    qh1 = await collab.enqueue_run(t_h1, "qa-backend-developer", "", "assign", is_leader=False)
    qh2 = await collab.enqueue_run(t_h2, "qa-frontend-developer", "", "assign", is_leader=False)
    fifo = [(await collab._claim_one())["id"], (await collab._claim_one())["id"]]
    probe.check("同优先级按 FIFO（入队 id 升序）", fifo == [qh1, qh2], f"实际={fifo}")

    # ---- Test C: 异常型失败自动重试 + 退避；达上限终落 failed ----
    collab.MAX_RETRY = 2
    run_seq = {"n": 0}
    fail_times = {"n": 0}

    async def fail_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        run_seq["n"] += 1
        fail_times["n"] += 1
        raise RuntimeError("模拟 CLI 冷启动失败")
        yield  # noqa: unreachable — 使其成为 async generator

    runner.execute_dispatch = fail_dispatch
    t_retry = await make_task(pid, "__retry__", "none")
    q_retry = await collab.enqueue_run(t_retry, "qa-tester", "", "assign", is_leader=False)

    it = await collab._claim_one()
    collab._running.add(it["id"])
    await collab._process_one(it)
    row = await _rq_row(q_retry)
    probe.check("异常型失败第1次：回 queued 且 attempts=1",
                row and row["status"] == "queued" and row["attempts"] == 1,
                f"status={row['status']} attempts={row['attempts']}")
    probe.check("退避已设置（next_retry_at 非空）",
                row and row["next_retry_at"], f"next_retry_at={row['next_retry_at']}")
    # 清退避窗口，模拟到点可领取
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET next_retry_at=NULL WHERE id=?", (q_retry,))
        await db.commit()
    finally:
        await db.close()

    # 退避窗口内不被领取：先设一个远期 next_retry_at，claim 应跳过它
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET next_retry_at=datetime('now','+300 seconds') WHERE id=?",
                         (q_retry,))
        await db.commit()
    finally:
        await db.close()
    skipped = await collab._claim_one()
    probe.check("退避窗口内的 run 不被领取", skipped is None,
                f"claim 返回={skipped['id'] if skipped else None}（应 None）")
    # 清退避，继续重试到上限
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET next_retry_at=NULL WHERE id=?", (q_retry,))
        await db.commit()
    finally:
        await db.close()
    it = await collab._claim_one()
    collab._running.add(it["id"])
    await collab._process_one(it)  # attempts→2（=MAX_RETRY，仍可再试一次后终止）
    row = await _rq_row(q_retry)
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET next_retry_at=NULL WHERE id=?", (q_retry,))
        await db.commit()
    finally:
        await db.close()
    it = await collab._claim_one()
    collab._running.add(it["id"])
    await collab._process_one(it)  # attempts→3 > MAX_RETRY=2 → 终落 failed
    row = await _rq_row(q_retry)
    probe.check("重试达上限后终落 failed",
                row and row["status"] == "failed" and row["attempts"] == 3,
                f"status={row['status']} attempts={row['attempts']}（期望 failed/3）")

    # ---- Test D: error 事件无产出（CLI/LLM 瞬时报错）属可重试类 ----
    async def err_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        run_seq["n"] += 1
        yield ExecEvent("system", "", {"run_id": f"tr-{run_seq['n']}"})
        yield ExecEvent("error", "boom")   # error 且无 text 产出 → 瞬时报错，可重试
        yield ExecEvent("done")

    runner.execute_dispatch = err_dispatch
    t_err = await make_task(pid, "__err_retry__", "none")
    q_err = await collab.enqueue_run(t_err, "qa-frontend-developer", "", "assign", is_leader=False)
    it = await collab._claim_one()
    collab._running.add(it["id"])
    await collab._process_one(it)
    row = await _rq_row(q_err)
    probe.check("error 无产出属可重试：回 queued attempts=1",
                row and row["status"] == "queued" and row["attempts"] == 1,
                f"status={row['status']} attempts={row['attempts']}（期望 queued/1）")

    # ---- Test E: 超时无交付（真卡死）不重试，直接 failed ----
    # 桩 hang 到底、不产事件 → idle 超时；grace 内仍无交付 → kill + failed，且不可重试。
    orig_idle, orig_grace = collab.IDLE_TIMEOUT_SEC, collab.GRACE_SEC
    collab.IDLE_TIMEOUT_SEC, collab.GRACE_SEC = 1, 1
    orig_kill = runner.kill_run
    runner.kill_run = lambda run_id: None

    async def hang_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        run_seq["n"] += 1
        yield ExecEvent("system", "", {"run_id": f"tr-{run_seq['n']}"})
        await asyncio.sleep(3600)   # 卡死，不再产出任何事件

    runner.execute_dispatch = hang_dispatch
    t_hang = await make_task(pid, "__hang_no_retry__", "none")
    q_hang = await collab.enqueue_run(t_hang, "qa-tester", "", "assign", is_leader=False)
    it = await collab._claim_one()
    collab._running.add(it["id"])
    await collab._process_one(it)
    row = await _rq_row(q_hang)
    collab.IDLE_TIMEOUT_SEC, collab.GRACE_SEC = orig_idle, orig_grace
    runner.kill_run = orig_kill
    probe.check("超时无交付不重试，直接 failed（attempts=1）",
                row and row["status"] == "failed" and row["attempts"] == 1,
                f"status={row['status']} attempts={row['attempts']}（期望 failed/1）")

    return probe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili-sched-"))
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
    print(f"\n=== scheduling probe: {passed}/{total} ===")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

