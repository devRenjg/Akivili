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
    """收集本任务（含所有子任务）里**真正产出过东西**的角色。

    口径 = 跑过 run 的成员（task_runs）∪ 在本任务/子任务会话里留下过 assistant 发言的成员
    （messages.author_slug）。后者覆盖「直接建子任务卡片（jian subtask --body-file）」这类
    有真实产出但不产生 task_run 的路径——干了活就该有沉淀，不能只认执行型 run。
    与下游 _task_context 取产出的口径（同样按 messages）一致，无产出者在 _reflect_one 里
    context 为空自动跳过（返回 0）。去重返回 [{slug, name, provider_id}]。
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
        # 有本人 assistant 发言的成员（含直接建卡型产出，无 run）
        says = await (await db.execute(
            f"""SELECT DISTINCT m.author_slug FROM messages m
                JOIN tasks tk ON tk.conversation_id=m.conversation_id
                WHERE tk.id IN ({ph}) AND m.role='assistant' AND m.author_slug<>''""",
            task_ids)).fetchall()
        slugs = list(dict.fromkeys(  # 保序去重：run 优先，再补发言型
            [r["agent_slug"] for r in runs] + [r["author_slug"] for r in says]))
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


_TASK_TAG_RE = None


def _bullet_text(bullet: str) -> str:
    """剥掉条目尾部的任务归属标记 <!-- akivili:task:ID -->，返回纯经验正文（用于去重/压缩/喂模型）。
    ID 兼容非数字历史标记（如 liuying-2024q1），用 \\S+ 而非 \\d+。"""
    import re
    return re.sub(r"\s*<!--\s*akivili:task:\S+\s*-->\s*$", "", bullet).strip()


def _bullet_tag(bullet: str) -> str:
    """取条目尾部的任务归属标记（含注释符），无则返回空串。"""
    import re
    m = re.search(r"<!--\s*akivili:task:\S+\s*-->\s*$", bullet.strip())
    return m.group(0) if m else ""


def _inherit_tag(compacted_text: str, existing_bullets: list[str], fallback_marker: str) -> str:
    """压缩后的一条经验，回溯继承其最相似源条目的原始 task 标记，保住血缘。
    匹配不上（真正新合成/大幅改写）才用 fallback（当前任务标记）。"""
    import difflib
    ct = _bullet_text(compacted_text)
    best_ratio, best_tag = 0.0, ""
    for eb in existing_bullets:
        ratio = difflib.SequenceMatcher(None, ct, _bullet_text(eb)).ratio()
        if ratio > best_ratio:
            best_ratio, best_tag = ratio, _bullet_tag(eb)
    # 阈值 0.6：足够相似才判为「同一条经验的压缩版」，继承其原标记
    if best_ratio >= 0.6 and best_tag:
        return best_tag
    return fallback_marker


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

请复盘，提炼**对你以后同类工作最有价值**的经验/教训/Know-how（最多 {n} 条，宁缺毋滥）。要求：
- 每条一行，以 `- ` 开头，一句话，具体、可操作。
- 聚焦你**本专业领域**的「方法、坑、诀窍、判断依据」，例如工具用法、数据口径、易错点、提效技巧、协作方式。
- **不要**写这次任务的结论/数字/结果本身（那是归档，不是经验）。
- 只输出条目，不要任何前言、编号、解释、总结。

🔴 **质量门槛（重要）**：只有当你**确实学到了本专业领域的新方法、新坑或新诀窍**时才写。
如果这次只是常规的沟通/介绍/自我描述/汇报/走流程类任务，没有可迁移到未来工作的专业增量，
**就只回一个字：无**。别为凑数写放之四海皆准的空话（如"用固定结构表达""锚定项目场景"这类通用套话）。"""

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

    # 超上限 → 让模型压缩合并。喂给模型的是纯正文；压缩后**按相似度回溯继承原标记**保住血缘，
    # 只有真正新合成/大幅改写、匹配不上任何源条目的才归当前任务（marker）。
    # （旧实现把压缩结果统一贴当前任务标记，会抹平血缘 → 度量层 knowhow 复用率无法追溯来源。）
    if len(merged) > KNOWHOW_MAX:
        merged_before = list(merged)   # 保留带标记的源条目，供回溯匹配
        bullets_text = "\n".join(f"- {_bullet_text(b)}" for b in merged)
        compacted = await runner.run_oneshot(
            member["provider_id"], sys_prompt,
            COMPACT_PROMPT.format(cap=KNOWHOW_MAX, bullets=bullets_text), timeout_sec=180)
        c = _extract_bullets(compacted)
        if c:
            merged = [f"{_bullet_text(b)} {_inherit_tag(b, merged_before, marker)}"
                      for b in c[:KNOWHOW_MAX]]
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

