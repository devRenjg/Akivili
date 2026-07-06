"""任务完成后的「经验反思」引擎。

设计目标（与「per-run 归档」区分开）：
- per-run 记忆（runner._persist_memory）只是流水式记录「这次干了啥」。
- 本模块在**任务进入 done** 时触发，让每个真正参与执行的角色**用自己的模型复盘**，
  提炼 3-5 条「下次这类活怎么做更好」的 Know-how，写进各自记忆的受管段落。
  这才是「经验沉淀 + Know-how 学习」——不是任务结论的原样存档，而是可迁移的做事要领。

治理：Know-how 存独立受管段落 `<!-- akivili:managed:knowhow ... -->`，
超过上限时再让模型合并去重成 Top-N，避免无限膨胀污染人格上下文。
"""
import asyncio

from database import get_connection
from memory import read_memory, upsert_managed_section
from config import is_test_project

# 单角色 Know-how 条目上限，超过则触发压缩合并
KNOWHOW_MAX = 30
# 每次反思产出的经验条数指引
BULLETS_PER_TASK = "3-5"
# 受管段落 key
KNOWHOW_KEY = "knowhow"


async def _participants(task_id: int) -> list[dict]:
    """收集本任务（含所有子任务）里**真正跑过 run**的角色。
    以 task_runs 为准（有 run = 真的执行过、有产出），去重返回 [{slug, name, provider_id}]。
    """
    db = await get_connection()
    try:
        # 本任务 + 其子任务的所有 id
        rows = await (await db.execute(
            "SELECT id FROM tasks WHERE id=? OR parent_task_id=?", (task_id, task_id))).fetchall()
        task_ids = [r["id"] for r in rows]
        if not task_ids:
            return []
        ph = ",".join("?" for _ in task_ids)
        runs = await (await db.execute(
            f"SELECT DISTINCT agent_slug FROM task_runs WHERE task_id IN ({ph}) AND agent_slug<>''",
            task_ids)).fetchall()
        slugs = [r["agent_slug"] for r in runs]
        if not slugs:
            return []
        # 取项目内这些角色的展示名 + 生效模型
        prow = await (await db.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,))).fetchone()
        project_id = prow["project_id"] if prow else 0
        out = []
        for slug in slugs:
            a = await (await db.execute(
                """SELECT pa.slug, pa.name, pa.persona, p.provider_id AS provider_id, p.nickname AS nickname
                   FROM project_agents pa LEFT JOIN agent_profiles p ON p.slug=pa.slug
                   WHERE pa.project_id=? AND pa.slug=? LIMIT 1""", (project_id, slug))).fetchone()
            if a and a["provider_id"]:
                out.append(dict(a))
        return out
    finally:
        await db.close()


async def _task_context(task_id: int, slug: str) -> str:
    """给某角色复盘用的上下文：任务标题/描述 + 本任务及子任务里该角色的发言/产出。"""
    db = await get_connection()
    try:
        t = await (await db.execute(
            "SELECT title, description, conversation_id FROM tasks WHERE id=?", (task_id,))).fetchone()
        if not t:
            return ""
        parts = [f"任务：{t['title']}"]
        if t["description"]:
            parts.append(f"任务描述：{t['description']}")
        # 该角色在本任务 + 子任务里的发言（真实产出）
        rows = await (await db.execute(
            """SELECT m.content FROM messages m
               JOIN tasks tk ON tk.conversation_id=m.conversation_id
               WHERE (tk.id=? OR tk.parent_task_id=?) AND m.role='assistant'
                     AND (m.author_slug=? OR m.author_slug='')
               ORDER BY m.id""", (task_id, task_id, slug))).fetchall()
        says = [r["content"].strip() for r in rows if (r["content"] or "").strip()]
        if says:
            parts.append("你在本任务中的产出/发言：\n" + "\n---\n".join(s[:1500] for s in says))
        return "\n\n".join(parts)
    finally:
        await db.close()


def _extract_bullets(section_body: str) -> list[str]:
    """从受管段落正文里抽出已有的 know-how 条目（以 - 开头的行）。保留行内任务标记。"""
    out = []
    for ln in (section_body or "").splitlines():
        s = ln.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
    return out


def _bullet_text(bullet: str) -> str:
    """剥掉条目尾部的任务归属标记 <!-- akivili:task:ID -->，返回纯经验正文（用于去重/压缩/喂模型）。"""
    import re
    return re.sub(r"\s*<!-- akivili:task:\d+ -->\s*$", "", bullet).strip()


def _current_knowhow(slug: str) -> list[str]:
    """读该角色记忆里现有的 know-how 受管段落条目。"""
    import re
    mem = read_memory(slug)
    start = f"<!-- akivili:managed:{KNOWHOW_KEY}:start -->"
    end = f"<!-- akivili:managed:{KNOWHOW_KEY}:end -->"
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), mem, re.DOTALL)
    return _extract_bullets(m.group(1)) if m else []


REFLECT_SYSTEM = """你正在进行一次「任务复盘」。你是 {who}，你的职责设定如下：

{persona}

复盘的目的：从这次任务里提炼出**可迁移的经验与 Know-how**，写进你自己的长期记忆，
让你以后做同类工作更快更好。不要复述任务结论本身，要提炼**做事要领**。"""

REFLECT_PROMPT = """下面是你刚完成的一次任务及你的产出：

{context}

请复盘，提炼 {n} 条**对你以后同类工作最有价值**的经验/教训/Know-how。要求：
- 每条一行，以 `- ` 开头，一句话，具体、可操作。
- 聚焦「方法、坑、诀窍、判断依据」，例如工具用法、数据口径、易错点、提效技巧、协作方式。
- **不要**写这次任务的结论/数字/结果本身（那是归档，不是经验）。
- 只输出这 {n} 条，不要任何前言、编号、解释、总结。
如果这次任务实在没有值得沉淀的新经验，只回一个词：无。"""

COMPACT_PROMPT = """以下是你长期积累的工作 Know-how 条目，现在偏多、有重复或过时。
请合并去重、保留最有价值的，压缩到 **{cap} 条以内**：

{bullets}

要求：每条一行以 `- ` 开头，一句话；合并同类、删冗余、留精华；只输出条目本身，无前言无编号。"""


async def _reflect_one(task_id: int, member: dict) -> int:
    """让单个角色对本任务复盘，把新 Know-how 合并进其受管段落。返回新增条数。"""
    from executor import runner
    slug = member["slug"]
    who = (member.get("nickname") or "").strip()
    who = f"{who}（{member['name']}）" if who else member["name"]
    context = await _task_context(task_id, slug)
    if not context:
        return 0
    sys_prompt = REFLECT_SYSTEM.format(who=who, persona=(member.get("persona") or "")[:2000])
    user_prompt = REFLECT_PROMPT.format(context=context[:6000], n=BULLETS_PER_TASK)
    text = await runner.run_oneshot(member["provider_id"], sys_prompt, user_prompt, timeout_sec=180)
    new_bullets = _extract_bullets(text)
    # 「无」或空 → 本次无沉淀
    if not new_bullets or (len(new_bullets) == 0 and text.strip() in ("无", "无。")):
        return 0

    from memory import task_marker
    marker = task_marker(task_id)
    existing = _current_knowhow(slug)
    # 去重（按去空白后的“正文”，即剥掉尾部任务标记后比较）合并；
    # 新条目带上本任务标记，供任务删除时精准清理。
    seen = {_bullet_text(b) for b in existing}
    merged = existing + [f"{b.strip()} {marker}" for b in new_bullets
                         if b.strip() and _bullet_text(b) not in seen]

    # 超上限 → 让模型压缩合并（喂给模型的是纯正文，压缩后条目视为源自本任务、统一带本任务标记）
    if len(merged) > KNOWHOW_MAX:
        bullets_text = "\n".join(f"- {_bullet_text(b)}" for b in merged)
        compacted = await runner.run_oneshot(
            member["provider_id"], sys_prompt,
            COMPACT_PROMPT.format(cap=KNOWHOW_MAX, bullets=bullets_text), timeout_sec=180)
        c = _extract_bullets(compacted)
        if c:
            merged = [f"{b} {marker}" for b in c[:KNOWHOW_MAX]]
        else:
            merged = merged[-KNOWHOW_MAX:]   # 压缩失败兜底：留最近的（保留其原标记）

    body = ("## 🧠 工作经验与 Know-how（做同类任务前先看）\n\n"
            + "\n".join(f"- {b}" for b in merged))
    upsert_managed_section(slug, KNOWHOW_KEY, body)
    return len(new_bullets)


async def reflect_on_task_done(task_id: int) -> None:
    """任务进入 done 时调用：所有真正参与执行的角色各自复盘、沉淀 Know-how。
    fire-and-forget 友好——异常吞掉，绝不影响任务状态流转。测试项目跳过。"""
    try:
        db = await get_connection()
        try:
            t = await (await db.execute("SELECT title FROM tasks WHERE id=?", (task_id,))).fetchone()
        finally:
            await db.close()
        if not t or is_test_project(t["title"] or ""):
            return
        members = await _participants(task_id)
        if not members:
            return
        from activity import log_activity
        # 并发让各角色复盘（互不依赖），单个失败不拖累其他
        results = await asyncio.gather(
            *(_reflect_one(task_id, m) for m in members), return_exceptions=True)
        learned = [m for m, r in zip(members, results) if isinstance(r, int) and r > 0]
        if learned:
            names = "、".join((m.get("nickname") or m["name"]) for m in learned)
            await log_activity(task_id, "commented", "system", "",
                               {"note": f"✅ 任务完成，{names} 已经沉淀本次经验"})
    except Exception:  # noqa: BLE001
        pass

