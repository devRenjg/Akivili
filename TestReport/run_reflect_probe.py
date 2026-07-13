"""Akivili 任务完成反思(Know-how 沉淀)探针。

在隔离库里验证 reflect.py：
  1. 任务 done → 真正跑过 run 的角色各自复盘、写入 knowhow 受管段落。
  2. 只覆盖「有 run」的参与者；没参与的角色不写。
  3. 超过上限触发压缩合并（条数不超过 KNOWHOW_MAX）。
  4. 测试项目（__test__ 前缀）跳过，不污染真实记忆。

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


async def seed(paths, title="真实项目·数据巡检"):
    import agents as agents_mod
    from database import get_connection
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("反思探针项目", str(paths["project"]), "reflect probe"))
        pid = cur.lastrowid
        # 两个成员：数据工程师(参与) + 前端(不参与)
        for slug, name, prov in [("engineering-data-engineer", "数据工程师", "p-claude"),
                                 ("engineering-frontend-developer", "前端开发者", "p-claude")]:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) VALUES (?,?,?,?,?,0)",
                (pid, slug, name, "🤖", f"你是{name}，专业负责相关工作。"))
            await db.execute(
                "INSERT OR IGNORE INTO agent_profiles (slug, provider_id, nickname) VALUES (?,?,?)",
                (slug, prov, {"engineering-data-engineer": "Echo", "engineering-frontend-developer": "Iris"}[slug]))
        # 建任务 + 会话
        conv = await db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, title))
        conv_id = conv.lastrowid
        tcur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, conversation_id, assignee_slug) "
            "VALUES (?,?,?,?,?,?)",
            (pid, title, "校验一批数据并做基础分析", "in_progress", conv_id, "engineering-data-engineer"))
        task_id = tcur.lastrowid
        # 数据工程师真的跑过一个 run + 有产出发言
        await db.execute(
            "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) VALUES (?,?,?,?, 'succeeded')",
            (task_id, conv_id, "engineering-data-engineer", "p-claude"))
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, author_slug) VALUES (?,?,?,?)",
            (conv_id, "assistant", "校验完成：清理了7条错位脏数据，逐批抽样比对指标一致。", "engineering-data-engineer"))
        await db.commit()
        return pid, task_id, conv_id
    finally:
        await db.close()


async def run_probe(paths, keep):
    probe = Probe()
    await bootstrap_backend(paths)
    import reflect
    from executor import runner
    from memory import read_memory

    # 假 run_oneshot：反思→固定3条；压缩→返回精简2条
    calls = {"reflect": 0, "compact": 0}

    async def fake_oneshot(provider_id, system_prompt, prompt, project_dir=".", timeout_sec=300):
        if "压缩" in prompt or "cap" in prompt.lower() or "以内" in prompt:
            calls["compact"] += 1
            return "- 合并后的经验A\n- 合并后的经验B"
        calls["reflect"] += 1
        return "- 大数据量取数要分批处理\n- 字段错位脏数据先备份再删\n- 指标核对用逐批抽样比对源"
    runner.run_oneshot = fake_oneshot

    try:
        # ---- Test 1: 任务 done → 参与角色写入 knowhow ----
        pid, task_id, _ = await seed(paths)
        await reflect.reflect_on_task_done(task_id)
        mem_de = read_memory("engineering-data-engineer")
        probe.check("参与角色(数据工程师)写入 knowhow 受管段落",
                    "akivili:managed:knowhow:start" in mem_de and "分批" in mem_de,
                    f"reflect调用={calls['reflect']}")
        probe.check("knowhow 段落含具体 Know-how 条目",
                    mem_de.count("- ") >= 3)

        # ---- Test 2: 未参与角色不写 ----
        mem_fe = read_memory("engineering-frontend-developer")
        probe.check("未参与角色(前端)不写 knowhow",
                    "akivili:managed:knowhow" not in mem_fe)

        # ---- Test 3: 超上限触发压缩 ----
        # 预置 KNOWHOW_MAX 条已有条目，再反思一次应触发压缩
        from memory import upsert_managed_section
        many = "## 🧠 工作经验与 Know-how\n\n" + "\n".join(f"- 旧经验{i}" for i in range(reflect.KNOWHOW_MAX))
        upsert_managed_section("engineering-data-engineer", "knowhow", many)
        calls["compact"] = 0
        await reflect.reflect_on_task_done(task_id)
        mem_after = read_memory("engineering-data-engineer")
        import re
        mm = re.search(r"knowhow:start -->(.*?)<!-- akivili:managed:knowhow:end", mem_after, re.DOTALL)
        n_bullets = mm.group(1).count("\n- ") if mm else 0
        probe.check("超上限触发压缩合并", calls["compact"] >= 1)
        probe.check("压缩后条数不超过上限",
                    n_bullets <= reflect.KNOWHOW_MAX, f"条数={n_bullets}, 上限={reflect.KNOWHOW_MAX}")

        # ---- Test 3b: 压缩后血缘保留（旧条目继承原 task 标记，不被抹平成当前任务）----
        # 预置带「历史任务标记」的满额 knowhow，其中一条内容独特；压缩返回它的近乎原样版本，
        # 应继承原标记 task:43，而非当前任务标记。
        from memory import upsert_managed_section, task_marker
        uniq = "整列错位脏数据靠单行校验抓不出要按批次采样比对列间相关性"
        seeded = [f"- {uniq} <!-- akivili:task:43 -->"]
        seeded += [f"- 历史常规经验{i} <!-- akivili:task:43 -->" for i in range(reflect.KNOWHOW_MAX - 1)]
        upsert_managed_section("engineering-data-engineer", "knowhow",
                               "## 🧠 工作经验与 Know-how\n\n" + "\n".join(seeded))
        # 压缩桩：返回那条独特经验的近乎原样文本（会被相似度匹配到 task:43 源条目）
        async def fake_oneshot_prov(provider_id, system_prompt, prompt, project_dir=".", timeout_sec=300):
            if "压缩" in prompt or "cap" in prompt.lower() or "以内" in prompt:
                calls["compact"] += 1
                return f"- {uniq}\n- 全新合成的经验条目xyz"
            calls["reflect"] += 1
            return "- 大数据量取数要分批处理\n- 字段错位脏数据先备份再删\n- 指标核对用逐批抽样比对源"
        runner.run_oneshot = fake_oneshot_prov
        cur_marker = task_marker(task_id)
        await reflect.reflect_on_task_done(task_id)
        mem_prov = read_memory("engineering-data-engineer")
        mp = re.search(r"knowhow:start -->(.*?)<!-- akivili:managed:knowhow:end", mem_prov, re.DOTALL)
        prov_body = mp.group(1) if mp else ""
        # 那条独特经验应仍带 task:43（继承原标记），而非当前任务标记
        line_uniq = next((l for l in prov_body.splitlines() if uniq in l), "")
        probe.check("压缩后旧经验继承原 task 标记(血缘不被抹平)",
                    "akivili:task:43" in line_uniq and cur_marker not in line_uniq,
                    f"line={line_uniq[:80]}")
        # 全新合成条目应归当前任务标记
        line_new = next((l for l in prov_body.splitlines() if "xyz" in l), "")
        probe.check("压缩后新合成条目归当前任务标记",
                    cur_marker in line_new or "akivili:task:" in line_new,
                    f"line={line_new[:80]}")
        runner.run_oneshot = fake_oneshot  # 还原

        # ---- Test 4: 测试项目跳过 ----
        calls["reflect"] = 0
        pid2, task2, _ = await seed(paths, title="__test__反思跳过")
        await reflect.reflect_on_task_done(task2)
        probe.check("测试项目跳过反思（不调模型）", calls["reflect"] == 0)

    finally:
        pass
    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    base = Path(tempfile.mkdtemp(prefix="akivili_reflect_"))
    paths = setup_isolated_config(base)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
        n = len(probe.results)
        passed = sum(1 for r in probe.results if r[1])
        print(f"\nReflect probe: {passed}/{n} passed")
        sys.exit(0 if probe.ok else 1)
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
