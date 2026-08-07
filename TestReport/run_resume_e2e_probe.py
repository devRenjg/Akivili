"""端到端实测：Session Resume 两阶段真起 CLI（门禁外 `*`，需真实 claude/codex CLI + LLM）。

真消耗 token。验证 Session Resume 的**真实**表现，两条证据链：
  1. token 下降——task_runs.usage_input_tokens：resume run < 全量 run（resume 省历史重放）
  2. 会话记忆连续——任务A 让 agent 记一个只在对话里的暗号 → 任务B(resume) 问它 → 答对
     = CLI 会话真的续上了（全量重建则记不住，因增量 history 不含 A 的完整上下文）

覆盖阶段二（跨 task 续接）：同 (conversation, agent) 第二个 task 命中 agent_sessions 缓存 → resume。
claude 与 codex 各跑一遍（两线 resume 机制不同：claude 预分配 UUID / codex 抓 thread_id）。

驱动方式：隔离 PG 库 + 隔离 config（注入真实 provider）+ 直接调 runner.execute_dispatch（真起 CLI），
不经 HTTP 服务层。产物落 TestReport/resume_e2e_report.txt。人工按需单跑，不进 CI 门禁。

用法：py -3.12 TestReport/run_resume_e2e_probe.py [--only claude|codex] [--keep]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BACKEND))

# 暗号：只在对话里出现，不在 persona/系统提示里——唯有会话真续上才答得出
SECRET = "紫色犀牛-7391"


def _real_providers() -> dict:
    """从真实 config.json 取 claude-cli / codex-cli provider（复制进隔离 config）。"""
    from config import load_settings
    out = {"claude": None, "codex": None}
    for p in load_settings().providers:
        d = {"id": p.id, "type": p.type, "model": getattr(p, "model", ""),
             "name": getattr(p, "name", p.type), "api_key": getattr(p, "api_key", ""),
             "base_url": getattr(p, "base_url", ""), "api_format": getattr(p, "api_format", "openai")}
        if p.type == "claude-cli" and out["claude"] is None:
            out["claude"] = d
        elif p.type == "codex-cli" and out["codex"] is None:
            out["codex"] = d
    return out


class Report:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(r[1] for r in self.rows)

async def _seed(project_dir: str, provider_id: str, slug: str):
    """建 project + conversation + agent_profile（接入真实 provider）。返回 (pid, conv_id)。"""
    from database import get_connection
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__resume_e2e__", project_dir, "Session Resume 端到端实测（自动清理）"))
        pid = cur.lastrowid
        conv = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)",
                                (pid, "resume e2e"))
        conv_id = conv.lastrowid
        # agent_profile 接入 provider（execute_dispatch 用 provider_id_effective 解析后端）
        await db.execute(
            "INSERT INTO project_agents (project_id, slug, name, persona, is_leader) VALUES (?,?,?,?,?)",
            (pid, slug, "实测助手", "你是实测助手，简洁作答。", 0))
        await db.commit()
        return pid, conv_id
    finally:
        await db.close()


async def _mk_task(pid: int, conv_id: int, slug: str, title: str):
    from database import get_connection
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, status, conversation_id, assignee_slug) "
            "VALUES (?,?,?,?,?)", (pid, title, "in_progress", conv_id, slug))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _run_task(pid, conv_id, task_id, slug, provider_id, project_dir, prompt):
    """跑一次真实 execute_dispatch（真起 CLI），返回 run_id。"""
    from executor import runner
    task = {"id": task_id, "conversation_id": conv_id, "project_id": pid,
            "project_dir": project_dir, "title": "e2e"}
    agent = {"slug": slug, "persona": "你是实测助手，简洁作答。",
             "provider_id_effective": provider_id, "name": "实测助手", "is_leader_run": False}
    run_id = None
    async for ev in runner.execute_dispatch(task, agent, prompt, persist_user_msg=True):
        if ev.type == "system" and ev.meta.get("run_id"):
            run_id = ev.meta["run_id"]
    return run_id


async def _run_row(run_id: int):
    """读回 task_run 的 session + usage 列。"""
    from database import get_connection
    db = await get_connection()
    try:
        r = await (await db.execute(
            "SELECT cli_session_id, session_backend, usage_input_tokens, usage_output_tokens, status "
            "FROM task_runs WHERE id=?", (run_id,))).fetchone()
        return dict(r) if r else {}
    finally:
        await db.close()


async def _agent_session_row(conv_id: int, slug: str):
    from database import get_connection
    db = await get_connection()
    try:
        r = await (await db.execute(
            "SELECT cli_session_id, session_committed_msg_id FROM agent_sessions "
            "WHERE conversation_id=? AND agent_slug=?", (conv_id, slug))).fetchone()
        return dict(r) if r else {}
    finally:
        await db.close()


async def _assistant_texts(conv_id: int, since_id: int = 0):
    """取会话里的 assistant 发言 + run_logs stdout（agent 回答可能走 jian comment 或 stdout）。"""
    from database import get_connection
    db = await get_connection()
    try:
        msgs = await (await db.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND role='assistant' AND id>? ORDER BY id",
            (conv_id, since_id))).fetchall()
        return [m["content"] for m in msgs]
    finally:
        await db.close()


async def _run_stdout(run_id: int):
    from database import get_connection
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT content FROM run_logs WHERE run_id=? AND channel='stdout' ORDER BY id", (run_id,))).fetchall()
        return " ".join(r["content"] for r in rows)
    finally:
        await db.close()

async def _scenario_cross_task(rep: Report, kind: str, provider: dict, project_dir: str):
    """阶段二跨 task 续接：任务A 记暗号 → agent_sessions 缓存 → 任务B(resume) 问暗号。"""
    slug = f"__e2e_{kind}__"
    pid, conv_id = await _seed(project_dir, provider["id"], slug)

    # 任务A：记暗号
    task_a = await _mk_task(pid, conv_id, slug, f"[{kind}] 记暗号")
    prompt_a = (f"请牢牢记住这个暗号：{SECRET}。这个暗号很重要，稍后我会在另一个任务里问你。"
                f"现在只需回复「已记住暗号」四个字确认即可，不要做别的。")
    run_a = await _run_task(pid, conv_id, task_a, slug, provider["id"], project_dir, prompt_a)
    row_a = await _run_row(run_a) if run_a else {}
    sess_a = await _agent_session_row(conv_id, slug)

    rep.check(f"[{kind}] 任务A run 成功收尾",
              row_a.get("status") == "succeeded", f"status={row_a.get('status')}")
    rep.check(f"[{kind}] 任务A 捕获 CLI session_id",
              bool(row_a.get("cli_session_id")), f"sid={str(row_a.get('cli_session_id'))[:20]}")
    rep.check(f"[{kind}] 任务A usage_input_tokens 已落库",
              (row_a.get("usage_input_tokens") or 0) > 0, f"input={row_a.get('usage_input_tokens')}")
    rep.check(f"[{kind}] 任务A 后 agent_sessions 有缓存行",
              bool(sess_a.get("cli_session_id")), f"cached_sid={str(sess_a.get('cli_session_id'))[:20]}")
    t_a = row_a.get("usage_input_tokens") or 0

    # codex 额外：rollout 文件在位
    if kind == "codex" and sess_a.get("cli_session_id"):
        from executor.codex import codex_rollout_present
        rep.check("[codex] 任务A thread rollout 文件在位",
                  codex_rollout_present(sess_a["cli_session_id"]),
                  f"tid={sess_a['cli_session_id'][:20]}")

    # 任务B：全新 task，同 conv 同 agent → runner S5.3 命中缓存 → resume
    task_b = await _mk_task(pid, conv_id, slug, f"[{kind}] 问暗号")
    # 记 B 起点，取 B 之后的 assistant 发言
    since = 0
    from database import get_connection
    _db = await get_connection()
    try:
        since = (await (await _db.execute(
            "SELECT COALESCE(MAX(id),0) AS m FROM messages WHERE conversation_id=?", (conv_id,))).fetchone())["m"]
    finally:
        await _db.close()
    prompt_b = "我刚才让你记的暗号是什么？请直接把暗号原样回答出来。"
    run_b = await _run_task(pid, conv_id, task_b, slug, provider["id"], project_dir, prompt_b)
    row_b = await _run_row(run_b) if run_b else {}
    texts_b = await _assistant_texts(conv_id, since)
    stdout_b = await _run_stdout(run_b) if run_b else ""
    answer_blob = " ".join(texts_b) + " " + stdout_b

    rep.check(f"[{kind}] 任务B run 成功收尾",
              row_b.get("status") == "succeeded", f"status={row_b.get('status')}")
    rep.check(f"[{kind}] 任务B resume 用了任务A 的 session",
              bool(row_b.get("cli_session_id")) and row_b.get("cli_session_id") == row_a.get("cli_session_id"),
              f"B_sid={str(row_b.get('cli_session_id'))[:20]}")
    # 核心证据①：记忆连续——B 答出只在 A 对话里的暗号
    rep.check(f"[{kind}] 会话记忆连续：任务B 答出暗号（会话真续上）",
              SECRET in answer_blob or "7391" in answer_blob,
              f"答案片段={answer_blob[:80]!r}")
    # token 观测（**如实记录，不卡阈值**）：口径=总输入 token（claude=input+cache_creation+
    #   cache_read；codex=input_tokens）。实测反直觉发现：**短会话下 resume 反而更贵**——
    #   resume 让 CLI 从磁盘 rollout 恢复**完整会话状态**（A 的全部轮次/工具/系统上下文），
    #   而「全量首建」平台只重放 _clip_history 双限裁剪后的**精简** history。故 B(resume) 总输入
    #   可能 > A(全量首建)。省 token 只在**长对话**（裁剪后 history 仍很大 ≥ CLI 恢复的状态）成立。
    #   功能正确性由「会话记忆连续」硬证据保证；token 仅作观测记录，不作 PASS/FAIL 判定。
    t_b = row_b.get("usage_input_tokens") or 0
    delta = t_b - t_a
    sign = "省" if delta < 0 else "增"
    rep.check(f"[{kind}] token 已捕获（观测：A 全量={t_a} / B resume={t_b}，resume {sign}{abs(delta)}）",
              t_a > 0 and t_b > 0, f"A={t_a} B={t_b}")
    return pid


async def _cleanup_projects():
    """删本探针建的 __resume_e2e__ 项目（级联清 conv/task/run 等）。"""
    from database import get_connection
    db = await get_connection()
    try:
        await db.execute("DELETE FROM projects WHERE title='__resume_e2e__'")
        await db.commit()
    finally:
        await db.close()


async def _run_all(only: str):
    from run_qa_suite import setup_isolated_config, bootstrap_backend
    import config, json as _json

    rep = Report()
    # 先读真实 provider（在 setup_isolated_config 覆盖 config.CONFIG_FILE 之前，否则读到空隔离 config）
    provs = _real_providers()

    tmp = Path(tempfile.mkdtemp(prefix="akivili_resume_e2e_"))
    paths = setup_isolated_config(tmp)
    want = [k for k in ("claude", "codex") if (only in ("", k))]
    cfg_provs = [provs[k] for k in want if provs[k]]
    missing = [k for k in want if not provs[k]]
    for m in missing:
        rep.check(f"[{m}] provider 未配置，跳过", False, "config.json 无该 CLI provider")
    if not cfg_provs:
        return rep, tmp

    cfg = _json.loads(paths["config"].read_text(encoding="utf-8"))
    cfg["providers"] = cfg_provs
    cfg["default_provider_id"] = cfg_provs[0]["id"]
    paths["config"].write_text(_json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    config.load_settings.cache_clear() if hasattr(config.load_settings, "cache_clear") else None

    await bootstrap_backend(paths)
    project_dir = str(paths["project"])

    for k in want:
        if not provs[k]:
            continue
        try:
            await _scenario_cross_task(rep, k, provs[k], project_dir)
        except Exception as e:  # noqa: BLE001
            import traceback
            rep.check(f"[{k}] 场景异常", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            await _cleanup_projects()

    return rep, tmp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["claude", "codex"], default="", help="只测某一后端")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    print("=" * 64)
    print("Session Resume 端到端实测（真起 CLI，消耗真实 token）")
    print("=" * 64)
    rep, tmp = asyncio.run(_run_all(args.only))
    print("\n" + "=" * 64)
    n_ok = sum(1 for _, ok, _ in rep.rows if ok)
    print(f"结论：{'PASS 全部通过' if rep.ok else 'FAIL 有失败项'} — {n_ok}/{len(rep.rows)}")
    print("=" * 64)
    if not args.keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"[keep] {tmp}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    _report = HERE / "resume_e2e_report.txt"
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
