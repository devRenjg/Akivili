"""S1.4 探针：验证 claude 预分配 session_id 捕获链路（agent-session-resume-minimal 阶段一）。

验证三件事，不依赖完整 execute_dispatch（不需要真 task/agent/DB 行）：
  1. ClaudeCodeBackend.run 执行前生成 UUID 并通过 on_session 回调回传（run 一启动即回传）
  2. 生成的 UUID 被注入命令行 `--session-id <uuid>`
  3. ctx.cli_session_id 非空时用它（resume 场景预留），为空时自生成新 UUID

做法：monkeypatch subprocess.Popen 拦下命令行（不真起 claude 子进程，纯验证命令构造 + 回调时序），
读取传给 Popen 的 cmd 数组断言 --session-id 存在且值 == on_session 回传值。

纯只读/离线：不碰 DB、不起真 CLI。产物落 TestReport/session_capture_probe_report.txt。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import uuid as uuidmod

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


class _FakePopen:
    """假 Popen：记录 cmd，立即"结束"，stdout 吐一行 result 事件让 run() 正常收尾。"""
    last_cmd = None

    def __init__(self, cmd, **kwargs):
        _FakePopen.last_cmd = list(cmd)
        self.returncode = 0
        self.pid = 999999
        self.stdin = io.StringIO()
        # stream-json：一行 result 让 _parse_line 产出结束事件
        self.stdout = io.StringIO('{"type":"result","subtype":"success"}\n')
        self.stderr = io.StringIO("")

    def wait(self):
        return 0


def _run_case(preassigned: str) -> dict:
    """跑一次 ClaudeCodeBackend.run，返回 {captured_session, cmd, session_id_flag_value}。"""
    from executor.base import ExecContext
    from executor import claude_code

    captured = {"session": None}

    def _on_session(sid):
        captured["session"] = sid

    ctx = ExecContext(prompt="hi", system_prompt="", project_dir=os.getcwd(),
                      cli_session_id=preassigned)

    async def _drive():
        # monkeypatch Popen + containment（避免真 contain 999999）
        orig_popen = claude_code.subprocess.Popen
        claude_code.subprocess.Popen = _FakePopen
        try:
            evs = []
            async for ev in claude_code.ClaudeCodeBackend().run(ctx, on_session=_on_session):
                evs.append(ev.type)
            return evs
        finally:
            claude_code.subprocess.Popen = orig_popen

    evs = asyncio.run(_drive())
    cmd = _FakePopen.last_cmd or []

    def _val_after(flag):
        if flag in cmd:
            i = cmd.index(flag)
            if i + 1 < len(cmd):
                return cmd[i + 1]
        return None

    # S2.4：首建走 --session-id、续跑（预分配非空）走 --resume。两者取值都拿。
    return {"captured": captured["session"], "cmd": cmd,
            "session_id_val": _val_after("--session-id"),
            "resume_val": _val_after("--resume"), "events": evs}


def main():
    print("=" * 60)
    print("S1.4 claude 预分配 session_id 捕获探针")
    print("=" * 60)

    # 用例 A：不预分配（ctx.cli_session_id 空）→ run 自生成新 UUID
    print("\n[A] 首建（不预分配）：run 自生成 UUID")
    a = _run_case(preassigned="")
    a_captured_is_uuid = _is_uuid(a["captured"])
    # 首建：命令走 --session-id，值 == 回传（无 --resume）
    a_flag_match = a["captured"] == a["session_id_val"] and a["session_id_val"] is not None
    a_no_resume = a["resume_val"] is None
    print(f"    on_session 回传 = {a['captured']}")
    print(f"    命令 --session-id 值 = {a['session_id_val']}")
    print(f"    [1] 回传值是合法 UUID: {'OK' if a_captured_is_uuid else 'FAIL'}")
    print(f"    [2] 命令 --session-id==回传、无 --resume: {'OK' if a_flag_match and a_no_resume else 'FAIL'}")

    # 用例 B：预分配非空 = resume 场景（S2.4）→ 命令走 --resume <id>，无 --session-id
    print("\n[B] 预分配非空（resume 命中，S2.4）：命令走 --resume")
    fixed = str(uuidmod.uuid4())
    b = _run_case(preassigned=fixed)
    # S2.4：预分配非空 → 命令走 --resume <id>（不再是 --session-id）；回传值仍 == 传入
    b_resume = b["resume_val"] == fixed and b["session_id_val"] is None and b["captured"] == fixed
    print(f"    传入 = {fixed}")
    print(f"    on_session 回传 = {b['captured']}")
    print(f"    命令 --resume 值 = {b['resume_val']}  (--session-id 值 = {b['session_id_val']})")
    print(f"    [3] 命令 --resume==传入、无 --session-id、回传==传入: {'OK' if b_resume else 'FAIL'}")

    all_ok = a_captured_is_uuid and a_flag_match and a_no_resume and b_resume
    print("\n" + "=" * 60)
    print(f"结论：{'✅ 全部通过 —— 预分配捕获链路正确' if all_ok else '❌ 有失败项'}")
    print("=" * 60)
    return 0 if all_ok else 1


def _is_uuid(s) -> bool:
    if not isinstance(s, str):
        return False
    try:
        uuidmod.UUID(s)
        return True
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    _report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_capture_probe_report.txt")
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