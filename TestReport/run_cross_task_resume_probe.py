"""S5.5 探针：跨 task 续接（agent-session-resume-minimal 阶段二 S5）。

验证「同一 (conversation, agent) 的下一个 task 复用上个 task 会话」的核心逻辑（不真起 CLI）：
  A. upsert + lookup 往返：成功收尾写 agent_sessions，下个 task 查得回同一 session + committed。
  B. 后写覆盖：同 key 再 upsert → 覆盖旧值、始终至多一行（唯一键 (conversation_id, agent_slug)）。
  C. 并发写同 key 不报错：两个 task 几乎同时收尾写同 key → ON CONFLICT DO UPDATE 不崩、
     恰一行、值为其一（best-effort，design.md 抉择二）。
  D. 两段查找顺序：run_queue 有 session → 用它（同 run，不查缓存）；run_queue 空 → 查 agent_sessions
     （跨 task）；两者皆空 → resume_session_id 保持空（全量首建）。
  E. miss 降级：(conversation, agent) 无缓存行 → lookup 返回 None → 全量首建，不劣于现状。

用真 DB（agent_sessions 无 FK，用合成 conv_id 隔离，测完清理）。产物落 cross_task_resume_report.txt。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

# 合成会话 id 区间（远离真实自增 id，测完按此清理）
_CONV_A = 990000001
_CONV_B = 990000002
_SLUG = "__cross_task_probe__"


async def _cleanup():
    from models import get_session_factory
    from sqlalchemy import text as _t
    async with get_session_factory()() as s:
        await s.execute(_t("DELETE FROM agent_sessions WHERE agent_slug = :slug"), {"slug": _SLUG})
        await s.commit()


async def _row_count(conv_id: int) -> int:
    from models import get_session_factory
    from sqlalchemy import text as _t
    async with get_session_factory()() as s:
        n = (await s.execute(
            _t("SELECT count(*) FROM agent_sessions WHERE conversation_id=:c AND agent_slug=:s"),
            {"c": conv_id, "s": _SLUG})).scalar()
    return int(n or 0)


async def _run():
    from executor.runner import _lookup_agent_session, _upsert_agent_session
    results = {}

    await _cleanup()

    # ── A. upsert + lookup 往返 ───────────────────────────────
    await _upsert_agent_session(_CONV_A, _SLUG, "sid-task1", 10, "p-1", "claude", "/wd")
    got = await _lookup_agent_session(_CONV_A, _SLUG)
    results["A_roundtrip"] = (
        got is not None and got["cli_session_id"] == "sid-task1"
        and got["session_committed_msg_id"] == 10)

    # ── B. 后写覆盖 + 至多一行 ─────────────────────────────────
    await _upsert_agent_session(_CONV_A, _SLUG, "sid-task2", 25, "p-1", "claude", "/wd")
    got2 = await _lookup_agent_session(_CONV_A, _SLUG)
    cnt = await _row_count(_CONV_A)
    results["B_overwrite"] = (
        got2 is not None and got2["cli_session_id"] == "sid-task2"
        and got2["session_committed_msg_id"] == 25 and cnt == 1)

    # ── C. 并发写同 key 不报错、恰一行、值为其一 ─────────────────
    crashed = False
    try:
        await asyncio.gather(
            _upsert_agent_session(_CONV_B, _SLUG, "sid-cc-X", 100, "p-1", "codex", "/wd"),
            _upsert_agent_session(_CONV_B, _SLUG, "sid-cc-Y", 200, "p-1", "codex", "/wd"),
        )
    except Exception:  # noqa: BLE001
        crashed = True
    got3 = await _lookup_agent_session(_CONV_B, _SLUG)
    cnt3 = await _row_count(_CONV_B)
    results["C_concurrent"] = (
        not crashed and cnt3 == 1 and got3 is not None
        and got3["cli_session_id"] in ("sid-cc-X", "sid-cc-Y"))

    # ── D. 两段查找顺序（复刻 runner S5.3 判定）─────────────────
    #   run_queue 有 session → 用它（同 run，不查缓存）；空 → 查 agent_sessions（跨 task）。
    async def _resolve(rq_session: str, conv_id: int, cli_backend: bool):
        resume_sid = rq_session
        committed = 0
        source = "run" if resume_sid else ""
        if not resume_sid and cli_backend:
            cached = await _lookup_agent_session(conv_id, _SLUG)
            if cached and cached.get("cli_session_id"):
                resume_sid = cached["cli_session_id"]
                committed = cached.get("session_committed_msg_id") or 0
                source = "cross_task"
        return resume_sid, committed, source

    # D1: run_queue 有 session（同 run）→ 用 run_queue，不被缓存覆盖
    d1_sid, d1_c, d1_src = await _resolve("rq-sid", _CONV_A, True)
    d1 = d1_sid == "rq-sid" and d1_src == "run"
    # D2: run_queue 空 + 缓存有（跨 task）→ 用缓存的 sid-task2 + committed 25
    d2_sid, d2_c, d2_src = await _resolve("", _CONV_A, True)
    d2 = d2_sid == "sid-task2" and d2_c == 25 and d2_src == "cross_task"
    # D3: 非 CLI backend（api）→ 不查缓存，全量首建
    d3_sid, _, d3_src = await _resolve("", _CONV_A, False)
    d3 = d3_sid == "" and d3_src == ""
    results["D_lookup_order"] = d1 and d2 and d3

    # ── E. miss 降级：无缓存行 → None → 全量首建 ─────────────────
    miss = await _lookup_agent_session(999999999, _SLUG)  # 从未写过
    e_sid, _, e_src = await _resolve("", 999999999, True)
    results["E_miss_fallback"] = miss is None and e_sid == "" and e_src == ""

    await _cleanup()
    return results


def main():
    print("=" * 60)
    print("S5.5 跨 task 续接探针（阶段二）")
    print("=" * 60)
    results = asyncio.run(_run())

    labels = {
        "A_roundtrip": "A. upsert 写入 + lookup 读回同一 session+committed",
        "B_overwrite": "B. 同 key 后写覆盖、始终至多一行（唯一键）",
        "C_concurrent": "C. 并发写同 key 不报错、恰一行、值为其一",
        "D_lookup_order": "D. 两段查找：run_queue 优先→缓存→全量（api 不查缓存）",
        "E_miss_fallback": "E. 无缓存 → lookup=None → 全量首建（不劣于现状）",
    }
    all_ok = True
    for key, label in labels.items():
        ok = results.get(key, False)
        all_ok = all_ok and ok
        print(f"    [{'OK' if ok else 'FAIL'}] {label}")

    print("\n" + "=" * 60)
    print(f"结论：{'PASS 全部通过 —— 跨 task 续接 + best-effort 缓存正确' if all_ok else 'FAIL 有失败项'}")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    _report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cross_task_resume_report.txt")
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
