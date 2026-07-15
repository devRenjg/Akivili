"""Akivili 循环闸「产出即重置」探针（task149 误伤事故回归）。

钉死改进后的循环闸判定 `_mention_chain_len`：
  - 旧口径：末尾连续 mention 链式自动 run 的**条数**达 MAX_MENTION_CHAIN 即熔断——会误伤
    多轮真实协作（复验/返工来回好几棒，每棒都有交付，却被当成死循环掐断，task149 事故）。
  - 新口径：只累计末尾连续**空转**（无产出）的 mention 链；一旦遇到某棒产出了真实交付
    （该 run 落过带 run_id 的 assistant 消息 = jian comment/subtask），链即重置。
    → 有产出的长链协作不被误掐；纯 @ 空转（无产出）仍如期累积熔断，保护不削弱。

隔离库（临时 config/DB/workspace，绝不碰真实 jianagency.db）。不触发真实 CLI/LLM。
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

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


async def _seed_task(paths: dict) -> tuple[int, int]:
    """建隔离项目 + 一个带会话的任务，返回 (task_id, conversation_id)。"""
    import agents as agents_mod  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__chain_reset_project__", str(paths["project"]), "chain reset probe"))
        pid = cur.lastrowid
        cur = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)",
                               (pid, "__chain_task__"))
        conv = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, status, priority, conversation_id) "
            "VALUES (?,?,?,?,?)", (pid, "__chain_task__", "in_progress", "none", conv))
        tid = cur.lastrowid
        await db.commit()
        return tid, conv
    finally:
        await db.close()


async def _enqueue_chain_run(task_id: int, slug: str, src_run: int) -> int:
    """入队一条 mention 链式 run（绕过闸直接插 run_queue，模拟已被接受的历史链），返回 run_queue id。"""
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO run_queue (task_id, agent_slug, trigger, is_leader, prompt, status, "
            "attempts, source_run_id) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, slug, "mention", 0, "", "done", 1, src_run))
        rqid = cur.lastrowid
        await db.commit()
        return rqid
    finally:
        await db.close()


async def _attach_task_run(rqid: int, task_id: int, conv: int, slug: str,
                           produce_msg: bool) -> int:
    """给某个 run_queue 项建一条对应 task_run，并回填 run_queue.task_run_id。
    produce_msg=True 时再落一条带 run_id 的 assistant 消息（模拟 jian comment 真实交付）。"""
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO task_runs (task_id, conversation_id, agent_slug, status) VALUES (?,?,?,?)",
            (task_id, conv, slug, "succeeded"))
        trid = cur.lastrowid
        await db.execute("UPDATE run_queue SET task_run_id=? WHERE id=?", (trid, rqid))
        if produce_msg:
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content, author_slug, run_id) "
                "VALUES (?,?,?,?,?)", (conv, "assistant", "交付：复验结论", slug, trid))
        await db.commit()
        return trid
    finally:
        await db.close()


async def run_probe(paths: dict) -> Probe:
    probe = Probe()
    await bootstrap_backend(paths)
    import collab  # noqa: PLC0415

    collab.MAX_MENTION_CHAIN = 4  # 压小闸值便于构造

    # ── 1) 有产出的长链不累积：造 6 棒 mention 链，每棒都有真实交付 → 链长应=0 ─────────
    tid, conv = await _seed_task(paths)
    for i in range(6):
        rq = await _enqueue_chain_run(tid, f"agent-{i%3}", 1000 + i)
        await _attach_task_run(rq, tid, conv, f"agent-{i%3}", produce_msg=True)
    ln_productive = await collab._mention_chain_len(tid)
    probe.check("6 棒 mention 链但每棒都有交付 → 链长重置为 0（不误伤真实协作）",
                ln_productive == 0, f"链长={ln_productive}（期望 0）")
    # 闸判定：此时链式入队应被放行（远未撞闸）
    rq_ok = await collab.enqueue_run(tid, "agent-next", "", "mention",
                                     is_leader=False, source_run_id=9001)
    probe.check("有产出协作链下，新的 mention 入队被放行（不误掐）", rq_ok is not None, f"rq={rq_ok}")

    # ── 2) 纯空转链仍如期累积熔断：造 4 棒无产出 mention 链 → 链长=4=闸值 ─────────────
    tid2, conv2 = await _seed_task(paths)
    for i in range(4):
        rq = await _enqueue_chain_run(tid2, f"spam-{i}", 2000 + i)
        await _attach_task_run(rq, tid2, conv2, f"spam-{i}", produce_msg=False)  # 无交付
    ln_empty = await collab._mention_chain_len(tid2)
    probe.check("4 棒纯空转（无产出）mention 链 → 链长累积到 4（保护不削弱）",
                ln_empty == 4, f"链长={ln_empty}（期望 4）")
    rq_block = await collab.enqueue_run(tid2, "spam-5", "", "mention",
                                        is_leader=False, source_run_id=2999)
    probe.check("纯空转链达闸值 → 新 mention 入队被拒（死循环仍如期熔断）",
                rq_block is None, f"rq={rq_block}")

    # ── 3) 混合链：中间有一棒产出，链只从最新往回数到那棒 ─────────────────────────────
    #   构造（从早到晚）：空转, 空转, 有产出, 空转, 空转 → 末尾连续空转=2，遇产出棒停 → 链长=2
    tid3, conv3 = await _seed_task(paths)
    layout = [False, False, True, False, False]  # 是否产出
    for i, prod in enumerate(layout):
        rq = await _enqueue_chain_run(tid3, f"mix-{i}", 3000 + i)
        await _attach_task_run(rq, tid3, conv3, f"mix-{i}", produce_msg=prod)
    ln_mix = await collab._mention_chain_len(tid3)
    probe.check("混合链：末尾数到最近一棒有产出即停（链长=末尾连续空转数 2）",
                ln_mix == 2, f"链长={ln_mix}（期望 2）")

    # ── 4) task_run_id 为空（run 没起来）视为无产出，计入空转 ──────────────────────────
    tid4, conv4 = await _seed_task(paths)
    rq = await _enqueue_chain_run(tid4, "norun", 4000)  # 不 attach task_run → task_run_id 保持 NULL
    ln_norun = await collab._mention_chain_len(tid4)
    probe.check("run 没起来（task_run_id 空）视为空转，计入链长",
                ln_norun == 1, f"链长={ln_norun}（期望 1）")

    return probe


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-chainreset-"))
    try:
        paths = setup_isolated_config(tmp)
        probe = await run_probe(paths)
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"[keep] 临时目录保留：{tmp}")
    total = len(probe.results)
    passed = sum(1 for r in probe.results if r[1])
    print(f"\n=== mention chain reset probe: {passed}/{total} ===")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
