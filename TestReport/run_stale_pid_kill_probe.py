"""Akivili stale-pid-kill guard probe.

Nails down the task140 crash root cause: a stale pid left in runner._RUN_PIDS
must NEVER be killed. After a process exits, the OS recycles its pid number and
may reassign it to an innocent process (even the platform backend itself after a
restart). `kill_run` uses `taskkill /F /T` which strips the *entire* process tree,
so killing a recycled pid can take down an unrelated process tree.

The fix has two prongs, both asserted here:
  1. Identity check in kill_run — register (pid, creation_time); before killing,
     re-read the pid's creation time and refuse if the process is gone (None) or
     the creation time differs (pid was recycled).
  2. Unconditional pid cleanup — clear_pid() drops the registration; the normal
     finish path must call it OUTSIDE the fragile善后 try block so a _persist_memory
     exception can never leave a stale pid behind.

Pure unit probe: spawns real short-lived subprocesses to get genuine live/dead
pids + creation times. No DB/config/CLI/LLM. Nothing touches jianagency.db.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# runner imports config/database at module load — put backend on the path.
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from executor import runner  # noqa: E402


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


def _spawn_sleeper(seconds: int = 30) -> subprocess.Popen:
    """Spawn a real, harmless child process we fully own (a Python sleep)."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def main() -> int:
    p = Probe()

    # ── 1) register_pid captures (pid, creation_time) — structure guard ─────────
    proc1 = _spawn_sleeper()
    try:
        runner.register_pid(9001, proc1.pid)
        entry = runner._RUN_PIDS.get(9001)
        p.check("register_pid 存 (pid, 创建时间) 元组",
                isinstance(entry, tuple) and len(entry) == 2 and entry[0] == proc1.pid,
                f"entry={entry}")
        p.check("创建时间指纹已抓到（非 None）",
                entry is not None and entry[1] is not None,
                f"ctime={entry[1] if entry else None}")

        # ── 2) live matching process IS killed (normal kill still works) ────────
        killed = runner.kill_run(9001)
        time.sleep(0.8)
        p.check("身份匹配的存活进程正常被 kill", killed and not _alive(proc1),
                f"kill_run={killed}, alive={_alive(proc1)}")
        p.check("kill 后注册表已清除该 run", 9001 not in runner._RUN_PIDS)
    finally:
        if _alive(proc1):
            proc1.kill()

    # ── 3) reused pid (ctime mismatch) is REFUSED — core regression ─────────────
    #   Model pid reuse: register a live proc, then tamper the stored creation time
    #   so it differs from what the pid currently reports. kill_run must refuse and
    #   the live process must survive untouched.
    proc2 = _spawn_sleeper()
    try:
        runner.register_pid(9002, proc2.pid)
        real_pid, real_ctime = runner._RUN_PIDS[9002]
        # simulate: this pid number now belongs to a *different* process than registered
        runner._RUN_PIDS[9002] = (real_pid, (real_ctime or 0) + 12345)
        refused = runner.kill_run(9002)
        time.sleep(0.3)
        p.check("陈旧/复用 pid（创建时间不符）被拒杀", refused is False,
                f"kill_run={refused}")
        p.check("被冒名顶替的存活进程未被误杀", _alive(proc2),
                f"alive={_alive(proc2)}")
        p.check("拒杀后陈旧登记被清除", 9002 not in runner._RUN_PIDS)
    finally:
        if _alive(proc2):
            proc2.kill()

    # ── 4) dead pid (process gone → ctime None) is REFUSED ──────────────────────
    proc3 = _spawn_sleeper()
    dead_pid = proc3.pid
    proc3.kill()
    proc3.wait(timeout=5)
    time.sleep(0.3)
    # manually register the now-dead pid (as a leaked stale entry would look)
    runner._RUN_PIDS[9003] = (dead_pid, 111111)
    refused_dead = runner.kill_run(9003)
    p.check("已退出进程的 pid（创建时间取不到）被拒杀", refused_dead is False,
            f"kill_run={refused_dead}, dead_pid={dead_pid}")
    p.check("拒杀已死 pid 后登记被清除", 9003 not in runner._RUN_PIDS)

    # ── 5) clear_pid unconditionally drops registration ─────────────────────────
    proc4 = _spawn_sleeper()
    try:
        runner.register_pid(9004, proc4.pid)
        runner.clear_pid(9004)
        p.check("clear_pid 清除注册（正常收尾路径的无条件清理）",
                9004 not in runner._RUN_PIDS)
        # after clear, kill_run finds no entry → no-op False, process untouched
        no_entry = runner.kill_run(9004)
        p.check("clear_pid 后 kill_run 无目标（返回 False，不误动进程）",
                no_entry is False and _alive(proc4),
                f"kill_run={no_entry}, alive={_alive(proc4)}")
    finally:
        if _alive(proc4):
            proc4.kill()

    # ── 6) unknown run_id is a safe no-op ───────────────────────────────────────
    p.check("未知 run_id kill_run 安全空操作", runner.kill_run(999999) is False)

    total = len(p.results)
    passed = sum(1 for r in p.results if r[1])
    print(f"\n{passed}/{total} 通过")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
