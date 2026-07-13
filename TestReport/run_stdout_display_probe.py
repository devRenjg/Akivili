"""Akivili 会话正文来源探针（CLI stdout 不落正文）。

验证 runner.execute_dispatch 的会话消息落库分流（隔离库 + 假后端）：
  1. CLI 后端（claude/codex）：流式 stdout 全文**不落成 assistant 会话消息**
     （真实交付走 jian comment），但仍进 run_logs 供日志详情排查。
  2. API 后端：stdout final_text **落成** assistant 会话消息（无 jian 通道，stdout 即唯一产出）。
  3. CLI run 里 Agent 走了 jian comment 的发言，正常保留在会话正文里（不受影响）。
  4. 收工写记忆的 stdout 兜底不受影响（_RUN_CTX.stream_text 仍被填充）。

用真实 execute_dispatch + 假 ExecutorBackend（不调真实 CLI/LLM）。临时 config/DB，测完清理。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import setup_isolated_config, bootstrap_backend  # noqa: E402


class Probe:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(r[1] for r in self.results)


async def seed_project(paths):
    from database import get_connection
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("正文来源探针项目", str(paths["project"]), "probe"))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def make_task(pid, title, slug):
    """建一个任务 + 会话，返回 (task_dict, conv_id)。task_dict 形如 execute_dispatch 期望的行。"""
    from database import get_connection
    db = await get_connection()
    try:
        conv = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, title))
        conv_id = conv.lastrowid
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, status, conversation_id, assignee_slug) "
            "VALUES (?,?,?,?,?)",
            (pid, title, "in_progress", conv_id, slug))
        await db.commit()
        return cur.lastrowid, conv_id
    finally:
        await db.close()


async def assistant_msgs(conv_id):
    from database import get_connection
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT content, author_slug FROM messages WHERE conversation_id=? AND role='assistant' ORDER BY id",
            (conv_id,))).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def stdout_logs(run_id):
    from database import get_connection
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT content FROM run_logs WHERE run_id=? AND channel='stdout' ORDER BY id",
            (run_id,))).fetchall()
        return [r["content"] for r in rows]
    finally:
        await db.close()


class FakeProvider:
    def __init__(self, ptype):
        self.id = f"p-{ptype}"
        self.type = ptype          # claude-cli / codex-cli / api
        self.model = "fake-model"
        self.api_key = ""
        self.base_url = ""
        self.api_format = "openai"


class FakeBackend:
    """假后端：yield 一段流式 stdout 文本（模拟 Agent「边干边碎念」），不调真实 CLI/LLM。"""
    def __init__(self, stdout_text):
        self._text = stdout_text

    async def run(self, ctx, on_pid=None):
        from executor.base import ExecEvent
        if on_pid:
            on_pid(0)
        yield ExecEvent("text", self._text)
        yield ExecEvent("done")


class FakeBackendWithJian:
    """假后端：执行中（yield done 之前）真的调用 jian comment 落一条交付，模拟守规矩的 Agent。"""
    def __init__(self, stdout_text, task_id, slug, body):
        self._text = stdout_text
        self._task_id = task_id
        self._slug = slug
        self._body = body

    async def run(self, ctx, on_pid=None):
        from executor.base import ExecEvent
        from routes import agent_cli
        if on_pid:
            on_pid(0)
        yield ExecEvent("text", self._text)
        # Agent 在执行中调用 jian comment（真实平台动作，落 messages）
        await agent_cli.add_comment(agent_cli.CommentReq(
            task_id=self._task_id, agent_slug=self._slug, body=self._body))
        yield ExecEvent("done")


STDOUT_CHATTER = "先设 PYTHONUTF8=1，jian 命令通过 jian.bat 调用。roster 已取到，连通正常。"
JIAN_DELIVERABLE = "大家好，我是后端架构师。这是我通过 jian comment 提交的正式自我介绍。"


async def task_activities(task_id):
    from database import get_connection
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT action, actor_type, actor_name, detail FROM activities WHERE task_id=? ORDER BY id",
            (task_id,))).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


def has_no_deliverable_marker(acts):
    import json
    for a in acts:
        if a["action"] == "commented" and a["actor_type"] == "system":
            try:
                note = json.loads(a["detail"] or "{}").get("note", "")
            except (ValueError, TypeError):
                note = ""
            if "未通过 jian" in note:
                return True
    return False


async def run_one(paths, ptype, with_jian=False):
    """跑一次真实 execute_dispatch（假 provider/backend），返回本轮的 (run_id, conv_id, task_id)。

    with_jian=True 时用 FakeBackendWithJian：执行中真的调 jian comment 落交付（守规矩的 Agent）。
    """
    from executor import runner
    pid = getattr(run_one, "_pid", None)
    if pid is None:
        pid = await seed_project(paths)
        run_one._pid = pid
    slug = "specialized-project-owner"
    title = f"{ptype}{'+jian' if with_jian else ''} 正文来源探针"
    task_id, conv_id = await make_task(pid, title, slug)

    fake_provider = FakeProvider(ptype)
    if with_jian:
        fake_backend = FakeBackendWithJian(STDOUT_CHATTER, task_id, slug, JIAN_DELIVERABLE)
    else:
        fake_backend = FakeBackend(STDOUT_CHATTER)
    runner._provider_by_id = lambda _pid_str: fake_provider
    runner._pick_backend = lambda _p: fake_backend

    task = {
        "id": task_id, "conversation_id": conv_id, "project_id": pid,
        "project_dir": str(paths["project"]), "title": title,
    }
    agent = {"slug": slug, "persona": "你是负责人。", "provider_id_effective": fake_provider.id,
             "name": "项目负责人", "is_leader_run": False}

    run_id = None
    async for ev in runner.execute_dispatch(task, agent, "请做自我介绍", persist_user_msg=False):
        if ev.type == "system" and ev.meta.get("run_id"):
            run_id = ev.meta["run_id"]
    return run_id, conv_id, task_id


async def run_probe(paths):
    probe = Probe()
    await bootstrap_backend(paths)
    from executor.runner import _RUN_CTX  # noqa: F401
    from executor import runner
    from routes import agent_cli  # noqa: F401

    # --- 场景 A：CLI 后端（claude-cli）无 jian——stdout 不落正文，且打「未走 jian」标记 ---
    run_id, conv_id, task_id = await run_one(paths, "claude-cli")
    msgs = await assistant_msgs(conv_id)
    logs = await stdout_logs(run_id)
    acts = await task_activities(task_id)
    probe.check("CLI 无 jian: stdout 未落成 assistant 会话消息",
                all(STDOUT_CHATTER not in m["content"] for m in msgs),
                f"assistant 消息数={len(msgs)}")
    probe.check("CLI 无 jian: stdout 仍进 run_logs（日志可排查）",
                any(STDOUT_CHATTER in c for c in logs),
                f"stdout 日志条数={len(logs)}")
    probe.check("CLI 无 jian: 打「未通过 jian 提交交付」标记（不拿 stdout 兜底）",
                has_no_deliverable_marker(acts),
                f"活动数={len(acts)}")

    # --- 场景 B：CLI 后端 + 执行中走了 jian comment——发言保留在正文、且不打标记 ---
    run_id2, conv_id2, task_id2 = await run_one(paths, "claude-cli", with_jian=True)
    msgs2 = await assistant_msgs(conv_id2)
    acts2 = await task_activities(task_id2)
    probe.check("CLI+jian: jian comment 的真实交付保留在会话正文",
                any(JIAN_DELIVERABLE in m["content"] for m in msgs2),
                f"assistant 消息数={len(msgs2)}")
    probe.check("CLI+jian: 正文里没有 stdout 碎语（仅有 jian comment 交付）",
                all(STDOUT_CHATTER not in m["content"] for m in msgs2),
                "正文干净")
    probe.check("CLI+jian: 有交付则不打「未走 jian」标记",
                not has_no_deliverable_marker(acts2),
                f"活动数={len(acts2)}")

    # --- 场景 C：API 后端——stdout final_text 即唯一产出，必须落成会话正文、且不打标记 ---
    run_id3, conv_id3, task_id3 = await run_one(paths, "api")
    msgs3 = await assistant_msgs(conv_id3)
    acts3 = await task_activities(task_id3)
    probe.check("API: stdout final_text 落成 assistant 会话消息",
                any(STDOUT_CHATTER in m["content"] for m in msgs3),
                f"assistant 消息数={len(msgs3)}")
    probe.check("API: 不打「未走 jian」标记（标记仅针对 CLI 后端）",
                not has_no_deliverable_marker(acts3),
                f"活动数={len(acts3)}")

    return probe


async def _task_id_of_conv(conv_id):
    from database import get_connection
    db = await get_connection()
    try:
        r = await (await db.execute("SELECT id FROM tasks WHERE conversation_id=?", (conv_id,))).fetchone()
        return r["id"] if r else None
    finally:
        await db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录供排查")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili_stdout_probe_"))
    try:
        paths = setup_isolated_config(tmp)
        probe = asyncio.run(run_probe(paths))
        print("\n" + ("✅ 全部通过" if probe.ok else "❌ 存在失败项"))
        n_ok = sum(1 for _, ok, _ in probe.results if ok)
        print(f"{n_ok}/{len(probe.results)} 通过")
        sys.exit(0 if probe.ok else 1)
    finally:
        if args.keep:
            print(f"[keep] 临时目录：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

