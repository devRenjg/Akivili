"""Akivili mention-prompt probe — @ 触发把发言原话作为指令传给成员 + 历史双限。

在隔离环境（临时 config/DB/workspace，绝不碰真实 jianagency.db）验证 task140 事故修复：
  1. @ 成员入队时 run_queue.prompt 非空、且含发言原话（修 Bug A：此前硬传空串丢失指令）。
  2. prompt 明示「需要读文件/启动服务就正常去做」，不含「不要读任何文件」绝对禁令（修 Bug B）。
  3. 多人 @ 各自都拿到含原话的 prompt。
  4. _clip_history 双限：条数上限 + 字符预算上限（防上下文撑爆/幻觉），至少保留最新 1 条。

不触发真实 CLI/LLM。跑完清理临时目录（除非 --keep）。
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
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def seed(paths: dict) -> tuple[int, int, dict]:
    """建隔离项目 + 负责人 + 2 名成员（含昵称）+ 一个顶层任务。返回 (pid, task_id, {slug:nickname})。"""
    import agents as agents_mod  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__mention_probe__", str(paths["project"]), "mention prompt probe"))
        pid = cur.lastrowid
        roster = [
            ("specialized-project-owner", "项目负责人", "星", 1),
            ("engineering-backend-architect", "后端架构师", "卡芙卡", 0),
            ("engineering-frontend-developer", "前端开发者", "花火", 0),
        ]
        nick = {}
        for slug, name, nickname, is_leader in roster:
            await db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) "
                "VALUES (?,?,?,?,?,?)", (pid, slug, name, "🧩", f"你是{name}。", is_leader))
            await db.execute(
                "INSERT INTO agent_profiles (slug, provider_id, nickname) VALUES (?,?,?) "
                "ON CONFLICT(slug) DO UPDATE SET nickname=excluded.nickname",
                (slug, "", nickname))
            nick[slug] = nickname
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority, assignee_slug) "
            "VALUES (?,?,?,?,?,?)",
            (pid, "启动服务并提交代码", "项目已写好，需启动服务给出访问地址并提交 Git", "in_progress", "none",
             "specialized-project-owner"))
        task_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    return pid, task_id, nick


async def _prompts_for(task_id: int) -> dict:
    """取该任务各 mention run 的 (agent_slug -> prompt)。"""
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT agent_slug, prompt FROM run_queue WHERE task_id=? AND trigger='mention' ORDER BY id",
            (task_id,))).fetchall()
    finally:
        await db.close()
    return {r["agent_slug"]: (r["prompt"] or "") for r in rows}


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    await bootstrap_backend(paths)
    import collab  # noqa: PLC0415
    from executor import runner  # noqa: PLC0415

    pid, tid, nick = await seed(paths)

    # ---- Test 1-3: @ 触发把发言原话作为 prompt 传给成员 ----
    utterance = "@卡芙卡 @花火 项目写好了你们把服务启动起来，告诉我服务访问地址，带 IP 的那种，然后代码交付物提交到 Git"
    triggered = await collab.parse_and_enqueue_mentions(
        tid, pid, utterance, author_slug="specialized-project-owner",
        leader_slug="specialized-project-owner", source_run_id=999)
    prompts = await _prompts_for(tid)

    probe.check("@ 的两名成员都被入队", set(triggered) >= {"engineering-backend-architect", "engineering-frontend-developer"},
                f"triggered={triggered}")
    kf = prompts.get("engineering-backend-architect", "")
    hh = prompts.get("engineering-frontend-developer", "")
    probe.check("卡芙卡 run 的 prompt 非空（修 Bug A：不再传空串）", len(kf) > 0, f"prompt_len={len(kf)}")
    probe.check("prompt 含发言原话（成员收到真实指令，而非通用模板）",
                "启动" in kf and "Git" in kf, f"含'启动'+'Git'={'启动' in kf and 'Git' in kf}")
    probe.check("prompt 含任务上下文（标题）", "启动服务并提交代码" in kf, "标题已带入")
    probe.check("花火 run 同样拿到含原话的 prompt", "启动" in hh and len(hh) > 0, f"prompt_len={len(hh)}")

    # ---- Test 2 修 Bug B：不含「不要读任何文件」绝对禁令，且明示需要就正常读 ----
    probe.check("prompt 不含『不要读任何文件』绝对禁令（修 Bug B）",
                "不要读任何文件" not in kf, "无绝对禁令")
    probe.check("prompt 明示需要读文件/启动服务就正常做",
                ("读项目文件" in kf or "启动服务" in kf) and "正常去做" in kf, "含许可措辞")

    # ---- Test 4: _clip_history 双限 ----
    # 条数限：造 30 条短消息，限 20 → 只留最近 20
    runner._HISTORY_MAX_MSGS = 20
    runner._HISTORY_MAX_CHARS = 12000
    many = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    clipped = runner._clip_history(many)
    probe.check("历史条数限：30 条裁到最近 20", len(clipped) == 20 and clipped[-1]["content"] == "m29",
                f"len={len(clipped)} last={clipped[-1]['content']}")

    # 字符预算限：造少量超长消息，条数没超但字符超预算 → 从最早侧丢，至少留最新 1 条
    runner._HISTORY_MAX_CHARS = 1000
    big = [{"role": "user", "content": "X" * 600} for _ in range(5)]  # 5*600=3000 > 1000
    clipped2 = runner._clip_history(big)
    total_chars = sum(len(m["content"]) for m in clipped2)
    probe.check("历史字符预算限：超预算从最早侧丢（总字符≤预算+单条）",
                len(clipped2) < 5 and total_chars <= 1000 + 600, f"kept={len(clipped2)} chars={total_chars}")
    # 单条即超预算时至少保留最新 1 条（不返回空）
    huge = [{"role": "user", "content": "Y" * 5000}]
    clipped3 = runner._clip_history(huge)
    probe.check("单条超预算仍至少保留最新 1 条（不空历史）", len(clipped3) == 1, f"kept={len(clipped3)}")

    # ---- Test 5: _apply_history_limits 从 Settings 生效 ----
    import config as config_mod  # noqa: PLC0415
    orig = config_mod.load_settings

    class _FS:
        history_max_msgs = 5
        history_max_chars = 800
    config_mod.load_settings = lambda: _FS()
    try:
        runner._apply_history_limits()
        probe.check("历史双限从 Settings 生效",
                    runner._HISTORY_MAX_MSGS == 5 and runner._HISTORY_MAX_CHARS == 800,
                    f"msgs={runner._HISTORY_MAX_MSGS} chars={runner._HISTORY_MAX_CHARS}")
    finally:
        config_mod.load_settings = orig

    # ---- Test 6: @昵称触发；只写名字不带 @ 不触发（task140/#451 停链治本口径）----
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority, assignee_slug) "
            "VALUES (?,?,?,?,?,?)",
            (pid, "收口指令", "继续收口", "in_progress", "none", "specialized-project-owner"))
        tid2 = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    # 6a: @昵称（卡芙卡）应触发后端架构师
    trig_nick = await collab.parse_and_enqueue_mentions(
        tid2, pid, "@卡芙卡 补映射后重出终版包", author_slug="specialized-project-owner",
        leader_slug="specialized-project-owner", source_run_id=1001)
    probe.check("@昵称（卡芙卡）触发对应成员（后端架构师）",
                "engineering-backend-architect" in trig_nick, f"triggered={trig_nick}")

    # 6b: 只写名字不带 @（#451 停链真因）→ 不触发任何人
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO tasks (project_id, title, description, status, priority, assignee_slug) "
            "VALUES (?,?,?,?,?,?)",
            (pid, "喊名字没@", "", "in_progress", "none", "specialized-project-owner"))
        tid3 = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    trig_bare = await collab.parse_and_enqueue_mentions(
        tid3, pid, "卡芙卡，你把终版包重出一下", author_slug="specialized-project-owner",
        leader_slug="specialized-project-owner", source_run_id=1002)
    probe.check("只写名字不带 @（#451 停链真因）→ 不触发任何人",
                trig_bare == [], f"triggered={trig_bare}（应为空）")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-mention-"))
    paths = setup_isolated_config(tmp)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")
    total = len(probe.results)
    passed = sum(1 for r in probe.results if r[1])
    print(f"\n=== mention prompt probe: {passed}/{total} ===")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
