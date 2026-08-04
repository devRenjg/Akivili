"""Akivili 跨进程 kill 信号 probe（worker-split-minimal 组 1 · D 类）。

执行面剥离到独立 worker 进程后，队列路径的 CLI 子进程挂在 worker 名下、API 进程 _RUN_PIDS 为空。
前端「终止运行」经 /runs/kill 打到 API，API 对不在本进程的 run 落 DB 信号 task_runs.kill_requested_at；
worker 的 consume_kill_signals() 扫信号，对**本进程 _RUN_PIDS 持有**的 run 执行 kill_run + finalize。

本 probe 用真实短命子进程（sleeper）拿真 pid，验证 consume_kill_signals 的端到端语义：
  1. 信号 + 本进程持有 → sleeper 被真杀、task_run→killed、信号清除、返回 killed=1
  2. 信号 + 本进程【不】持有（模拟 run 在另一进程）→ 不动它、sleeper 存活、信号保留、返回 0
  3. 信号 + run 已终态（自然收尾遗留的陈旧信号）→ 不 kill、但陈旧信号被清除
  4. 幂等：再调一次 consume 不重复 kill

用临时隔离 config/DB，不碰真库；spawn 的 sleeper 会被清理。No CLI/LLM.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
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


def _spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


async def _seed_run(paths: dict, task_id_title: str, status: str = "running") -> tuple[int, int]:
    """建 project+task+task_run，返回 (task_id, task_run_id)。"""
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        pid = (await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            (f"__killsig_proj_{task_id_title}__", str(paths["project"]), "kill signal probe"))).lastrowid
        tid = (await db.execute(
            "INSERT INTO tasks (title, status, project_id) VALUES (?,?,?)",
            (f"__killsig_task_{task_id_title}__", "in_progress", pid))).lastrowid
        rid = (await db.execute(
            "INSERT INTO task_runs (task_id, agent_slug, status) VALUES (?,?,?)",
            (tid, "killsig-agent", status))).lastrowid
        await db.commit()
        return tid, rid
    finally:
        await db.close()


async def _signal(rid: int) -> None:
    """模拟 API /runs/kill 落跨进程 kill 信号。"""
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        await db.execute(
            "UPDATE task_runs SET kill_requested_at=datetime('now') WHERE id=?", (rid,))
        await db.commit()
    finally:
        await db.close()


async def _run_status_and_signal(rid: int) -> tuple[str, bool]:
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT status, kill_requested_at FROM task_runs WHERE id=?", (rid,))).fetchone()
        return row["status"], (row["kill_requested_at"] is not None)
    finally:
        await db.close()


async def run(paths: dict) -> Probe:
    import collab  # noqa: PLC0415
    from executor import runner  # noqa: PLC0415
    p = Probe()

    # ── 场景 1：信号 + 本进程持有 → 真杀 + killed + 清信号 ────────────────────
    proc1 = _spawn_sleeper()
    try:
        tid1, rid1 = await _seed_run(paths, "owned", "running")
        runner.register_pid(rid1, proc1.pid)         # 模拟 worker 本进程持有该 run
        await _signal(rid1)                          # API 落 kill 信号
        killed = await collab.consume_kill_signals()
        st1, sig1 = await _run_status_and_signal(rid1)
        p.check("S1 consume 返回 killed=1", killed == 1, f"killed={killed}")
        p.check("S1 sleeper 子进程被真杀", not _alive(proc1), f"alive={_alive(proc1)}")
        p.check("S1 task_run → killed", st1 == "killed", f"status={st1}")
        p.check("S1 kill 信号已清除", not sig1, f"signal_present={sig1}")
        p.check("S1 _RUN_PIDS 已清该 run", rid1 not in runner._RUN_PIDS)
    finally:
        if _alive(proc1):
            proc1.kill()

    # ── 场景 2：信号 + 本进程【不】持有 → 不动、存活、信号保留 ─────────────────
    proc2 = _spawn_sleeper()
    try:
        tid2, rid2 = await _seed_run(paths, "foreign", "running")
        # 故意不 register_pid：模拟该 run 跑在另一个进程（本 worker 不持有）
        await _signal(rid2)
        killed2 = await collab.consume_kill_signals()
        st2, sig2 = await _run_status_and_signal(rid2)
        p.check("S2 非本进程持有 → 不计入 killed", killed2 == 0, f"killed={killed2}")
        p.check("S2 sleeper 存活（未误杀他进程的 run）", _alive(proc2), f"alive={_alive(proc2)}")
        p.check("S2 task_run 仍 running", st2 == "running", f"status={st2}")
        p.check("S2 kill 信号【保留】（留给宿主进程消费）", sig2, f"signal_present={sig2}")
    finally:
        proc2.kill()

    # ── 场景 3：信号 + run 已终态（陈旧信号）→ 不 kill 但清信号 ────────────────
    tid3, rid3 = await _seed_run(paths, "stale", "succeeded")
    await _signal(rid3)                              # 给已终态 run 打信号（模拟自然收尾后遗留）
    killed3 = await collab.consume_kill_signals()
    st3, sig3 = await _run_status_and_signal(rid3)
    p.check("S3 已终态 run 不被 kill", killed3 == 0, f"killed={killed3}")
    p.check("S3 已终态 run 状态不变", st3 == "succeeded", f"status={st3}")
    p.check("S3 陈旧信号被清除", not sig3, f"signal_present={sig3}")

    # ── 场景 4：幂等——再调一次不重复 kill、无异常 ───────────────────────────
    killed4 = await collab.consume_kill_signals()
    p.check("S4 幂等：再调无新 kill", killed4 == 0, f"killed={killed4}")

    return p


async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep temporary directory")
    args = parser.parse_args()

    import os as _os
    _tmpdir = r"C:\tmp" if _os.path.isdir(r"C:\tmp") else None
    tmp = Path(tempfile.mkdtemp(prefix="akivili-killsig-", dir=_tmpdir))
    paths = setup_isolated_config(tmp)
    await bootstrap_backend(paths)
    try:
        p = await run(paths)
    finally:
        if args.keep:
            print(f"Kept temp dir: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in p.results if r[1])
    print(f"\nKill signal probe: {passed}/{len(p.results)} passed")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
