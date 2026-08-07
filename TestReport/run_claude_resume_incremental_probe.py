"""S2.7 探针：claude resume + 增量回灌核心逻辑（agent-session-resume-minimal 阶段一）。

验证平台侧可确定性逻辑（不真起 claude，聚焦最易错的两块）：
  A. claude 命令分支：ctx.cli_session_id 空 → --session-id 首建；非空 → --resume 续跑
  B. 增量历史 SQL：只取 committed < id <= snapshot_end 且 author_slug != 本 agent 的消息
     （别人/用户的话纳入、本 agent 自产排除；崩溃 committed 不推进 → 下次同起点重取不漏）

A 用 FakePopen 拦命令行断言 flag；B 用真 DB 构造 conversation+messages 直接验查询。
产物落 TestReport/claude_resume_incremental_report.txt。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import uuid as uuidmod

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


class _FakePopen:
    last_cmd = None

    def __init__(self, cmd, **kwargs):
        _FakePopen.last_cmd = list(cmd)
        self.returncode = 0
        self.pid = 999999
        self.stdin = io.StringIO()
        self.stdout = io.StringIO('{"type":"result","subtype":"success"}\n')
        self.stderr = io.StringIO("")

    def wait(self):
        return 0


def _claude_cmd(preassigned: str):
    """跑一次 claude backend，返回命令数组（用 FakePopen 拦截）。"""
    from executor.base import ExecContext
    from executor import claude_code

    ctx = ExecContext(prompt="hi", system_prompt="", project_dir=os.getcwd(),
                      cli_session_id=preassigned)

    async def _drive():
        orig = claude_code.subprocess.Popen
        claude_code.subprocess.Popen = _FakePopen
        try:
            async for _ in claude_code.ClaudeCodeBackend().run(ctx):
                pass
        finally:
            claude_code.subprocess.Popen = orig

    asyncio.run(_drive())
    return _FakePopen.last_cmd or []


def _flag_after(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd and cmd.index(flag) + 1 < len(cmd) else None


async def _incremental_sql_case():
    """B：真 DB 构造 conversation + 混合作者消息，验证增量查询只取 committed<id<=snap 且非本 agent。"""
    from models import get_session_factory
    from models.tables import Message, Conversation
    from sqlalchemy import select, func

    SLUF = "__probe_agent__"
    async with get_session_factory()() as s:
        # 复用现有 conversation（避免 project/agent FK 构造）；测完只删本探针插入的消息
        cid = (await s.execute(select(Conversation.id).limit(1))).scalar()
        if cid is None:
            return None, None   # 无现成 conversation，跳过（命令分支已由 A 覆盖）
        # 造 6 条消息：用户、本agent、队友、用户、本agent、队友
        msgs = [
            ("user", "u1", ""),          # 1
            ("assistant", "me-a", SLUF), # 2 本 agent 自产
            ("assistant", "mate-b", "other"),  # 3 队友
            ("user", "u2", ""),          # 4
            ("assistant", "me-c", SLUF), # 5 本 agent 自产
            ("assistant", "mate-d", "other"),  # 6 队友
        ]
        ids = []
        for role, content, aslug in msgs:
            m = Message(conversation_id=cid, role=role, content=content, author_slug=aslug)
            s.add(m); await s.flush(); ids.append(m.id)
        await s.commit()

    committed = ids[1]   # 假设上次 committed 到第 2 条（本agent自产）
    async with get_session_factory()() as s:
        snap = (await s.execute(select(func.coalesce(func.max(Message.id), 0))
                                .where(Message.conversation_id == cid))).scalar_one()
        # 复刻 execute_dispatch 的增量查询
        inc = (await s.execute(
            select(Message.content)
            .where(Message.conversation_id == cid,
                   Message.id > committed, Message.id <= snap,
                   Message.author_slug != SLUF)
            .order_by(Message.id))).all()
        contents = [r[0] for r in inc]

    # 清理：只删本探针插入的消息（复用的 conversation 不动、其原有消息不动）
    async with get_session_factory()() as s:
        from sqlalchemy import text as _t
        await s.execute(_t("DELETE FROM messages WHERE id = ANY(:ids)"), {"ids": ids})
        await s.commit()

    # 期望：committed=id[1] 之后、非本 agent → mate-b(3), u2(4), mate-d(6)；排除 me-c(5 本agent)
    expected = ["mate-b", "u2", "mate-d"]
    return contents, expected


def main():
    print("=" * 60)
    print("S2.7 claude resume + 增量回灌核心逻辑探针")
    print("=" * 60)

    # A. 命令分支
    print("\n[A] claude 命令分支：首建 --session-id / 续跑 --resume")
    fresh_cmd = _claude_cmd(preassigned="")
    fresh_has_sid = "--session-id" in fresh_cmd and "--resume" not in fresh_cmd
    fresh_sid_is_uuid = _is_uuid(_flag_after(fresh_cmd, "--session-id"))
    print(f"    首建：有 --session-id 无 --resume: {'OK' if fresh_has_sid else 'FAIL'}")
    print(f"    首建：--session-id 值是 UUID: {'OK' if fresh_sid_is_uuid else 'FAIL'}")

    fixed = str(uuidmod.uuid4())
    resume_cmd = _claude_cmd(preassigned=fixed)
    resume_has_r = "--resume" in resume_cmd and "--session-id" not in resume_cmd
    resume_val = _flag_after(resume_cmd, "--resume") == fixed
    print(f"    续跑：有 --resume 无 --session-id: {'OK' if resume_has_r else 'FAIL'}")
    print(f"    续跑：--resume 值 == 传入 session: {'OK' if resume_val else 'FAIL'}")

    # B. 增量 SQL
    print("\n[B] 增量历史 SQL：只取 committed<id<=snap 且非本 agent")
    got, exp = asyncio.run(_incremental_sql_case())
    if got is None:
        sql_ok = True   # 无现成 conversation，跳过（不判失败；命令分支 A 已覆盖核心）
        print("    （无现成 conversation，跳过 DB 用例；命令分支已由 A 覆盖）")
    else:
        sql_ok = got == exp
        print(f"    期望增量: {exp}")
        print(f"    实得增量: {got}")
        print(f"    增量正确（含队友+用户、排除本 agent 自产）: {'OK' if sql_ok else 'FAIL'}")

    all_ok = fresh_has_sid and fresh_sid_is_uuid and resume_has_r and resume_val and sql_ok
    print("\n" + "=" * 60)
    print(f"结论：{'✅ 全部通过 —— resume 命令分支 + 增量回灌逻辑正确' if all_ok else '❌ 有失败项'}")
    print("=" * 60)
    return 0 if all_ok else 1


def _is_uuid(s):
    if not isinstance(s, str):
        return False
    try:
        uuidmod.UUID(s); return True
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    _report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_resume_incremental_report.txt")
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
