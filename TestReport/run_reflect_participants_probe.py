"""Akivili 反思参与者口径探针（直接建卡型也要沉淀）。

回归 reflect._participants 的扩展口径：参与者 = 有 task_runs 的成员 ∪ 在本任务/子任务
会话里有 author_slug=本人 assistant 发言的成员。覆盖「直接建子任务卡片
（jian subtask --body-file，有真实产出但不产生 task_run）」这类成员——干了活就该有沉淀。

背景：项目26 里 a69a74aa（前端）接了最多任务、大多是直接建卡型产出，此前因
_participants 只认 task_runs 而完全没被反思、几乎零沉淀。本探针把该场景钉死。

用假 runner.run_oneshot（不调真实模型）。临时 config/DB/记忆目录，测完清理。
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
    def __init__(self) -> None:
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(r[1] for r in self.results)


async def seed(paths):
    """父任务 + 两个子任务：
      子A owner=有 run 成员（task_run succeeded + 发言）
      子B owner=建卡型成员（无 task_run，仅有 author_slug 发言，模拟 jian subtask --body-file）
    """
    import agents as agents_mod
    from database import get_connection
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("反思参与者探针项目", str(paths["project"]), "reflect participants probe"))
        pid = cur.lastrowid
        members = [
            ("engineering-data-engineer", "数据工程师", "Echo"),      # 有 run
            ("engineering-frontend-developer", "前端开发者", "Iris"),  # 仅建卡
        ]
        for slug, name, nick in members:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) VALUES (?,?,?,?,?,0)",
                (pid, slug, name, "🤖", f"你是{name}，专业负责相关工作。"))
            await db.execute(
                "INSERT OR IGNORE INTO agent_profiles (slug, provider_id, nickname) VALUES (?,?,?)",
                (slug, "p-claude", nick))

        async def _task(title, status, parent=None, assignee=""):
            c = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, title))
            cid = c.lastrowid
            t = await db.execute(
                "INSERT INTO tasks (project_id, title, description, status, conversation_id, "
                "assignee_slug, parent_task_id) VALUES (?,?,?,?,?,?,?)",
                (pid, title, "技术分析", status, cid, assignee, parent))
            return t.lastrowid, cid

        parent_id, _ = await _task("产品内审需求技术分析", "in_progress")
        # 子A：数据工程师，真跑过 run + 发言
        subA, cidA = await _task("数据侧技术分析", "done", parent_id, "engineering-data-engineer")
        await db.execute(
            "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) "
            "VALUES (?,?,?,?, 'succeeded')", (subA, cidA, "engineering-data-engineer", "p-claude"))
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, author_slug) VALUES (?,?,?,?)",
            (cidA, "assistant", "数据侧分析：取数口径已核对，脏数据先备份再删。", "engineering-data-engineer"))
        # 子B：前端，直接建卡型——无 task_run，只有本人 assistant 发言（正文）
        subB, cidB = await _task("前端专项分析", "done", parent_id, "engineering-frontend-developer")
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, author_slug) VALUES (?,?,?,?)",
            (cidB, "assistant",
             "前端专项分析：跨容器跳转复用同一播放器实例，严禁重新拉流，否则二次首帧黑屏。", "engineering-frontend-developer"))
        await db.commit()
        return pid, parent_id, subA, subB
    finally:
        await db.close()


async def run_probe(paths, keep):
    probe = Probe()
    await bootstrap_backend(paths)
    import reflect
    from executor import runner
    from memory import read_memory

    async def fake_oneshot(provider_id, system_prompt, prompt, project_dir=".", timeout_sec=300):
        # 按各角色产出的特征句正向分流（避免用「数据」这类会误命中模板/父任务标题的宽词）：
        # 前端产出含「播放器」，数据侧产出含「取数口径」。
        if "播放器" in prompt:
            return "- 跨容器跳转复用播放器实例严禁重新拉流\n- 二次首帧黑屏是体验红线"
        return "- 大数据量取数分批处理\n- 脏数据先备份再删"
    runner.run_oneshot = fake_oneshot

    try:
        # ---- Test 1: _participants 同时纳入「有 run」与「仅建卡发言」两类成员 ----
        parts = await reflect._participants(await seed_ret(paths, probe))
        slugs = {p["slug"] for p in parts}
        probe.check("_participants 纳入有 run 的成员（数据工程师）",
                    "engineering-data-engineer" in slugs, f"参与者={sorted(slugs)}")
        probe.check("_participants 纳入直接建卡型成员（前端，无 run 有发言）",
                    "engineering-frontend-developer" in slugs, f"参与者={sorted(slugs)}")

        # ---- Test 2: 父任务 done → 两类成员都沉淀 knowhow ----
        await reflect.reflect_on_task_done(PARENT["id"])
        mem_data = read_memory("engineering-data-engineer")
        mem_front = read_memory("engineering-frontend-developer")
        probe.check("有 run 成员沉淀了 knowhow",
                    "分批处理" in mem_data or "备份再删" in mem_data,
                    "数据工程师 knowhow 已写")
        probe.check("直接建卡型成员也沉淀了 knowhow（核心修复）",
                    "复用播放器实例" in mem_front or "首帧黑屏" in mem_front,
                    "前端 knowhow 已写")
    finally:
        pass
    return probe


# 用一个可变容器把 seed 的父任务 id 传给 Test 2（避免改 run_probe 签名）
PARENT = {"id": None}


async def seed_ret(paths, probe):
    """跑一次 seed，记住父任务 id，返回父任务 id 供 _participants 用。"""
    pid, parent_id, subA, subB = await seed(paths)
    PARENT["id"] = parent_id
    return parent_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-reflectpart-"))
    paths = setup_isolated_config(tmp)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")
    total = len(probe.results)
    passed = sum(1 for r in probe.results if r[1])
    print(f"\n=== reflect participants probe: {passed}/{total} ===")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
