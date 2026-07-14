"""Akivili CLI 双管道死锁探针（run#243 事故回归）。

钉死 `_StderrDrainer`：CLI 子进程同时往 stdout+stderr 写时，stderr 必须被并发抽干，
否则 stderr 管道缓冲写满会把子进程憋死、拖垮 stdout 读取，主线程死等到超时被误杀。

用真实子进程验证（不调 CLI/LLM/不碰 DB）：
  1. 子进程先往 stderr 狂写（远超管道缓冲 4-8KB），再往 stdout 吐若干行 —— 用 drainer 后
     stdout 能完整读到、stderr 被完整抽干、进程正常退出，全程不挂起。
  2. 对照：不并发抽干（先读 stdout 到完，再 read stderr）在同样负载下会死锁 —— 用超时证明。
  3. drainer.result() 拿到完整 stderr 文本；子进程 returncode 正确。
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from executor.base import _StderrDrainer  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


# 子进程脚本：先往 stderr 狂写 STDERR_KB KB（撑爆管道缓冲），再往 stdout 吐 N 行。
# 若读取方不并发抽 stderr，子进程会阻塞在写 stderr、永远发不出 stdout → 死锁。
CHILD = r"""
import sys
# 1) 先往 stderr 狂写，远超 Windows 管道缓冲(4-8KB)
blob = "E" * 1024
for _ in range(200):          # ~200KB stderr
    sys.stderr.write(blob)
sys.stderr.flush()
# 2) 再往 stdout 吐 5 行（读取方必须此刻还能收到）
for i in range(5):
    sys.stdout.write(f"line{i}\n")
    sys.stdout.flush()
"""


def _spawn():
    return subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )


def _read_with_drainer(box: dict) -> None:
    """修复后的正确姿势：stderr 并发抽干，主线程读 stdout。"""
    proc = _spawn()
    drainer = _StderrDrainer(proc.stderr)
    lines = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            lines.append(line)
    proc.wait()
    box["lines"] = lines
    box["err_len"] = len(drainer.result())
    box["rc"] = proc.returncode


def _read_old_way(box: dict) -> None:
    """旧的死锁姿势：读完 stdout 才 read stderr。大 stderr 负载下会卡死。"""
    proc = _spawn()
    lines = []
    for line in proc.stdout:      # <- 会在这里死锁：子进程卡在写 stderr、不吐 stdout
        line = line.strip()
        if line:
            lines.append(line)
    proc.stderr.read()
    proc.wait()
    box["lines"] = lines
    try:
        proc.kill()
    except Exception:
        pass


def _run_with_timeout(fn, timeout: float) -> tuple[bool, dict]:
    """在线程里跑 fn，返回 (是否在超时内完成, 结果box)。"""
    box: dict = {}
    t = threading.Thread(target=fn, args=(box,), daemon=True)
    t.start()
    t.join(timeout=timeout)
    return (not t.is_alive()), box


def main() -> int:
    p = Probe()

    # ── 1) 修复后：drainer 并发抽干，全程不挂起，stdout 完整、stderr 抽干 ──────────
    done, box = _run_with_timeout(_read_with_drainer, timeout=15.0)
    p.check("并发抽干 stderr：读取在超时内完成（不死锁）", done, f"done={done}")
    p.check("stdout 5 行完整读到", box.get("lines") == [f"line{i}" for i in range(5)],
            f"lines={box.get('lines')}")
    p.check("stderr 被完整抽干（~200KB）", (box.get("err_len") or 0) >= 200 * 1024,
            f"err_len={box.get('err_len')}")
    p.check("子进程正常退出 rc=0", box.get("rc") == 0, f"rc={box.get('rc')}")

    # ── 2) 对照：旧姿势在同负载下死锁（超时未完成即证明）──────────────────────────
    done_old, box_old = _run_with_timeout(_read_old_way, timeout=6.0)
    p.check("对照组：旧「读完stdout才读stderr」姿势在大 stderr 下死锁（超时未完成）",
            done_old is False, f"old_done={done_old}（False=如期死锁，证明 bug 真实存在）")

    total = len(p.results)
    passed = sum(1 for r in p.results if r[1])
    print(f"\n{passed}/{total} 通过")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
