"""S3.5 探针：codex resume + thread_id 抓取 + rollout 校验（agent-session-resume-minimal 阶段一）。

验证平台侧可确定性逻辑（不真起 codex）：
  A. codex 命令分支：ctx.cli_session_id 空 → `exec --json ... --cd <dir> -`（首建）；
     非空 → `exec resume <flags> <thread_id> -`（续跑，**不含 --cd**，OPTIONS 在 id 前）
  B. thread_id 抓取：_extract_thread_id 从 thread.started 事件取 thread_id、经 on_session 回传
  C. rollout 校验：codex_rollout_present 真实 id 命中、不存在 id / 空 → False（降级依据）

A 用 FakePopen 拦命令行；B/C 直接单元测函数。产物落 TestReport/codex_resume_incremental_report.txt。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


class _FakePopen:
    last_cmd = None

    def __init__(self, cmd, **kwargs):
        _FakePopen.last_cmd = list(cmd)
        self.returncode = 0
        self.pid = 999999
        self.stdin = io.StringIO()
        # codex thread.started + item.completed：让 _reader 抓 thread_id + 正常收尾
        self.stdout = io.StringIO(
            '{"type":"thread.started","thread_id":"probe-thread-xyz"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n')
        self.stderr = io.StringIO("")

    def wait(self):
        return 0


def _codex_run(preassigned: str):
    """跑一次 codex backend（FakePopen 拦命令），返回 (命令数组, on_session 回传值)。"""
    from executor.base import ExecContext
    from executor import codex

    ctx = ExecContext(prompt="hi", system_prompt="", project_dir=os.getcwd(),
                      cli_session_id=preassigned)
    captured = {"sid": None}

    def _on_session(sid):
        captured["sid"] = sid

    async def _drive():
        orig = codex.subprocess.Popen
        codex.subprocess.Popen = _FakePopen
        try:
            async for _ in codex.CodexBackend().run(ctx, on_session=_on_session):
                pass
        finally:
            codex.subprocess.Popen = orig

    asyncio.run(_drive())
    return _FakePopen.last_cmd or [], captured["sid"]


def main():
    from executor.codex import codex_rollout_present, _extract_thread_id
    print("=" * 60)
    print("S3.5 codex resume + thread_id 抓取 + rollout 校验探针")
    print("=" * 60)

    # A. 命令分支
    print("\n[A] codex 命令分支：首建 exec --json / 续跑 exec resume")
    fresh_cmd, fresh_sid = _codex_run(preassigned="")
    fresh_ok = ("exec" in fresh_cmd and "resume" not in fresh_cmd
                and "--cd" in fresh_cmd and "--json" in fresh_cmd)
    print(f"    首建命令: {' '.join(fresh_cmd)}")
    print(f"    首建：exec --json 含 --cd、无 resume: {'OK' if fresh_ok else 'FAIL'}")
    # 首建时 thread_id 从 thread.started 抓到并回传
    print(f"    首建：on_session 回传抓到的 thread_id: {fresh_sid}  {'OK' if fresh_sid=='probe-thread-xyz' else 'FAIL'}")

    resume_cmd, _ = _codex_run(preassigned="thr-abc-999")
    # 续跑：exec resume <id> -，不含 --cd，id 在 resume 之后、- 之前
    has_resume = "resume" in resume_cmd
    no_cd = "--cd" not in resume_cmd
    id_present = "thr-abc-999" in resume_cmd
    id_after_resume = has_resume and id_present and resume_cmd.index("thr-abc-999") > resume_cmd.index("resume")
    resume_ok = has_resume and no_cd and id_after_resume
    print(f"    续跑命令: {' '.join(resume_cmd)}")
    print(f"    续跑：exec resume 含 id、无 --cd、id 在 resume 后: {'OK' if resume_ok else 'FAIL'}")

    # B. thread_id 抓取
    print("\n[B] thread_id 抓取")
    b1 = _extract_thread_id('{"type":"thread.started","thread_id":"t-123"}') == "t-123"
    b2 = _extract_thread_id('{"type":"item.completed","item":{}}') is None
    b3 = _extract_thread_id("not json") is None
    print(f"    thread.started 取到 / 非该事件 None / 非 JSON None: {'OK' if b1 and b2 and b3 else 'FAIL'}")

    # C. rollout 校验
    print("\n[C] rollout 在位校验（降级依据）")
    import glob as _g, re as _re
    home = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
    files = _g.glob(os.path.join(home, "**", "rollout-*.jsonl"), recursive=True)
    real_hit = True
    if files:
        m = _re.search(r"([0-9a-f-]{36})\.jsonl$", files[0])
        if m:
            real_hit = codex_rollout_present(m.group(1))
        print(f"    真实 rollout id 命中: {'OK' if real_hit else 'FAIL'}")
    else:
        print("    （本机无 codex rollout，跳过真实命中用例）")
    miss = not codex_rollout_present("00000000-0000-0000-0000-000000000000")
    empty = not codex_rollout_present("")
    print(f"    不存在 id / 空 id → False（降级）: {'OK' if miss and empty else 'FAIL'}")

    all_ok = (fresh_ok and fresh_sid == "probe-thread-xyz" and resume_ok
              and b1 and b2 and b3 and real_hit and miss and empty)
    print("\n" + "=" * 60)
    print(f"结论：{'✅ 全部通过 —— codex resume 命令分支 + 抓取 + rollout 校验正确' if all_ok else '❌ 有失败项'}")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    _report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_resume_incremental_report.txt")
    _f = io.open(_report, "w", encoding="utf-8")
    _orig = sys.stdout
    sys.stdout = _f
    try:
        rc = main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=_f)
        rc = 3
    finally:
        sys.stdout = _orig
        _f.close()
    print(f"report -> {_report} (rc={rc})")
    sys.exit(rc)