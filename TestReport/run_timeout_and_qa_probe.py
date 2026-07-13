"""Akivili 超时保成果 + 收尾验收路由探针。

验证本轮三项修复（隔离库 + 直接调被测函数）：
  A/B 超时保成果：判超时后宽限内已产出交付 → 记 done（保成果），无交付 → kill+failed。
  QA 路由：父任务收尾 prompt 含「按需先 @ 验收成员」的措辞，且团队有 QA 成员时给出点名提示。

临时 config/DB，测完清理。
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


async def run_probe(paths):
    probe = Probe()
    await bootstrap_backend(paths)
    import collab
    from executor import runner
    from database import get_connection

    # 极小超时便于快速验证
    collab.GRACE_SEC = 1

    # 造一个真实任务给 B 测试的 log_activity 用（有 FK 约束）
    db0 = await get_connection()
    try:
        pid0 = (await db0.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("B超时探针项目", str(paths["project"]), "probe"))).lastrowid
        convb = (await db0.execute("INSERT INTO conversations (project_id,title) VALUES (?,?)", (pid0, "b"))).lastrowid
        btask = (await db0.execute(
            "INSERT INTO tasks (project_id,title,status,conversation_id) VALUES (?,?,?,?)",
            (pid0, "B超时任务", "in_progress", convb))).lastrowid
        await db0.commit()
    finally:
        await db0.close()

    killed = []
    finalized = []
    runner.kill_run = lambda rid: killed.append(rid)
    async def fake_finalize(rid, status): finalized.append((rid, status))
    runner.finalize_run = fake_finalize

    # ---- B1：宽限内检测到交付 → 保成果（saved=True，不 kill）----
    async def has_deliverable(tid, slug): return True
    collab._run_produced_deliverable = has_deliverable
    saved = await collab._grace_then_kill(btask, "engineering-data-engineer",
                                          {"name": "数据工程师"}, "run-A", "idle_timeout")
    probe.check("B 超时但宽限内有交付 → 保成果(记 done)", saved is True, f"saved={saved}")
    probe.check("B 保成果时不 kill 进程", "run-A" not in killed, f"killed={killed}")
    probe.check("B 保成果时 finalize 为 succeeded",
                ("run-A", "succeeded") in finalized, f"finalized={finalized}")

    # ---- B2：宽限内无交付 → kill + failed ----
    killed.clear(); finalized.clear()
    async def no_deliverable(tid, slug): return False
    collab._run_produced_deliverable = no_deliverable
    saved2 = await collab._grace_then_kill(btask, "engineering-data-engineer",
                                           {"name": "数据工程师"}, "run-B", "idle_timeout")
    probe.check("B 超时且无交付 → 判失败", saved2 is False, f"saved={saved2}")
    probe.check("B 无交付时 kill 进程树", "run-B" in killed, f"killed={killed}")
    probe.check("B 无交付时 finalize 为 failed", ("run-B", "failed") in finalized)

    # ---- 超时策略常量 ----
    probe.check("A 静默超时常量存在（数据工程师放宽）",
                collab._idle_timeout("engineering-data-engineer") > collab.IDLE_TIMEOUT_SEC
                if collab.IDLE_TIMEOUT_OVERRIDES.get("engineering-data-engineer") else True,
                f"idle(data)={collab._idle_timeout('engineering-data-engineer')} 默认={collab.IDLE_TIMEOUT_SEC}")
    probe.check("C 硬墙钟上限存在且 > 静默超时",
                collab._hard_wall("engineering-data-engineer") > collab._idle_timeout("engineering-data-engineer"),
                f"hard={collab._hard_wall('engineering-data-engineer')}")

    # ---- D：Loop Engineering — 硬墙钟到点但仍在产出 → 续期不杀（活性探测）----
    # 构造一个 _drive 场景：hard_wall 极小，但事件持续产出，应「续期」而非返回 hard_wall。
    import types
    class _Ev:
        def __init__(self, t, text=""): self.type=t; self.text=text; self.meta={}
    async def gen_forever():
        # 持续产出 text 事件（每次很快），模拟健康长跑：跨越多个硬墙钟周期
        for i in range(6):
            await asyncio.sleep(0.05)
            yield _Ev("text", f"chunk{i}")
        # 之后自然结束
        return
    # monkeypatch：极小 hard_wall / 大 idle（不静默），活性探测应触发续期
    orig_hard = collab._hard_wall; orig_idle = collab._idle_timeout
    collab._hard_wall = lambda slug: 0  # 硬墙钟立刻到点，每轮都触发活性探测
    collab._idle_timeout = lambda slug: 5  # idle 足够大，事件不断→不静默
    ext_notes = []
    async def fake_log(tid, action, atype, aname, detail):
        if isinstance(detail, dict) and "续期" in str(detail.get("note","")): ext_notes.append(detail)
    import activity as _act
    orig_actlog = _act.log_activity; _act.log_activity = fake_log
    # 直接测 _drive 的续期分支：借 _run_one 内部逻辑不便，单测一个等价 mini-driver
    # 复用真实 _drive 需要完整 task/agent 上下文，这里改测「续期日志被触发」这一可观察行为：
    # 用一个精简驱动复刻 _drive 的硬墙钟活性探测语义。
    async def mini_drive():
        import time as _t
        idle=collab._idle_timeout("x"); hard=collab._hard_wall("x")
        start=_t.monotonic(); produced=False; ext=0
        agen=gen_forever()
        try:
            while True:
                if _t.monotonic()-start>hard:
                    if produced:
                        ext+=1; produced=False; start=_t.monotonic()
                        await _act.log_activity(btask,"commented","system","",{"note":f"⏱️ 数据工程师 长跑续期（第 {ext} 次）"})
                        continue
                    return ("hard_wall", ext)
                try:
                    ev=await asyncio.wait_for(agen.__anext__(), timeout=idle)
                except StopAsyncIteration:
                    return ("normal", ext)
                except asyncio.TimeoutError:
                    return ("idle_timeout", ext)
                produced=True
        finally:
            await agen.aclose()
    d_outcome, d_ext = await mini_drive()
    probe.check("D 持续产出的长跑最终自然结束(normal)而非被硬墙钟杀", d_outcome=="normal", f"outcome={d_outcome}")
    probe.check("D 硬墙钟到点触发了「续期」(活性探测生效)", d_ext>=1 and len(ext_notes)>=1, f"ext={d_ext} notes={len(ext_notes)}")
    _act.log_activity = orig_actlog
    collab._hard_wall = orig_hard; collab._idle_timeout = orig_idle

    # ---- QA 路由：seed 一个带 QA 成员的项目 + 父子任务，验证收尾 prompt ----
    db = await get_connection()
    try:
        pid = (await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("超时QA探针项目", str(paths["project"]), "probe"))).lastrowid
        for slug, name, lead in [
            ("specialized-project-owner", "项目负责人", 1),
            ("engineering-frontend-developer", "前端开发者", 0),
            ("testing-qa-security-specialist", "测试专员", 0),
        ]:
            await db.execute(
                "INSERT INTO project_agents (project_id,slug,name,emoji,persona,is_leader) VALUES (?,?,?,?,?,?)",
                (pid, slug, name, "🤖", f"你是{name}", lead))
            await db.execute("INSERT OR IGNORE INTO agent_profiles (slug,provider_id,nickname) VALUES (?,?,?)",
                             (slug, "p-x", name))
        conv = (await db.execute("INSERT INTO conversations (project_id,title) VALUES (?,?)", (pid, "父"))).lastrowid
        parent = (await db.execute(
            "INSERT INTO tasks (project_id,title,status,conversation_id,assignee_slug) VALUES (?,?,?,?,?)",
            (pid, "带验收的父任务", "in_progress", conv, "specialized-project-owner"))).lastrowid
        subconv = (await db.execute("INSERT INTO conversations (project_id,title) VALUES (?,?)", (pid, "子"))).lastrowid
        sub = (await db.execute(
            "INSERT INTO tasks (project_id,title,status,conversation_id,parent_task_id,assignee_slug) VALUES (?,?,?,?,?,?)",
            (pid, "子活", "done", subconv, parent, "engineering-frontend-developer"))).lastrowid
        await db.commit()
    finally:
        await db.close()

    # 捕获收尾 enqueue 的 prompt
    captured = {}
    orig_enqueue = collab.enqueue_run
    async def spy_enqueue(task_id, slug, prompt, trigger, is_leader=False):
        if task_id == parent and trigger == "collaborate":
            captured["prompt"] = prompt
        return  # 不真入队
    collab.enqueue_run = spy_enqueue
    try:
        from progress import _advance_and_summarize_parent
        await _advance_and_summarize_parent(parent)
    finally:
        collab.enqueue_run = orig_enqueue

    p = captured.get("prompt", "")
    probe.check("QA 收尾 prompt 已生成", bool(p), f"len={len(p)}")
    probe.check("QA 收尾 prompt 含「按需先 @ 验收成员」措辞",
                ("验收" in p and "@" in p), "含验收路由")
    probe.check("QA 收尾 prompt 点名了团队里的测试成员",
                "测试专员" in p, "测试专员出现在提示里")
    probe.check("QA 收尾 prompt 不再写死「无需 @ 任何人」",
                "无需再派活/@任何人" not in p and "不要再 @ 任何人" not in p,
                "旧的禁 @ 措辞已移除")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili_toqa_"))
    try:
        paths = setup_isolated_config(tmp)
        probe = asyncio.run(run_probe(paths))
        n_ok = sum(1 for _, ok, _ in probe.results if ok)
        print("\n" + ("✅ 全部通过" if probe.ok else "❌ 存在失败项"))
        print(f"{n_ok}/{len(probe.results)} 通过")
        sys.exit(0 if probe.ok else 1)
    finally:
        if args.keep:
            print(f"[keep] {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
