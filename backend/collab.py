"""多 Agent 协同引擎：队列 + asyncio 后台循环（串行单并发）+ @mention 解析 + 防死循环。

设计思路：
- @mention 是 Agent 间唯一通信原语：发言里 @成员 → 给该成员入队一个 run。
- Team Leader 执行时被注入「协作协议 + 团队花名册」，由 LLM 决策派给谁。
- 后台循环串行领取 run_queue 里的 queued 任务执行。
- 防死循环：pending 去重 + Leader 自触发守卫 + 单任务协同深度上限。
"""
import asyncio
import re

from database import get_connection

# 单个任务的协同累计运行上限，防失控烧钱
MAX_RUNS_PER_TASK = 20

# —— 执行超时策略（A 静默超时 + B 保成果 + 硬墙钟兜底）——
#
# 旧策略是「固定墙钟超时」：不管在不在干活，到点就 kill+标 failed。对慢取数角色（数据工程师
# 经 Narya/ingest 遍历全库，单轮真要 1h+）是误伤——真在产出却被切、且已完成的成果被销毁。
#
# 新策略：
#  A) 静默超时（idle）：只要 Agent **持续有输出**（stdout/工具事件）就不算超时；只有**连续
#     IDLE_TIMEOUT_SEC 无任何事件**（真卡死/僵死）才判超时。慢但在干活的任务永不被误杀。
#  B) 超时保成果：判超时后，先给 GRACE_SEC 宽限，尝试让 Agent 自己收尾（jian comment/status）
#     落库已完成的成果，宽限内正常结束就算成功；宽限仍无动静才 kill。
#  C) 硬墙钟兜底（HARD_WALL_SEC）：防极端失控（真死循环狂刷日志既不静默也不结束），设一个
#     总时长天花板，到顶无条件终止。
#
# 均为模块全局，便于测试 monkeypatch 调小。

# 静默超时：连续多久无输出事件判为卡死（默认 15 分钟）
IDLE_TIMEOUT_SEC = 900
# 按角色覆盖静默超时：数据类角色单个取数脚本可能 sleep 轮询较久，放宽到 30 分钟
IDLE_TIMEOUT_OVERRIDES = {
    "engineering-data-engineer": 1800,
}
# 判超时后给 Agent 的收尾宽限（默认 90 秒）：让它落库已完成的成果
GRACE_SEC = 90
# 硬墙钟总上限：无条件终止的天花板（默认 3 小时）
HARD_WALL_SEC = 10800
# 数据类等长跑角色的硬墙钟（默认 4 小时）
HARD_WALL_OVERRIDES = {
    "engineering-data-engineer": 14400,
}


def _idle_timeout(slug: str) -> int:
    """该角色的静默超时秒数（运行时读模块全局，便于测试 monkeypatch）。"""
    return IDLE_TIMEOUT_OVERRIDES.get(slug, IDLE_TIMEOUT_SEC)


def _hard_wall(slug: str) -> int:
    """该角色的硬墙钟总上限秒数。"""
    return HARD_WALL_OVERRIDES.get(slug, HARD_WALL_SEC)


async def _run_produced_deliverable(task_id: int, slug: str) -> bool:
    """该 run 是否已产出真实交付：本任务会话里有该 Agent 的 assistant 发言（jian comment/subtask），
    或它已把任务状态改动过（jian status）。用于「超时保成果」：已交付就不该判失败销毁。"""
    from database import get_connection
    db = await get_connection()
    try:
        row = await (await db.execute(
            """SELECT 1 FROM messages m JOIN tasks t ON t.conversation_id=m.conversation_id
               WHERE t.id=? AND m.role='assistant' AND m.author_slug=? LIMIT 1""",
            (task_id, slug))).fetchone()
        if row:
            return True
        act = await (await db.execute(
            "SELECT 1 FROM activities WHERE task_id=? AND actor_type='agent' AND actor_name=? "
            "AND action IN ('commented','status_changed') LIMIT 1", (task_id, slug))).fetchone()
        return bool(act)
    finally:
        await db.close()


async def _grace_then_kill(task_id: int, slug: str, agent: dict,
                           run_id, outcome: str) -> bool:
    """判超时后的收尾（B 保成果）：给 GRACE_SEC 宽限让 Agent 自己落库成果；
    宽限内产出了真实交付则视为成功（保成果、不销毁），否则 kill 进程树 + 落 failed。

    返回 True=已保住成果（run 记 done）、False=真失败（已 kill + finalize failed）。

    注（Loop Engineering）：新版 _drive 已把「硬墙钟到点但仍在产出」的健康长任务挡在前面
    （续期继续跑，不进本函数）。故能走到这里的 outcome 基本是：idle_timeout（连续静默、
    疑似卡死），或 hard_wall 且此刻确已静默。宽限是给「刚好在收尾、马上要落库」的最后机会；
    宽限内仍无交付才判卡死 kill。调用方在 saved=True 时会补 on_execution_complete 推进任务状态。
    """
    from activity import log_activity
    from executor import runner
    # 宽限：分几次轮询，任何一次检测到已产出交付就提前收尾
    waited = 0
    step = 15
    while waited < GRACE_SEC:
        await asyncio.sleep(min(step, GRACE_SEC - waited))
        waited += step
        if await _run_produced_deliverable(task_id, slug):
            # Agent 在宽限内交付了 —— 保成果。让其自然收尾落库终态；这里补记一条说明。
            if run_id:
                await runner.finalize_run(run_id, "succeeded")
            await log_activity(task_id, "commented", "system", "",
                               {"note": f"⏱️ {agent.get('name', slug)} 执行较久但已产出交付，"
                                        f"按完成处理（超时保护：宽限内检测到成果）。"})
            return True
    # 宽限仍无交付 → 真判失败：kill 进程树 + 落 failed（杜绝孤儿 running）
    if run_id:
        runner.kill_run(run_id)
        await runner.finalize_run(run_id, "failed")
    reason = ("连续无输出超时（疑似卡死）" if outcome == "idle_timeout"
              else "超过硬性时长上限")
    await log_activity(task_id, "task_failed", "agent", agent.get("name", slug),
                       {"reason": f"执行终止：{reason}，且宽限内无成果产出"})
    return False

_loop_started = False


# ---------- 协作协议（注入 Leader 系统提示） ----------

TEAM_LEADER_PROTOCOL = """## 团队负责人协作协议

你是本任务的**负责人（Owner）**，对结果负责。你被唤醒 = 该你决策了。

### 🚫 四条铁律（违反即失败）

1. **绝不 @ 你自己、绝不委派给自己**。你的发言里**不许出现指向你自己的 @**。你被唤醒就是让你决策，不是让你喊自己。
2. **只能 @ 下方「团队花名册」里真实列出的成员**，用花名册里 `委派用 @xxx` 给出的**确切名字**。**绝不允许编造、虚构、想象任何不在花名册里的成员**（不要凭领域知识杜撰角色名）。花名册里有谁，你才能点谁。
3. 需要成员出力时，**必须真的通过下面的方式点名/建卡**，让系统真正唤醒他们；不要只在文字里描述"团队有谁"就当作完成了。
4. **只能用 `jian` 命令在平台上操作**。**绝不用 Bash/脚本/SQL 直接读写数据库、绝不用你内置的任务/待办/子代理工具（TaskCreate、TodoWrite、内置 Agent 等）来"模拟"建卡或成员**。你替成员写的内容不是他本人的产出——要成员真正干活，只能 `jian subtask --owner <成员名> --assign` 让平台唤醒他本人。

### 怎么做

先判断这件事该怎么完成：

**A. 你自己就能答/能做的**（简单问答、你职责范围内）：直接做、直接答，不 @ 任何人。做完即结束。

**B. 需要团队成员出力的**，从两条里选一条（同一件事只用一条）：

- **B1 讨论/协作型**：用 `jian comment "…@成员名…"` 点名花名册里的真实成员，他们会在本任务里回复。

- **B2 要各自产出成果型**（如"每个成员各自做自我介绍/各自负责一块"）：**给花名册里每一个成员各建一张子任务并指派给他**：
  `jian subtask --title "XX（成员名）的自我介绍" --owner <成员名> --assign --body-file 说明.md`
  这会建一张 Owner=该成员的卡片并**自动触发他在自己的卡片里完成**。**花名册里除你以外的每个成员都要建一张**，一个都不能漏，也不能只给一个人。`--owner` 用花名册里 `委派用 @xxx` 的名字。

**C. 重新被唤醒时**：读新进展。还有成员没完成就继续等；全部完成后写一段**总结收尾**（`jian comment` 汇总各成员成果），**不 @ 任何人**，任务结束。

### 再次强调
- 点名/建卡只认花名册里的真实成员名。杜撰的名字系统会忽略、没人会被唤醒，等于没做。
- 收尾总结不 @ 任何人（否则造成循环）。
- 能自己答的别硬委派；要各自产出就给**每个**真实成员各建一张子任务。
"""


async def _member_names(project_id: int, exclude_slug: str = "") -> list[str]:
    """项目里真实成员的 @ 名字（昵称优先，回退 name），可排除某人（如负责人自己）。
    用于把真实花名册直接内联进 Leader 的 prompt，避免它自行探索/臆想成员。"""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            """SELECT pa.slug, pa.name, p.nickname AS nickname
               FROM project_agents pa LEFT JOIN agent_profiles p ON p.slug = pa.slug
               WHERE pa.project_id=? ORDER BY pa.is_leader DESC, pa.id""",
            (project_id,))).fetchall()
    finally:
        await db.close()
    names = []
    for r in rows:
        if r["slug"] == exclude_slug:
            continue
        names.append((r["nickname"] or r["name"] or "").strip())
    return [n for n in names if n]


async def build_roster(project_id: int, viewer_slug: str = "") -> str:
    """生成团队花名册：项目（= 一个 Team）全员的角色/一句话职责/技能 + @语法。
    每个成员执行时都注入此花名册——让整个 Team 互相认识（不只负责人）。
    viewer_slug 标出"就是你自己"，动态查库，组织变动即最新。"""
    db = await get_connection()
    try:
        prow = await (await db.execute(
            "SELECT title FROM projects WHERE id=?", (project_id,))).fetchone()
        team_name = (prow["title"] if prow else "") or "本项目"
        members = await (await db.execute(
            """SELECT pa.slug, pa.name, pa.emoji, pa.persona, pa.is_leader, p.nickname AS nickname
               FROM project_agents pa LEFT JOIN agent_profiles p ON p.slug = pa.slug
               WHERE pa.project_id=? ORDER BY pa.is_leader DESC, pa.id""",
            (project_id,))).fetchall()
        skills_map = {}
        for m in members:
            rows = await (await db.execute(
                "SELECT s.name FROM agent_skills a JOIN skills s ON s.slug=a.skill_slug WHERE a.agent_slug=?",
                (m["slug"],))).fetchall()
            skills_map[m["slug"]] = [r["name"] for r in rows]
    finally:
        await db.close()

    def profile_summary(persona: str) -> str:
        """从 persona 提取专业摘要：跳过标题行，取头几句实质内容（便于负责人按专业匹配派活）。"""
        picked = []
        for ln in (persona or "").splitlines():
            s = ln.strip()
            if not s or s.startswith("---") or s.startswith("#"):
                continue
            # 去掉列表/强调符号
            s = s.lstrip("-*•").replace("**", "").strip()
            if s:
                picked.append(s)
            if len(picked) >= 2:      # 取前两句实质行
                break
        text = " ".join(picked)
        return (text[:120] + "…") if len(text) > 120 else (text or "（暂无职责说明）")

    def display(m):
        nick = (m["nickname"] or "").strip()
        return f"{nick}（{m['name']}）" if nick else m["name"]

    lines = [
        f"## 你的团队：{team_name}",
        "",
        f"你是「{team_name}」这个项目团队的一员。以下是你**完整的团队成员**（这就是本项目的全部团队，"
        f"不存在其它成员，也不需要去别处查找）。派活/协作时按每个人的**专长**匹配：",
        "",
    ]
    for m in members:
        who = display(m)
        emoji = m["emoji"] or ("🧭" if m["is_leader"] else "🤖")
        role = "负责人 Leader" if m["is_leader"] else "成员"
        sk = "、".join(skills_map.get(m["slug"], [])) or "无标注技能"
        tag = "（← 就是你自己）" if m["slug"] == viewer_slug else ""
        lines.append(f"- {emoji} **{who}** — {role}{tag}")
        lines.append(f"  专长：{profile_summary(m['persona'])}")
        lines.append(f"  技能：{sk}　委派/点名用 `@{m['name']}`")
    if len(members) <= 1:
        lines.append("（团队暂无其他成员）")
    return "\n".join(lines)


# ---------- 入队 / 去重 / 深度上限 ----------

async def _run_count(task_id: int) -> int:
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT COUNT(*) c FROM run_queue WHERE task_id=?", (task_id,))).fetchone()
        return row["c"] if row else 0
    finally:
        await db.close()


async def enqueue_run(task_id: int, agent_slug: str, prompt: str,
                      trigger: str = "mention", is_leader: bool = False) -> int | None:
    """入队一个待执行 run。pending 去重 + 深度上限。返回 run_queue id 或 None（被拒）。"""
    if await _run_count(task_id) >= MAX_RUNS_PER_TASK:
        from activity import log_activity
        await log_activity(task_id, "commented", "system", "",
                           {"note": f"协同已达上限（{MAX_RUNS_PER_TASK} 次运行），自动停止以防失控"})
        return None
    db = await get_connection()
    try:
        # pending 去重：同一 (task, agent) 已有 queued/running 就不重复入队
        dup = await (await db.execute(
            "SELECT id FROM run_queue WHERE task_id=? AND agent_slug=? AND status IN ('queued','running')",
            (task_id, agent_slug))).fetchone()
        if dup:
            return None
        cur = await db.execute(
            "INSERT INTO run_queue (task_id, agent_slug, trigger, is_leader, prompt) VALUES (?,?,?,?,?)",
            (task_id, agent_slug, trigger, 1 if is_leader else 0, prompt))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _last_run_was_leader(task_id: int, agent_slug: str) -> bool:
    """该 agent 在此任务上最近一次运行是否以 leader 身份（自触发守卫用）。"""
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT is_leader FROM run_queue WHERE task_id=? AND agent_slug=? AND status='done' ORDER BY id DESC LIMIT 1",
            (task_id, agent_slug))).fetchone()
        return bool(row and row["is_leader"])
    finally:
        await db.close()


# ---------- @mention 解析 ----------

async def parse_and_enqueue_mentions(task_id: int, project_id: int, text: str,
                                     author_slug: str, leader_slug: str) -> list[str]:
    """从发言解析 @成员名，为被 @ 的成员入队。返回被触发的 slug 列表。
    - 绝不入队作者自己（防自触发/自 @）。
    - 只认项目里真实存在的成员；@ 了不存在的名字 → 记一条活动提示（不再静默丢弃）。
    - **子任务内的 @ 不唤醒负责人**：子任务是叶子工作，负责人统筹只在顶层任务发生，
      否则成员在子任务里 @负责人 会让负责人在子任务里再派活、层层派生（cascade）。"""
    if not text or "@" not in text:
        return []
    # 判断本 task 是否子任务（有 parent_task_id）
    db0 = await get_connection()
    try:
        prow = await (await db0.execute(
            "SELECT parent_task_id FROM tasks WHERE id=?", (task_id,))).fetchone()
        in_subtask = bool(prow and prow["parent_task_id"])
    finally:
        await db0.close()
    db = await get_connection()
    try:
        members = await (await db.execute(
            """SELECT pa.slug, pa.name, pa.is_leader, p.nickname AS nickname
               FROM project_agents pa LEFT JOIN agent_profiles p ON p.slug = pa.slug
               WHERE pa.project_id=?""", (project_id,))).fetchall()
    finally:
        await db.close()
    members = [dict(m) for m in members]
    # 按名字长度降序，避免短名误命中
    members.sort(key=lambda x: len(x["name"]), reverse=True)

    triggered = []
    seen = set()
    matched_spans = []  # 记录命中的 @名字，用于事后找出"@ 了但不存在"的
    for m in members:
        # 绝不入队作者自己：防 Leader @ 自己、防成员回触自己造成循环
        if m["slug"] == author_slug or m["slug"] in seen:
            # 作者自己就算被 @ 到，也记为已处理，避免落入"未匹配"告警
            for n in [m["name"]] + ([m["nickname"]] if m.get("nickname") else []):
                if re.search(r"@" + re.escape(n), text):
                    matched_spans.append(n)
            continue
        names = [m["name"]]
        if m.get("nickname"):
            names.append(m["nickname"])
        hit = next((n for n in names if re.search(r"@" + re.escape(n), text)), None)
        if hit:
            matched_spans.append(hit)
            # 被 @ 的成员是否 leader 身份运行（据其在项目里的 is_leader）
            is_leader_run = bool(m.get("is_leader")) or (m["slug"] == leader_slug)
            # 子任务内 @负责人：不唤醒（子任务是叶子工作，负责人统筹只在顶层；否则层层派生）
            if in_subtask and is_leader_run:
                continue
            rid = await enqueue_run(task_id, m["slug"], "", "mention", is_leader_run)
            if rid:
                triggered.append(m["slug"])
                seen.add(m["slug"])

    # 找出 @ 了却匹配不到任何真实成员的名字（Leader 编造成员名的典型症状），记活动提示。
    # 仅当"一个真实成员都没 @ 到"时才告警——否则多半是发言里顺带出现的普通词（如解释 @xx 用法），
    # 有真实成员被唤醒就说明协同在正常进行，不用为杂散词报警。
    if not triggered:
        unknown = _unmatched_mentions(text, matched_spans)
        if unknown:
            from activity import log_activity
            await log_activity(
                task_id, "commented", "system", "",
                {"note": f"⚠️ 发言里 @ 的名字不在团队花名册中、无人被唤醒：{'、'.join(unknown)}。"
                         f"请只 @ 花名册里的真实成员。"})
    return triggered


def _unmatched_mentions(text: str, matched: list[str]) -> list[str]:
    """从文本里抽出所有 @token，减去已匹配到真实成员的，剩下的即"@ 了但不存在"的名字。"""
    # @后面跟中文/英文/数字/下划线的连续片段（遇标点/空格/括号停止）
    tokens = re.findall(r"@([\w一-鿿]+)", text)
    out = []
    for t in tokens:
        # 若该 token 是某个已匹配名字的前缀/包含，视为已命中
        if any(t == mm or t.startswith(mm) or mm.startswith(t) for mm in matched):
            continue
        if t not in out:
            out.append(t)
    return out


# ---------- 执行一个队列 run ----------

async def _load_task_agent(task_id: int, slug: str):
    """取任务(含项目路径) + 成员(含 persona + 生效 provider)。"""
    db = await get_connection()
    try:
        task = await (await db.execute(
            """SELECT t.*, p.local_path AS project_dir
               FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?""", (task_id,))).fetchone()
        if not task:
            return None, None
        task = dict(task)
        agent = await (await db.execute(
            "SELECT * FROM project_agents WHERE project_id=? AND slug=? LIMIT 1",
            (task["project_id"], slug))).fetchone()
        if not agent:
            return task, None
        agent = dict(agent)
        prof = await (await db.execute(
            "SELECT provider_id FROM agent_profiles WHERE slug=?", (slug,))).fetchone()
        agent["provider_id_effective"] = prof["provider_id"] if prof else ""
        return task, agent
    finally:
        await db.close()


async def _run_one(item: dict) -> None:
    """执行队列里的一个 run：跑到底、落库，然后解析其发言里的 @ 继续入队。"""
    from executor import runner

    task_id = item["task_id"]
    slug = item["agent_slug"]
    is_leader = bool(item["is_leader"])
    task, agent = await _load_task_agent(task_id, slug)
    if not task or not agent:
        return

    # Leader 的 prompt：把「真实花名册 + 明确动作」直接推进正文（不让它自己去探索/查库）。
    task_brief = f"任务：{task.get('title','')}"
    if task.get("description"):
        task_brief += f"\n任务描述：{task['description']}"
    # 是否顶层任务：只有顶层任务才做「负责人统筹派活」；子任务一律是叶子工作（防止层层再派生）
    is_top_level = task.get("parent_task_id") in (None, 0)
    # 收尾/带完整指令的 run（如 maybe_advance_parent 发来的总结指令）：prompt 已完整，不再追加派活指令
    has_explicit_prompt = bool((item.get("prompt") or "").strip())
    if is_leader and is_top_level and not has_explicit_prompt:
        # 首次统筹：直接内联本项目真实成员（除负责人自己），Leader 无需 jian roster、更无需翻文件系统
        members = await _member_names(task["project_id"], exclude_slug=slug)
        if members:
            roster_line = "、".join(f"@{n}" for n in members)
            members_directive = (
                f"\n\n———\n"
                f"【你的团队成员（就这些人，不要臆想、不要去代码库里找别的）】：{roster_line}\n\n"
                f"【立即执行，不要探索】收到本任务后**第一件事**就是照上面的名单派活，"
                f"不要先读代码、不要先翻目录、不要先写团队介绍或概述——那些都是多余动作。\n"
                f"若任务要每位成员各自产出（如各自做自我介绍/各自负责一块），"
                f"就给上面**每一位**成员各建一张子任务并指派给他本人：\n"
                f"  `jian subtask --title \"（成员名）· 标题\" --owner <成员名> --assign --body-file 说明.md`\n"
                f"（--assign 会真正唤醒该成员本人来完成；每位成员一张，一个都不能漏。"
                f"介绍团队 = 让成员**本人**出来介绍，不是你替他们写。）\n"
                f"若只是讨论型，则用 `jian comment \"…@成员名…\"` 点名真实成员。\n"
                f"⚠️ 若你这一轮结束时一张子任务都没建、一个成员都没 @，就是**失败**——"
                f"说明你越俎代庖自己写了，而没有让团队真正干活。\n"
                f"绝不 @ 你自己、绝不编造上面名单以外的人。")
        else:
            members_directive = (
                "\n\n（本项目暂无其他成员）你在职责范围内直接作答并 `jian comment` 汇报即可，不要 @ 任何人。")
        prompt = f"{task_brief}\n\n请作为团队负责人统筹推进这个任务。{members_directive}"
    elif is_leader and has_explicit_prompt:
        # 负责人收尾汇总等带完整指令的 run：直接用该指令（maybe_advance_parent 已写清「不再派活/@」）
        prompt = item["prompt"]
    else:
        # 成员被 @ 唤醒：开头就是强指令，禁止探索项目，直接产出并用 jian 落库
        prompt = item["prompt"] or (
            f"【立即执行，不要探索项目、不要读任何文件、不要研究目录】\n"
            f"团队负责人在对话里 @ 了你，请你就地完成属于你的那份工作，"
            f"完全凭你自己的角色人格与职责作答。\n\n{task_brief}\n\n"
            f"步骤：\n"
            f"1) 若负责人要你产出一份东西（如自我介绍、你负责的方案），先把内容写进一个 .md 文件，"
            f"再执行 `jian subtask --title \"标题\" --body-file 文件路径` 建成子任务卡片（Owner 默认是你自己）。\n"
            f"2) 否则直接 `jian comment \"你的结论\"` 汇报。\n"
            f"3) 完成即结束，不要 @ 任何人（除非确实要别人接力）。")

    # 标记 agent 是否 leader 身份（runner 据此注入协议+花名册）
    agent["is_leader_run"] = is_leader
    agent["leader_slug"] = await get_leader_slug(task["project_id"])

    collected = []
    run_id_box = {"id": None}
    had_error = {"v": False}

    async def _drive():
        """逐事件消费执行流，返回「结束原因」：
          normal        —— 生成器自然跑完（Agent 正常收尾）
          idle_timeout  —— 连续 idle 秒无任何事件（判卡死）
          hard_wall     —— 超过硬墙钟总上限，且此刻已静默（真该收）

        Loop Engineering 理念：长时间跑不是问题，卡死才是问题。判据是「还在不在产出」而非
        「跑了多久」。故：
        - 静默超时（连续 idle 秒无任何事件）= 真卡死信号 → 立即返回 idle_timeout。
        - 硬墙钟到点 ≠ 直接杀：先做活性探测(liveness)——若仍在持续产出事件，则「续期」
          （重置硬墙钟起点、记一条长跑续期活动），继续追踪直到它真正结束或真的静默；
          只有硬墙钟到点且此刻确已静默，才返回 hard_wall 收尾。
        静默超时用「对每次取下一个事件设 idle 超时」实现——只要 Agent 在持续产出事件，
        取事件就不会超时；真卡死（无任何事件）才在 idle 秒后触发。
        """
        import time as _time
        idle = _idle_timeout(slug)
        hard = _hard_wall(slug)
        start = _time.monotonic()
        produced_since_wall = False   # 本个硬墙钟周期内是否有产出（活性标志）
        extensions = 0
        agen = runner.execute_dispatch(task, agent, prompt, persist_user_msg=False)
        try:
            while True:
                if _time.monotonic() - start > hard:
                    # 硬墙钟到点：活性探测。本周期内仍在产出 → 续期，不杀。
                    if produced_since_wall:
                        extensions += 1
                        produced_since_wall = False
                        start = _time.monotonic()
                        try:
                            from activity import log_activity
                            await log_activity(
                                task_id, "commented", "system", "",
                                {"note": f"⏱️ {agent.get('name', slug)} 长跑续期（第 {extensions} 次）："
                                         f"硬墙钟到点但仍在持续产出，判为健康长任务、继续追踪不终止。"})
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    return "hard_wall"
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), timeout=idle)
                except StopAsyncIteration:
                    return "normal"
                except asyncio.TimeoutError:
                    return "idle_timeout"
                produced_since_wall = True   # 收到事件 = 活着且在产出
                if ev.type == "system" and ev.meta.get("run_id"):
                    run_id_box["id"] = ev.meta["run_id"]
                elif ev.type == "text":
                    collected.append(ev.text)
                elif ev.type == "error":
                    had_error["v"] = True
        finally:
            await agen.aclose()

    run_status = "done"   # run_queue 的最终状态：done / failed
    saved_via_grace = False   # 是否经「超时保成果」落终态（此时 collected 可能为空，但 DB 里已有交付）
    try:
        outcome = await _drive()
        if outcome != "normal":
            # 判超时（静默/硬墙钟）：先给宽限，尝试让 Agent 自己收尾落库已完成的成果（保成果 B）。
            # 宽限内该 run 若被 finalize/自然结束（进程自己 jian comment 后退出），视为成功、不销毁成果。
            rid = run_id_box["id"]
            saved = await _grace_then_kill(task_id, slug, agent, rid, outcome)
            run_status = "done" if saved else "failed"
            saved_via_grace = saved
    except Exception:  # noqa: BLE001
        run_status = "failed"

    final_text = "".join(collected).strip()
    if had_error["v"] and not final_text:
        run_status = "failed"

    # 双保险（对齐 task_runs 实际结果）：execute_dispatch 内部已把本 run 的 task_runs 落终态；
    # 若那里已判 succeeded，则本 run 就是成功的——不能因收尾/善后阶段的异常在外层误判 failed，
    # 否则 run_queue=failed 与 task_runs=succeeded 分叉，状态无法推进（task 82 事故根因）。
    rid = run_id_box.get("id")
    if run_status == "failed" and rid:
        db2 = await get_connection()
        try:
            tr = await (await db2.execute(
                "SELECT status FROM task_runs WHERE id=?", (rid,))).fetchone()
        finally:
            await db2.close()
        if tr and tr["status"] == "succeeded":
            run_status = "done"

    # 解析该成员发言里的 @，继续协同
    if final_text:
        leader_slug = agent["leader_slug"]
        await parse_and_enqueue_mentions(task_id, task["project_id"], final_text, slug, leader_slug)

    # 执行完成后的状态流转（绝不触发经验沉淀——沉淀只在父任务人工验收 done 时发生）：
    #  - 子任务执行完 → 直接进「完成(done)」（子任务无"验证中"概念），但**不触发沉淀**。
    #  - 子任务全部 done → 父任务自动进「验证中」，等人工验收。
    #  - 无子任务的独立顶层任务执行完 → 自身进「验证中」，等人工验收。
    #  - 经验沉淀 + 已解决计数：只在管理员把父任务/独立任务人工验收拖入「完成」时触发（routes/tasks.py），
    #    届时把父+全部子任务的经验一起沉淀。
    # 推进条件：正常收尾有产出，或「超时保成果」已落终态（后者 final_text 可能为空，
    # 但 DB 里已有交付——不能因 collected 为空就漏掉状态推进，否则任务会卡在 in_progress，
    # 只能等 Agent 自己某次 jian status 侥幸推进。Bug 修复：保成果路径也要推进任务状态）。
    if run_status == "done" and (final_text or saved_via_grace) and not is_leader:
        from progress import on_execution_complete
        # 传本 run 的队列行 id：此刻它的 done 标记还没写（在 _process_one finally 里），
        # 需从"是否还有待跑 run"里排除自己，否则父任务永远收不了尾。
        await on_execution_complete(task_id, exclude_run_id=item.get("id"))
    return run_status


async def get_leader_slug(project_id: int) -> str:
    """实时查当前项目的负责人 slug（不缓存——团队/负责人随时可能增删改，必须每次读最新）。"""
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT slug FROM project_agents WHERE project_id=? AND is_leader=1 LIMIT 1",
            (project_id,))).fetchone()
        return row["slug"] if row else ""
    finally:
        await db.close()


# ---------- 后台循环（小并发池：卡死只占一个槽，不阻塞其他 Agent） ----------

MAX_CONCURRENCY = 3          # 同时最多跑几个 Agent
_running: set = set()        # 正在跑的 run_queue id


async def _claim_one() -> dict | None:
    """原子领取一个 queued run（标 running）。返回 item 或 None。"""
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT * FROM run_queue WHERE status='queued' ORDER BY id LIMIT 1")).fetchone()
        if not row:
            return None
        item = dict(row)
        await db.execute("UPDATE run_queue SET status='running' WHERE id=?", (item["id"],))
        await db.commit()
        return item
    finally:
        await db.close()


async def _process_one(item: dict) -> None:
    """执行一个 run 并落库最终状态。（_run_one 内部已有 6 分钟超时兜底）"""
    status = "done"
    try:
        rs = await _run_one(item)
        if rs == "failed":
            status = "failed"
    except Exception as e:  # noqa: BLE001 — 吞异常防带崩队列，但必须留痕（否则失败零线索）
        status = "failed"
        # 留痕只能进后端进程 stderr：这里手上只有 run_queue.id（item["id"]），
        # 而 run_logs.run_id 外键指向的是 task_runs.id——两者是各自独立的自增序列，
        # 同一个数字在两表里指代不同 run。若把 run_queue.id 塞进 run_logs，日志会误挂到
        # task_runs 里同号的另一个 run 上（排查 task82 时「run 80 跑了 4 小时」的假象即此类串号）。
        # 且异常可能发生在 task_run 建立之前，此刻根本没有可关联的 task_runs.id。
        import traceback
        print(f"[collab] run_queue#{item.get('id')} 执行异常，落 failed："
              f"{type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
    finally:
        db = await get_connection()
        try:
            await db.execute("UPDATE run_queue SET status=? WHERE id=?", (status, item["id"]))
            await db.commit()
        finally:
            await db.close()
        _running.discard(item["id"])


async def _tick() -> bool:
    """确定性单步：领取一个 queued run 并同步执行到底。
    有活干返回 True，队列空返回 False。
    供测试逐步驱动队列用（生产走 _loop 并发池，二者共用 _claim_one/_process_one）。"""
    item = await _claim_one()
    if not item:
        return False
    _running.add(item["id"])
    await _process_one(item)
    return True


async def _loop():
    while True:
        try:
            # 一次性把空闲槽填满（连续 claim，不在 claim 之间等待），
            # 否则短任务永远达不到 MAX_CONCURRENCY。只有队列空/池满才休眠。
            claimed_any = False
            while len(_running) < MAX_CONCURRENCY:
                item = await _claim_one()
                if not item:
                    break
                _running.add(item["id"])
                asyncio.create_task(_process_one(item))
                claimed_any = True
            if claimed_any:
                await asyncio.sleep(0.05)   # 让出事件循环，随即继续补槽
                continue
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1.0)             # 队列空或池满：轮询间隔


async def reclaim_orphan_runs() -> int:
    """启动时回收孤儿 running —— 两层都要清：run_queue 与 task_runs。

    执行状态由本进程内存态（run_queue 靠 _running 集合 + _process_one 协程；task_runs 靠
    execute_dispatch 生成器自然收尾或 finalize_run 补落）驱动。进程重启 / 生成器被取消 / 连接
    断开时，这些收尾路径都跑不到，会留下 status='running' 的孤儿：
      - run_queue 孤儿 → progress.py 误判「任务仍在执行中」（外部卡片、进度面板）；
      - task_runs 孤儿 → 详情页右侧「执行记录」列表 RunRow 直接显示「执行中」（/runs 读 task_runs）。
    两层数据源不同、必须一起回收，否则一层清了另一层仍露馅（曾只清 run_queue，task_runs 残留
    导致详情页右侧持续显示执行中）。

    只在 start_loop 之前调用（此刻 _running / _RUN_PIDS 必为空、_loop 尚未领新活），故所有
    running 皆为无主孤儿。run_queue 落 failed；task_runs 落 killed（被中断，非正常失败）。"""
    from executor import runner  # noqa: PLC0415
    affected_tasks: set[int] = set()
    db = await get_connection()
    try:
        # 1) run_queue 层
        q_rows = await (await db.execute(
            "SELECT id, task_id FROM run_queue WHERE status='running'")).fetchall()
        # 2) task_runs 层
        r_rows = await (await db.execute(
            "SELECT id, task_id FROM task_runs WHERE status='running'")).fetchall()
        if not q_rows and not r_rows:
            return 0
        if q_rows:
            await db.execute("UPDATE run_queue SET status='failed' WHERE status='running'")
        if r_rows:
            # task_runs 孤儿的终态按其任务是否已成功收尾区分：
            #  - 任务已 done/reviewing → 这条 run 正是任务成功的产出，落 succeeded（否则会把
            #    已完成任务的成果 run 误标 killed，污染卡片「执行完成」显示与 solved_tasks 计数）；
            #  - 任务未收尾 → 确是被中断的孤儿，落 killed。
            await db.execute(
                """UPDATE task_runs SET status='succeeded', ended_at=datetime('now')
                   WHERE status='running' AND task_id IN (
                       SELECT id FROM tasks WHERE status IN ('done','reviewing'))""")
            await db.execute(
                "UPDATE task_runs SET status='killed', ended_at=datetime('now') WHERE status='running'")
        await db.commit()
        for row in q_rows:
            affected_tasks.add(row["task_id"])
        for row in r_rows:
            affected_tasks.add(row["task_id"])
    finally:
        await db.close()
    # 清掉内存注册表里可能残留的这些 run（正常应已空，防御性）
    for row in r_rows:
        runner._RUN_PIDS.pop(row["id"], None)
        runner._KILLED.discard(row["id"])
    if not affected_tasks:
        return len(q_rows) + len(r_rows)
    # 查受影响任务的当前状态：已 done/reviewing 的不记「回收失败」活动（那是成功任务、只是 run 没落库）
    db = await get_connection()
    try:
        ph = ",".join("?" for _ in affected_tasks)
        finished = {r["id"] for r in await (await db.execute(
            f"SELECT id FROM tasks WHERE id IN ({ph}) AND status IN ('done','reviewing')",
            tuple(affected_tasks))).fetchall()}
    finally:
        await db.close()
    # 落终态后，对未收尾的任务记一条活动 + 尝试推进父任务状态（清掉「执行中」假象后可能可收尾）
    from activity import log_activity
    from progress import maybe_advance_parent
    for tid in affected_tasks:
        try:
            if tid not in finished:
                await log_activity(tid, "task_failed", "system", "",
                                   {"note": "重启前遗留的「执行中」记录已回收（进程已不存在），非真正在执行"})
            await maybe_advance_parent(tid)
        except Exception:  # noqa: BLE001
            pass
    return len(q_rows) + len(r_rows)


def start_loop():
    """FastAPI 启动时调用，拉起后台协同循环（幂等）。"""
    global _loop_started
    if _loop_started:
        return
    _loop_started = True
    asyncio.create_task(_loop())


