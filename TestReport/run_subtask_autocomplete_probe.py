"""Akivili 子任务自动收尾探针。

验证 collab._run_one 的自动收尾逻辑（隔离库 + 假执行器）：
  1. 被指派的子任务 run 成功后，平台自动把子任务置 done（不依赖 Agent 调 jian status done）。
  2. 子任务全部 done 后，父任务经 maybe_advance_parent 推进到 reviewing 并唤醒负责人。
  3. 顶层任务的 run（非子任务）不被误置 done。
  4. run 失败的子任务不置 done。

用假 runner.execute_dispatch（不调真实 CLI/LLM）。临时 config/DB，测完清理。
"""
from __future__ import annotations

import argparse
import asyncio
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


async def seed(paths):
    import agents as agents_mod
    from database import get_connection
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("子任务收尾探针项目", str(paths["project"]), "probe"))
        pid = cur.lastrowid
        members = [
            ("specialized-project-owner", "项目负责人", 1),
            ("engineering-frontend-developer", "前端开发者", 0),
            ("engineering-backend-architect", "后端架构师", 0),
        ]
        for slug, name, lead in members:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) VALUES (?,?,?,?,?,?)",
                (pid, slug, name, "🤖", f"你是{name}。", lead))
            await db.execute(
                "INSERT OR IGNORE INTO agent_profiles (slug, provider_id, nickname) VALUES (?,?,?)",
                (slug, "p-claude", name))
        await db.commit()
        return pid
    finally:
        await db.close()


async def make_task(pid, title, parent=None, assignee=""):
    from database import get_connection
    db = await get_connection()
    try:
        conv = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, title))
        status = "in_progress"
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, status, conversation_id, parent_task_id, assignee_slug) "
            "VALUES (?,?,?,?,?,?)",
            (pid, title, status, conv.lastrowid, parent, assignee))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def task_status(tid):
    from database import get_connection
    db = await get_connection()
    try:
        r = await (await db.execute("SELECT status FROM tasks WHERE id=?", (tid,))).fetchone()
        return r["status"] if r else None
    finally:
        await db.close()


async def run_probe(paths):
    probe = Probe()
    await bootstrap_backend(paths)
    import collab
    from executor import runner
    from executor.base import ExecEvent

    # 假执行器：不调真实模型，成功时 yield 一段文本；hang_slugs 里的返回错误
    behavior = {"fail_slugs": set()}

    async def fake_dispatch(task_obj, agent_obj, prompt, persist_user_msg=True, user_name=""):
        slug = agent_obj["slug"]
        yield ExecEvent("system", "", {"run_id": f"r-{task_obj['id']}-{slug}"})
        if slug in behavior["fail_slugs"]:
            yield ExecEvent("error", "模拟失败")
            yield ExecEvent("done")
            return
        yield ExecEvent("text", f"{slug} 的自我介绍已完成。")
        yield ExecEvent("done")
    runner.execute_dispatch = fake_dispatch
    runner.kill_run = lambda rid: None

    from database import get_connection

    async def enqueue(task, slug, done=True):
        """建一条 run_queue（模拟真实入队），执行后按需标记 done——供 maybe_review 的"无待跑 run"判据。"""
        db = await get_connection()
        try:
            cur = await db.execute(
                "INSERT INTO run_queue (task_id, agent_slug, trigger, is_leader, prompt, status) "
                "VALUES (?,?,?,?,?,?)", (task, slug, "assign", 0, "", "done" if done else "queued"))
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    pid = await seed(paths)

    # ---- Test 1: 子任务执行完 → 直接进 done；仍有兄弟子任务在跑时父任务不推进 ----
    parent = await make_task(pid, "Kickoff 团队 Show", assignee="specialized-project-owner")
    sub1 = await make_task(pid, "自我介绍：前端", parent=parent, assignee="engineering-frontend-developer")
    sub2 = await make_task(pid, "自我介绍：后端", parent=parent, assignee="engineering-backend-architect")
    await enqueue(sub1, "engineering-frontend-developer", done=True)
    await enqueue(sub2, "engineering-backend-architect", done=False)  # sub2 还在排队
    await collab._run_one({"task_id": sub1, "agent_slug": "engineering-frontend-developer",
                           "is_leader": 0, "trigger": "assign", "prompt": "介绍你自己"})
    probe.check("子任务执行完直接进 done（无验证中概念）",
                await task_status(sub1) == "done", f"sub1={await task_status(sub1)}")
    probe.check("还有子任务在跑时父任务不推进",
                await task_status(parent) == "in_progress", f"parent={await task_status(parent)}")

    # ---- Test 2: 全部子任务 done → 父任务自动进 reviewing（子任务保持 done） ----
    db = await get_connection()
    try:
        await db.execute("UPDATE run_queue SET status='done' WHERE task_id=?", (sub2,))
        await db.commit()
    finally:
        await db.close()
    await collab._run_one({"task_id": sub2, "agent_slug": "engineering-backend-architect",
                           "is_leader": 0, "trigger": "assign", "prompt": "介绍你自己"})
    probe.check("第二个子任务也进 done", await task_status(sub2) == "done")
    probe.check("全部子任务 done → 父任务自动进 reviewing",
                await task_status(parent) == "reviewing", f"parent={await task_status(parent)}")

    # ---- Test 3: 无子任务的独立顶层任务 run 成功 → 自动进 reviewing（不进 done） ----
    top = await make_task(pid, "独立顶层任务", assignee="engineering-frontend-developer")
    await enqueue(top, "engineering-frontend-developer", done=True)
    await collab._run_one({"task_id": top, "agent_slug": "engineering-frontend-developer",
                           "is_leader": 0, "trigger": "assign", "prompt": "做点事"})
    probe.check("独立顶层任务执行完 → 自动进 reviewing（非 done）",
                await task_status(top) == "reviewing", f"top={await task_status(top)}")

    # ---- Test 4: run 失败的任务不推进 ----
    behavior["fail_slugs"] = {"engineering-frontend-developer"}
    top2 = await make_task(pid, "会失败的独立任务", assignee="engineering-frontend-developer")
    await enqueue(top2, "engineering-frontend-developer", done=True)
    await collab._run_one({"task_id": top2, "agent_slug": "engineering-frontend-developer",
                           "is_leader": 0, "trigger": "assign", "prompt": "做点事"})
    probe.check("run 失败的任务不推进（保持 in_progress）",
                await task_status(top2) == "in_progress", f"top2={await task_status(top2)}")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    base = Path(tempfile.mkdtemp(prefix="akivili_subtask_"))
    paths = setup_isolated_config(base)
    try:
        probe = asyncio.run(run_probe(paths))
        n = len(probe.results)
        passed = sum(1 for r in probe.results if r[1])
        print(f"\nSubtask autocomplete probe: {passed}/{n} passed")
        sys.exit(0 if probe.ok else 1)
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
