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

# 单个 Agent 执行超时（秒）：超时 kill 子进程，避免卡死拖垮队列/留僵尸。
# 默认 30 分钟——多数角色一轮工作（含多轮工具调用）足够，又能兜住真卡死。
RUN_TIMEOUT_SEC = 1800

# 按角色 slug 覆盖超时：数据类角色常经外部取数服务拉数、单轮真实工作远超普通角色，
# 给 60 分钟。新增需要长跑的角色在此登记即可。
RUN_TIMEOUT_OVERRIDES = {
    "engineering-data-engineer": 3600,
}


def _run_timeout(slug: str) -> int:
    """该角色的执行超时秒数（有覆盖用覆盖，否则用默认）。
    运行时读模块全局，便于测试 monkeypatch RUN_TIMEOUT_SEC。"""
    return RUN_TIMEOUT_OVERRIDES.get(slug, RUN_TIMEOUT_SEC)

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

    async def _consume():
        async for ev in runner.execute_dispatch(task, agent, prompt):
            if ev.type == "system" and ev.meta.get("run_id"):
                run_id_box["id"] = ev.meta["run_id"]
            elif ev.type == "text":
                collected.append(ev.text)
            elif ev.type == "error":
                had_error["v"] = True

    run_status = "done"   # run_queue 的最终状态：done / failed
    timeout_sec = _run_timeout(slug)
    try:
        # 超时保护：单个 Agent 执行最多 timeout_sec 秒；超时 kill 子进程树，避免卡死拖垮队列与留僵尸
        await asyncio.wait_for(_consume(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        run_status = "failed"
        # _consume 被 wait_for 取消后，runner.execute_dispatch 生成器的收尾（落库 task_runs 终态）
        # 跑不到——这里主动 kill 进程树并把 task_run 落成 failed，杜绝孤儿 running 记录。
        if run_id_box["id"]:
            runner.kill_run(run_id_box["id"])
            await runner.finalize_run(run_id_box["id"], "failed")
        from activity import log_activity
        mins = timeout_sec // 60
        # actor_name 用角色名（agent["name"]）而非 slug，前端 timeline 才能解析出「昵称（角色）」
        await log_activity(task_id, "task_failed", "agent", agent.get("name", slug),
                           {"reason": f"执行超时（>{mins}分钟），已终止"})
    except Exception:  # noqa: BLE001
        run_status = "failed"

    final_text = "".join(collected).strip()
    if had_error["v"] and not final_text:
        run_status = "failed"

    # 解析该成员发言里的 @，继续协同
    if final_text:
        leader_slug = agent["leader_slug"]
        await parse_and_enqueue_mentions(task_id, task["project_id"], final_text, slug, leader_slug)
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
    except Exception:  # noqa: BLE001
        status = "failed"
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


def start_loop():
    """FastAPI 启动时调用，拉起后台协同循环（幂等）。"""
    global _loop_started
    if _loop_started:
        return
    _loop_started = True
    asyncio.create_task(_loop())


