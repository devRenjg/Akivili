"""Akivili 进程树 containment probe（worker-split-minimal 组 2）。

验证：worker 进程被**强杀**（taskkill /F，模拟崩溃/断电——kill_run 等清理代码根本跑不到）时，
它派生并 contain 的 CLI 子进程被 OS **连带清理**。这是「死 worker 不会再有子进程写库/写文件」的
物理保证（对标 Multica daemon 的 Job Object/cgroup containment）。

做法：
  1. 起一个「迷你 worker」子进程（_fake_worker）：init_containment() → 起一个 sleeper 子进程
     → contain(sleeper.pid) → 打印 sleeper PID → 空转。
  2. 主 probe 读到 sleeper PID，确认它活着。
  3. 强杀迷你 worker（taskkill /F /PID，不带 /T——故意只杀 worker 本身，验证 OS 靠 Job 自动清
     子进程，而非靠 taskkill 递归杀树）。
  4. 断言 sleeper 在约定时间内退出（Job KILL_ON_JOB_CLOSE 生效）。

不碰真实 DB/CLI/LLM，纯进程行为验证。仅 Windows 有 Job Object；非 Windows 跳过（PASS 记跳过）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))


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


# 迷你 worker 源码：init_containment → 起 sleeper → contain → 打印 pid → 空转。
# 作为独立进程运行（-c），以便主 probe 能强杀它、观察 sleeper 是否被连带清理。
_FAKE_WORKER = r"""
import sys, subprocess, time
sys.path.insert(0, r"{backend}")
from executor.containment import init_containment, contain
init_containment()
sleeper = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
ok = contain(sleeper.pid)
print("SLEEPER_PID=%d CONTAINED=%s" % (sleeper.pid, ok), flush=True)
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    """Windows: pid 是否存在。用 ctypes OpenProcess（env/PATH 无关，稳定）——不用 tasklist：
    门禁 runner 传自定义 env，tasklist 可能因缺 SystemRoot/PATH 而无法启动、stdout=None（曾致
    本探针在门禁内崩 TypeError）。OpenProcess 直接问内核，不依赖任何外部命令与环境变量。"""
    import ctypes  # noqa: PLC0415
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False   # 打不开 = 进程已不存在（或无权，但本探针的 sleeper 是自己起的，必有权）
    try:
        # 进程可能已退出但句柄仍可开（僵尸）：查退出码，STILL_ACTIVE(259) 才算活着。
        code = ctypes.c_ulong(0)
        if k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
            return code.value == 259   # STILL_ACTIVE
        return True
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def run() -> Probe:
    p = Probe()
    if os.name != "nt":
        p.check("非 Windows：Job Object containment 跳过（POSIX 走进程组）", True, "skipped")
        return p

    worker = subprocess.Popen(
        [sys.executable, "-c", _FAKE_WORKER.format(backend=str(BACKEND))],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    # 读到 sleeper pid（迷你 worker 的首行输出）
    sleeper_pid = None
    contained = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = worker.stdout.readline()
        if not line:
            if worker.poll() is not None:
                break
            continue
        if "SLEEPER_PID=" in line:
            for tok in line.split():
                if tok.startswith("SLEEPER_PID="):
                    sleeper_pid = int(tok.split("=")[1])
                elif tok.startswith("CONTAINED="):
                    contained = tok.split("=")[1].strip() == "True"
            break

    p.check("迷你 worker 起 sleeper 并 contain 成功", sleeper_pid is not None and contained,
            f"sleeper_pid={sleeper_pid} contained={contained}")
    if sleeper_pid is None:
        try:
            worker.kill()
        except Exception:  # noqa: BLE001
            pass
        return p

    time.sleep(0.5)
    p.check("强杀前 sleeper 存活", _alive(sleeper_pid), f"pid={sleeper_pid}")

    # 强杀迷你 worker 本身（不带 /T：故意不递归杀树，验证 OS 靠 Job 自动清子进程）
    subprocess.run(["taskkill", "/F", "/PID", str(worker.pid)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等 Job KILL_ON_JOB_CLOSE 生效（worker 句柄关闭 → OS 终止 Job 内进程）
    gone = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _alive(sleeper_pid):
            gone = True
            break
        time.sleep(0.3)

    p.check("强杀 worker（不带 /T）后 sleeper 被 OS 连带清理（Job KILL_ON_JOB_CLOSE 生效）",
            gone, f"sleeper pid={sleeper_pid} {'已退出' if gone else '仍存活(containment 失效!)'}")
    if not gone:
        subprocess.run(["taskkill", "/F", "/PID", str(sleeper_pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


def main() -> int:
    p = run()
    passed = sum(1 for r in p.results if r[1])
    print(f"\nContainment probe: {passed}/{len(p.results)} passed")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
