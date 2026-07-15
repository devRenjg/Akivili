"""Akivili task-gates probe — 单任务运行闸（总量闸 + 循环闸）可配置 & 熔断口径。

在隔离环境（临时 config/DB/workspace，绝不碰真实 jianagency.db）验证 collab.py 双闸熔断：
  1. MAX_RUNS_PER_TASK / MAX_MENTION_CHAIN 从 Settings（config.json + 环境变量）读取生效。
  2. 总量闸：任务累计入队 run 达 MAX_RUNS_PER_TASK 后拒绝新入队（绝对失控兜底）。
  3. 循环闸：连续 mention 链式自动 run（trigger=mention 且 source_run_id 非空）达
     MAX_MENTION_CHAIN 后拒绝该类入队（防 Agent 互相 @ 死循环）。
  4. 人工/负责人/指派介入（assign/collaborate 或 source 留空）打断链，链长清零重来
     —— 长程项目不受循环闸误伤。
  5. 总量闸放大后（>原 20），长程任务能持续入队（不再 20 次即停）。

不触发真实 CLI/LLM（只调 enqueue_run + 直接改 run_queue 状态绕开 pending 去重）。
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
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


async def seed(paths: dict) -> int:
    """建隔离项目 + 一个任务，返回 task_id。"""
    import agents as agents_mod  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__gates_probe_project__", str(paths["project"]), "task gates probe"))
        pid = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority) "
            "VALUES (?,?,?,?,?)", (pid, "__gates_task__", "", "in_progress", "none"))
        task_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    return task_id


async def _finish_all(task_id: int) -> None:
    """把该任务所有 queued/running run 标为 done，绕开 enqueue_run 的 pending 去重，
    以便继续入队构造链（模拟前一个 run 已执行完、触发了下一个 @）。"""
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET status='done' WHERE task_id=?", (task_id,))
        await db.commit()
    finally:
        await db.close()


async def _run_count(task_id: int) -> int:
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        r = await (await db.execute(
            "SELECT COUNT(*) c FROM run_queue WHERE task_id=?", (task_id,))).fetchone()
        return r["c"]
    finally:
        await db.close()


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    await bootstrap_backend(paths)
    import collab  # noqa: PLC0415
    import config as config_mod  # noqa: PLC0415

    # ---- Test 1: 闸值从 Settings 生效 ----
    orig_load = config_mod.load_settings

    class _FakeSettings:
        max_concurrency = 3
        max_retry = 2
        max_runs_per_task = 50
        max_mention_chain = 4
        orphan_sweep_interval_sec = 120
        orphan_sweep_idle_sec = 1800

    config_mod.load_settings = lambda: _FakeSettings()
    try:
        collab._apply_settings()
        probe.check("MAX_RUNS_PER_TASK 从 Settings 生效",
                    collab.MAX_RUNS_PER_TASK == 50, f"={collab.MAX_RUNS_PER_TASK}（期望 50）")
        probe.check("MAX_MENTION_CHAIN 从 Settings 生效",
                    collab.MAX_MENTION_CHAIN == 4, f"={collab.MAX_MENTION_CHAIN}（期望 4）")
    finally:
        config_mod.load_settings = orig_load

    # 直接压小闸值，避免造几十条数据
    collab.MAX_MENTION_CHAIN = 4
    collab.MAX_RUNS_PER_TASK = 200

    # ---- Test 2: 循环闸 —— 连续 mention 链达上限即拒 ----
    tid = await seed(paths)
    # 造 4 条连续 mention 链式自动 run（trigger=mention + source_run_id 非空），每条造完标 done
    accepted = 0
    for i in range(4):
        rq = await collab.enqueue_run(tid, f"agent-{i}", "", "mention",
                                      is_leader=False, source_run_id=1000 + i)
        if rq:
            accepted += 1
        await _finish_all(tid)
    probe.check("循环闸内 4 条 mention 链均入队成功", accepted == 4, f"accepted={accepted}")
    # 第 5 条（链已达 4）应被循环闸拒绝
    rq5 = await collab.enqueue_run(tid, "agent-5", "", "mention",
                                   is_leader=False, source_run_id=2000)
    probe.check("循环闸：第 5 条 mention 链式入队被拒（防 @ 死循环）", rq5 is None, f"rq5={rq5}")

    # ---- Test 3: 人工/指派介入打断链，链长清零 ----
    ln_before = await collab._mention_chain_len(tid)
    # 一次 assign（人工指派，非 mention 链）——应能入队，且把链打断
    rq_assign = await collab.enqueue_run(tid, "human-assigned", "", "assign", is_leader=False)
    await _finish_all(tid)
    ln_after = await collab._mention_chain_len(tid)
    probe.check("assign 介入不受循环闸限制（人工重派可入队）", rq_assign is not None, f"rq={rq_assign}")
    probe.check("assign 介入打断 mention 链（链长清零）",
                ln_before >= 4 and ln_after == 0, f"链长 {ln_before}→{ln_after}")
    # 链清零后，mention 链式入队又能继续
    rq_after = await collab.enqueue_run(tid, "agent-again", "", "mention",
                                        is_leader=False, source_run_id=3000)
    probe.check("链清零后 mention 链式入队恢复", rq_after is not None, f"rq={rq_after}")
    await _finish_all(tid)

    # ---- Test 4: 人工直接 @（source 留空）不计入循环闸 ----
    tid2 = await seed(paths)
    # source_run_id 留空 = 人工/系统直接发起的 mention，不是 Agent 自动 @，连发多条不触发循环闸
    ok_manual = True
    for i in range(6):  # 6 > MAX_MENTION_CHAIN(4)，若误计会被拒
        rq = await collab.enqueue_run(tid2, f"m-{i}", "", "mention",
                                      is_leader=False, source_run_id=None)
        if not rq:
            ok_manual = False
        await _finish_all(tid2)
    probe.check("人工直接 @（source 留空）不计入循环闸，连发 6 条均成功",
                ok_manual, f"6 条 source 留空 mention 全部入队={ok_manual}")

    # ---- Test 5: 总量闸放大后长程可跑（>原 20）----
    collab.MAX_MENTION_CHAIN = 999  # 本测隔离循环闸，只验总量闸
    collab.MAX_RUNS_PER_TASK = 30
    tid3 = await seed(paths)
    long_ok = 0
    for i in range(30):
        rq = await collab.enqueue_run(tid3, f"lr-{i}", "", "assign", is_leader=False)
        if rq:
            long_ok += 1
        await _finish_all(tid3)
    probe.check("总量闸放大后长程任务可持续入队（30 次 > 原 20 上限）", long_ok == 30, f"入队 {long_ok}/30")
    # 第 31 条触总量闸
    rq31 = await collab.enqueue_run(tid3, "lr-over", "", "assign", is_leader=False)
    probe.check("总量闸：达 MAX_RUNS_PER_TASK 后拒绝新入队", rq31 is None, f"rq31={rq31}")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-gates-"))
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
    print(f"\n=== task gates probe: {passed}/{total} ===")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
