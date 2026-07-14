"""Akivili 运行期孤儿泄漏探针（run#183/#185 泄漏事故回归）。

钉死「直接 @ 对话路径无超时兜底 → task_runs 卡 running 成孤儿」的两道防线：

  Prong 1（进程内兜底）：execute_dispatch 生成器被中断（客户端断连 aclose / 任务取消）时，
    在 yield 点抛 GeneratorExit/CancelledError，必须补落终态再传播，绝不留 running 孤儿。
  Prong 2（运行期巡检）：sweep_orphan_task_runs 周期扫 task_runs 里卡 running 且最后日志静默
    超阈值的孤儿，主动补落终态（任务已收尾→succeeded，否则 killed），不必等重启 reclaim。
  幂等基石：_finalize_if_running 只在仍 running 时落库，绝不覆盖已定终态。

隔离库 + 假执行器（不调真实 CLI/LLM/不碰真实 jianagency.db）。测完清理临时目录（--keep 保留）。
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
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


_PROJECT_ID = {"id": None}


async def _ensure_project() -> int:
    from database import get_connection
    if _PROJECT_ID["id"] is not None:
        return _PROJECT_ID["id"]
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__orphan_probe_project__", ".", "orphan probe"))
        pid = cur.lastrowid
        await db.commit()
        _PROJECT_ID["id"] = pid
        return pid
    finally:
        await db.close()


async def _mk_task(title: str, status: str = "in_progress") -> tuple[int, int]:
    """建一个带会话的任务，返回 (task_id, conversation_id)。"""
    from database import get_connection
    pid = await _ensure_project()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, title))
        conv_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO tasks (title, status, conversation_id, project_id) VALUES (?,?,?,?)",
            (title, status, conv_id, pid))
        tid = cur.lastrowid
        await db.commit()
        return tid, conv_id
    finally:
        await db.close()


async def _mk_run(task_id: int, conv_id: int, status: str = "running") -> int:
    from database import get_connection
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) "
            "VALUES (?,?,?,?,?)", (task_id, conv_id, "probe-agent", "", status))
        rid = cur.lastrowid
        await db.commit()
        return rid
    finally:
        await db.close()


async def _add_log_at(run_id: int, secs_ago: int) -> None:
    """给 run 插一条 run_logs，ts 设为「距今 secs_ago 秒」（模拟静默时长）。"""
    from database import get_connection
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO run_logs (run_id, ts, channel, content) "
            "VALUES (?, datetime('now', ?), 'stdout', 'probe')",
            (run_id, f"-{secs_ago} seconds"))
        await db.commit()
    finally:
        await db.close()


async def _run_status(run_id: int) -> str:
    from database import get_connection
    db = await get_connection()
    try:
        r = await (await db.execute("SELECT status FROM task_runs WHERE id=?", (run_id,))).fetchone()
        return r["status"] if r else "<none>"
    finally:
        await db.close()


async def scenario(p: Probe) -> None:
    from executor import runner

    # ── A) _finalize_if_running 幂等基石 ────────────────────────────────────────
    tid, conv = await _mk_task("orphan-A", "in_progress")
    r_run = await _mk_run(tid, conv, "running")
    ok1 = await runner._finalize_if_running(r_run, "killed")
    p.check("running 的 run 被 _finalize_if_running 落终态", ok1 and await _run_status(r_run) == "killed")
    # 再次调用：已 killed，不应再改（幂等，返回 False）
    ok2 = await runner._finalize_if_running(r_run, "succeeded")
    p.check("已定终态的 run 不被覆盖（幂等 no-op）",
            ok2 is False and await _run_status(r_run) == "killed")

    r_done = await _mk_run(tid, conv, "succeeded")
    ok3 = await runner._finalize_if_running(r_done, "killed")
    p.check("succeeded 的 run 绝不被改成 killed",
            ok3 is False and await _run_status(r_done) == "succeeded")

    # ── B) 生成器中断兜底（Prong 1）：aclose 中途 → 补落终态，不留 running ──────────
    #   用真实 execute_dispatch，但 monkeypatch backend 层为一个「永远 yield、不结束」的假流，
    #   模拟 Agent 正在产出时客户端断连 aclose。
    from executor import base as _base

    class _HangingBackend:
        async def run(self, ctx, on_pid=None):
            # 先吐一个事件，再无限挂起（等外部 aclose 抛 GeneratorExit）
            yield _base.ExecEvent("text", "working...")
            while True:
                await asyncio.sleep(0.05)
                yield _base.ExecEvent("text", "still working...")

    orig_pick = runner._pick_backend
    runner._pick_backend = lambda provider: _HangingBackend()
    try:
        tid_b, conv_b = await _mk_task("orphan-B", "in_progress")
        # execute_dispatch 需要 task dict 带 project_dir / conversation_id
        task = {"id": tid_b, "conversation_id": conv_b, "project_id": 1,
                "project_dir": ".", "title": "orphan-B"}
        agent = {"slug": "probe-agent", "persona": "", "provider_id_effective": "",
                 "name": "探针"}
        agen = runner.execute_dispatch(task, agent, "go", persist_user_msg=False)
        captured = {"run_id": None}
        # 消费前几个事件（拿到 run_id），模拟流式进行中
        n = 0
        async for ev in agen:
            if ev.type == "system" and ev.meta.get("run_id"):
                captured["run_id"] = ev.meta["run_id"]
            n += 1
            if n >= 3:
                break
        rid_b = captured["run_id"]
        mid_status = await _run_status(rid_b) if rid_b else "<none>"
        p.check("中断前 run 处于 running", rid_b is not None and mid_status == "running",
                f"run={rid_b} status={mid_status}")
        # 客户端断连：aclose 生成器 → 触发 GeneratorExit 兜底
        await agen.aclose()
        after = await _run_status(rid_b)
        p.check("生成器 aclose 后 run 被补落终态（不留 running 孤儿）",
                after == "killed", f"status={after}")
        p.check("中断兜底后 pid 注册表已清", rid_b not in runner._RUN_PIDS)
    finally:
        runner._pick_backend = orig_pick

    # ── C) 运行期巡检（Prong 2）：静默超阈值的孤儿被回收，新鲜的不动 ────────────────
    import collab

    # C1: 未收尾任务 + 静默很久 → 判 killed
    tid_c1, conv_c1 = await _mk_task("orphan-C1", "in_progress")
    r_stale = await _mk_run(tid_c1, conv_c1, "running")
    await _add_log_at(r_stale, 4000)   # 最后日志 4000 秒前（> 默认 1800 阈值）

    # C2: 已收尾任务（done）+ 静默很久 → 判 succeeded（其成果 run）
    tid_c2, conv_c2 = await _mk_task("orphan-C2", "done")
    r_stale_done = await _mk_run(tid_c2, conv_c2, "running")
    await _add_log_at(r_stale_done, 4000)

    # C3: 在跑的 run（刚写过日志）→ 不该被误杀
    tid_c3, conv_c3 = await _mk_task("orphan-C3", "in_progress")
    r_fresh = await _mk_run(tid_c3, conv_c3, "running")
    await _add_log_at(r_fresh, 5)      # 5 秒前刚有日志

    n_swept = await collab.sweep_orphan_task_runs(idle_sec=1800)
    p.check("巡检回收了静默超阈值的孤儿（2 条）", n_swept == 2, f"swept={n_swept}")
    p.check("未收尾任务的孤儿落 killed", await _run_status(r_stale) == "killed")
    p.check("已收尾任务的孤儿落 succeeded（保成果）",
            await _run_status(r_stale_done) == "succeeded")
    p.check("在跑的新鲜 run 不被误杀（仍 running）", await _run_status(r_fresh) == "running")

    # C4: 再扫一遍幂等——新鲜 run 仍在跑不动，已回收的不重复处理
    n_again = await collab.sweep_orphan_task_runs(idle_sec=1800)
    p.check("巡检幂等：已回收的不重复处理，新鲜的仍不动", n_again == 0, f"swept={n_again}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili-orphan-"))
    p = Probe()
    try:
        paths = setup_isolated_config(tmp)
        await bootstrap_backend(paths)
        await scenario(p)
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"[keep] 临时目录保留：{tmp}")

    total = len(p.results)
    passed = sum(1 for r in p.results if r[1])
    print(f"\n{passed}/{total} 通过")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
