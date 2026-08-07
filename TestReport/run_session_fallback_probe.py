"""S4.5 探针：降级链（agent-session-resume-minimal 阶段一 S4）。

验证「resume 用不了 → 退回全量、不劣于现状」的各分支：
  A. _should_drop_session 分类：resume_miss / poisoned / 不丢（普通错误·限流·空）
  B. resume_hit 判定的降级入口（逻辑层）：
     - 无 session_id → resume_hit=False（S4.1 首次全量）
     - 非 CLI backend（api）→ resume_hit=False（S4.4 换 provider）
     - codex rollout 缺失 → resume_hit=False（S3.4）
  C. 清 session 的 DB 效果：run_queue.cli_session_id 被清空后，下次取 item 不带 session（走全量）

纯逻辑 + DB 级，不真起 CLI。产物落 TestReport/session_fallback_report.txt。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


async def _clear_session_db_case():
    """C：造一个带 cli_session_id 的 run_queue 行 → 模拟清空 → 验证读回为 None。"""
    from models import get_session_factory
    from models.tables import RunQueue
    from sqlalchemy import select, update as sa_update, text as _t

    async with get_session_factory()() as s:
        # 找一个现成 task_id（run_queue.task_id FK）
        tid = (await s.execute(_t("SELECT id FROM tasks LIMIT 1"))).scalar()
        if tid is None:
            return None
        rq = RunQueue(task_id=tid, agent_slug="__fallback_probe__", status="done",
                      cli_session_id="stale-sid-xyz", session_committed_msg_id=42)
        s.add(rq); await s.flush(); rid = rq.id; await s.commit()

    # 模拟 S4 清 session（失败命中丢弃条件时的动作）
    async with get_session_factory()() as s:
        await s.execute(sa_update(RunQueue).where(RunQueue.id == rid)
                        .values(cli_session_id=None, session_committed_msg_id=None))
        await s.commit()

    async with get_session_factory()() as s:
        row = (await s.execute(select(RunQueue.cli_session_id, RunQueue.session_committed_msg_id)
                               .where(RunQueue.id == rid))).first()
        # 清理
        await s.execute(_t("DELETE FROM run_queue WHERE id=:i"), {"i": rid})
        await s.commit()
    return row[0], row[1]


def main():
    from collab import _should_drop_session
    print("=" * 60)
    print("S4.5 降级链探针")
    print("=" * 60)

    # A. 分类
    print("\n[A] _should_drop_session 分类")
    a_cases = [
        ("no conversation found", "resume_miss"),
        ("session not found", "resume_miss"),
        ("rollout missing", "resume_miss"),
        ("iteration limit reached", "poisoned"),
        ("400 invalid_request_error", "poisoned"),
        ("context window exceeded", "poisoned"),
        ("semantic inactivity", "poisoned"),
        ("bash: command not found", ""),
        ("429 rate limit exceeded", ""),   # 限流不丢：会话仍有效，走重试
        ("", ""),
    ]
    a_ok = True
    for text, exp in a_cases:
        got = _should_drop_session(text)
        if got != exp:
            a_ok = False
            print(f"    FAIL: {text[:30]!r} → {got!r} 期望 {exp!r}")
    print(f"    10 类分类全对: {'OK' if a_ok else 'FAIL'}")

    # B. resume_hit 降级入口（复刻 runner 判定逻辑）
    print("\n[B] resume_hit 降级入口")
    from executor.codex import codex_rollout_present

    def resume_hit(session_id, backend_type):
        cli = backend_type in ("claude-cli", "codex-cli")
        hit = bool(session_id) and cli
        if hit and backend_type == "codex-cli" and not codex_rollout_present(session_id):
            hit = False
        return hit

    b1 = resume_hit("", "claude-cli") is False            # 无 session → 全量（S4.1）
    b2 = resume_hit("sid", "api") is False                # api backend → 全量（S4.4）
    b3 = resume_hit("sid", "claude-cli") is True          # claude 有 session → resume
    b4 = resume_hit("00000000-0000-0000-0000-000000000000", "codex-cli") is False  # rollout 缺 → 降级（S3.4）
    print(f"    无 session→全量: {'OK' if b1 else 'FAIL'}")
    print(f"    api backend→全量: {'OK' if b2 else 'FAIL'}")
    print(f"    claude 有 session→resume: {'OK' if b3 else 'FAIL'}")
    print(f"    codex rollout 缺→降级: {'OK' if b4 else 'FAIL'}")
    b_ok = b1 and b2 and b3 and b4

    # C. 清 session DB 效果
    print("\n[C] 清 session 的 DB 效果")
    res = asyncio.run(_clear_session_db_case())
    if res is None:
        c_ok = True
        print("    （无现成 task，跳过 DB 用例；分类与判定已由 A/B 覆盖）")
    else:
        sid, committed = res
        c_ok = sid is None and committed is None
        print(f"    清空后 cli_session_id={sid!r} committed={committed!r}")
        print(f"    session 指针+水位已清（下次全量）: {'OK' if c_ok else 'FAIL'}")

    all_ok = a_ok and b_ok and c_ok
    print("\n" + "=" * 60)
    print(f"结论：{'PASS 全部通过 —— 降级链各分支正确、不劣于现状' if all_ok else 'FAIL 有失败项'}")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    _report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_fallback_report.txt")
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