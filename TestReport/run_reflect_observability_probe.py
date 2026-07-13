"""Akivili 反思可观测性探针（沉淀失败不再沉默）。

回归 reflect_on_task_done 对三类结果的留痕：
  - 成功(_reflect_one>0) → 汇总列入「已沉淀」
  - 无增量(_reflect_one==0) → 汇总计「N 人无新增经验」，不报错
  - 失败(_reflect_one 抛异常) → 逐个记「⚠️ X 沉淀失败（错误类型）可重跑」+ 汇总计失败数

背景：火花 task78 并发调 CLI 偶发失败被 gather(return_exceptions) 静默吞掉，
活动流只报成功者，干了活却没沉淀且无人知晓。本探针把三类留痕钉死。

monkeypatch _reflect_one 制造三种结果，不调真实模型。临时库，测完清理。
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
    """项目 + 3 个有 run 的成员（成功/无增量/失败各一），父任务 done 触发反思。"""
    import agents as agents_mod
    from database import get_connection
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("反思可观测探针项目", str(paths["project"]), "reflect observability probe"))
        pid = cur.lastrowid
        members = [
            ("engineering-data-engineer", "数据工程师", "Echo"),        # 成功
            ("engineering-frontend-developer", "前端开发者", "Iris"),    # 无增量
            ("engineering-backend-architect", "后端架构师", "Kafka"),    # 失败
        ]
        c = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)",
                             (pid, "产品内审需求技术分析"))
        conv_id = c.lastrowid
        t = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, conversation_id) "
            "VALUES (?,?,?,?,?)", (pid, "产品内审需求技术分析", "技术分析", "in_progress", conv_id))
        task_id = t.lastrowid
        for slug, name, nick in members:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) "
                "VALUES (?,?,?,?,?,0)", (pid, slug, name, "🤖", f"你是{name}。"))
            await db.execute(
                "INSERT OR IGNORE INTO agent_profiles (slug, provider_id, nickname) VALUES (?,?,?)",
                (slug, "p-claude", nick))
            # 每人一个 succeeded run + 一条发言（确保进 _participants）
            await db.execute(
                "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) "
                "VALUES (?,?,?,?, 'succeeded')", (task_id, conv_id, slug, "p-claude"))
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content, author_slug) VALUES (?,?,?,?)",
                (conv_id, "assistant", f"{name}的技术分析产出。", slug))
        await db.commit()
        return pid, task_id
    finally:
        await db.close()


async def _activities(task_id):
    from database import get_connection
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT detail FROM activities WHERE task_id=? AND action='commented' "
            "AND actor_type='system' ORDER BY id", (task_id,))).fetchall()
        import json
        return [json.loads(r["detail"] or "{}").get("note", "") for r in rows]
    finally:
        await db.close()


async def run_probe(paths, keep):
    probe = Probe()
    await bootstrap_backend(paths)
    import reflect

    pid, task_id = await seed(paths)

    # 按 slug 制造三类结果：成功(3条)/无增量(0)/失败(抛异常)
    async def fake_reflect_one(tid, member):
        s = member["slug"]
        if s == "engineering-data-engineer":
            return 3
        if s == "engineering-frontend-developer":
            return 0
        raise TimeoutError("模拟 run_oneshot 超时（并发调 CLI 偶发失败）")
    reflect._reflect_one = fake_reflect_one

    await reflect.reflect_on_task_done(task_id)
    notes = await _activities(task_id)
    blob = "\n".join(notes)

    probe.check("成功成员列入汇总「已沉淀」", "Echo 已沉淀" in blob, f"notes={notes}")
    probe.check("无增量成员计入汇总（不报错）", "1 人本次无新增" in blob,
                "无增量单独计数")
    probe.check("失败成员逐条留痕（带错误类型 + slug 可重跑）",
                any("Kafka 的经验沉淀失败（TimeoutError）" in n and "slug=engineering-backend-architect" in n
                    for n in notes), "失败留痕含错误类型与 slug")
    probe.check("汇总列出失败待重跑", "1 人沉淀失败待重跑" in blob and "Kafka" in blob,
                "汇总含失败计数")
    probe.check("失败不影响成功/无增量的正常汇总（三类共存一条汇总）",
                any("已沉淀" in n and "无新增" in n and "沉淀失败待重跑" in n for n in notes),
                "三类同现于汇总行")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-reflectobs-"))
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
    print(f"\n=== reflect observability probe: {passed}/{total} ===")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
