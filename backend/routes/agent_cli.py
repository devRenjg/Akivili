"""Agent CLI 后端：供 jian CLI 调用。身份由 runner 注入的 task_id + agent_slug 决定，
本地可信内网，无需 token（与全权限放开定位一致）。Agent 借此在平台上真正操作：
建子任务卡片（Owner=自己）、发言、改状态、查花名册。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_connection
import collab
from activity import log_activity

router = APIRouter(prefix="/api/agent-cli", tags=["agent-cli"])


async def _task_project(task_id: int):
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT id, project_id, title FROM tasks WHERE id=?", (task_id,))).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _resolve_member(project_id: int, ref: str) -> str:
    """把 owner 引用（slug / 名字 / 昵称）解析成成员 slug；解析不到返回空。"""
    ref = (ref or "").strip()
    if not ref:
        return ""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            """SELECT pa.slug, pa.name, p.nickname AS nickname
               FROM project_agents pa LEFT JOIN agent_profiles p ON p.slug = pa.slug
               WHERE pa.project_id=?""", (project_id,))).fetchall()
    finally:
        await db.close()
    for r in rows:
        if ref in (r["slug"], r["name"], r["nickname"] or ""):
            return r["slug"]
    return ""


async def _display_name(project_id: int, slug: str) -> str:
    """把成员 slug 解析成展示名「昵称（角色名）」；无昵称回退角色名；查不到回退 slug。
    用于活动/发言文案，避免暴露英文 slug。"""
    if not slug:
        return slug
    db = await get_connection()
    try:
        r = await (await db.execute(
            """SELECT pa.name, p.nickname AS nickname FROM project_agents pa
               LEFT JOIN agent_profiles p ON p.slug = pa.slug
               WHERE pa.project_id=? AND pa.slug=? LIMIT 1""", (project_id, slug))).fetchone()
    finally:
        await db.close()
    if not r:
        return slug
    nick = (r["nickname"] or "").strip()
    name = r["name"] or slug
    return f"{nick}（{name}）" if nick else name


class SubtaskReq(BaseModel):
    task_id: int          # 父任务（当前任务）
    agent_slug: str       # 调用者身份（runner 注入）
    title: str
    body: str = ""        # 子任务正文（作为首条对话消息落库）
    owner_slug: str = ""  # 子任务 Owner，默认=调用者自己
    assign: bool = False  # True=委派模式：指派给 owner 并触发其在子任务里执行（owner≠自己时）


@router.post("/subtask")
async def create_subtask(req: SubtaskReq):
    """Agent 建子任务卡片。两种用法：
    - 记录自己的产出（owner=自己 或 assign=False）：子任务建为 done，正文即成果。
    - 委派给他人（assign=True 且 owner≠自己）：子任务建为 in_progress，指派给 owner
      并触发其在这个子任务里执行（成员在自己的卡片里干活）。
    """
    parent = await _task_project(req.task_id)
    if not parent:
        raise HTTPException(404, "父任务不存在")
    pid = parent["project_id"]
    owner = await _resolve_member(pid, req.owner_slug) or req.agent_slug
    delegate = req.assign and owner != req.agent_slug
    status = "in_progress" if delegate else "done"
    db = await get_connection()
    try:
        conv = await db.execute(
            "INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, req.title.strip()))
        cur = await db.execute(
            """INSERT INTO tasks (project_id, title, assignee_slug, conversation_id, parent_task_id, status)
               VALUES (?,?,?,?,?,?)""",
            (pid, req.title.strip(), owner, conv.lastrowid, req.task_id, status))
        sub_id = cur.lastrowid
        if req.body.strip():
            # 自产出(assistant)：作者=建卡的 agent；委派(user 指令)：作者留空（相当于负责人下达）
            body_author = "" if delegate else req.agent_slug
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content, author_slug) VALUES (?,?,?,?)",
                (conv.lastrowid, "assistant" if not delegate else "user", req.body.strip(), body_author))
        await db.commit()
    finally:
        await db.close()
    owner_disp = await _display_name(pid, owner)
    note = (f"委派子任务给 {owner_disp}：{req.title.strip()}" if delegate
            else f"创建子任务并完成：{req.title.strip()}")
    await log_activity(req.task_id, "commented", "agent", req.agent_slug, {"note": note})
    # 委派模式：触发 owner 在子任务里执行（用子任务正文作为给他的指令）
    if delegate:
        await collab.enqueue_run(sub_id, owner, req.body.strip(), "assign", is_leader=False)
    else:
        # 自产出模式：子任务直接建为 done → 可能触发父任务推进
        from progress import maybe_advance_parent
        await maybe_advance_parent(sub_id)
    return {"ok": True, "subtask_id": sub_id, "owner": owner, "delegated": delegate}


class CommentReq(BaseModel):
    task_id: int
    agent_slug: str
    body: str


@router.post("/comment")
async def add_comment(req: CommentReq):
    """Agent 在当前任务发言（落库为 assistant 消息）。发言里的 @ 会照常触发协同。"""
    parent = await _task_project(req.task_id)
    if not parent:
        raise HTTPException(404, "任务不存在")
    db = await get_connection()
    try:
        conv = await (await db.execute(
            "SELECT conversation_id FROM tasks WHERE id=?", (req.task_id,))).fetchone()
        if conv and conv["conversation_id"]:
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content, author_slug) VALUES (?,?,?,?)",
                (conv["conversation_id"], "assistant", req.body.strip(), req.agent_slug))
            await db.commit()
    finally:
        await db.close()
    # 解析发言里的 @，继续协同（实时查负责人，团队变动即时生效）
    leader = await collab.get_leader_slug(parent["project_id"])
    await collab.parse_and_enqueue_mentions(req.task_id, parent["project_id"],
                                            req.body, req.agent_slug, leader)
    return {"ok": True}


class StatusReq(BaseModel):
    task_id: int
    agent_slug: str
    status: str


@router.post("/status")
async def set_status(req: StatusReq):
    """Agent 改当前任务状态。

    🔒 **任务完成必须人工验收**：Agent（含负责人）不允许把任务标记为 `done`。
    Agent 调 `jian status done` 时，一律**降级为 `reviewing`（验证中）**——表示「执行完成、
    等待人工验收」，`done` 只能由管理员在看板上人肉验收后手动操作。这样避免 Agent 自行了结任务、
    绕过验收，也避免未验收就触发经验沉淀。
    """
    STATUSES = ["backlog", "in_progress", "reviewing", "done", "blocked"]
    if req.status not in STATUSES:
        raise HTTPException(400, f"非法状态：{req.status}")

    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT status, parent_task_id FROM tasks WHERE id=?", (req.task_id,))).fetchone()
        old_status = row["status"] if row else ""
        is_subtask = bool(row["parent_task_id"]) if row else False
    finally:
        await db.close()

    target = req.status
    downgraded = False
    # 子任务没有"验证中"概念：子任务执行完就是 done（随父任务整体验收）。
    # Agent 对子任务标 reviewing → 归一为 done，避免子任务卡在 reviewing、父任务永远等不齐。
    if is_subtask and target == "reviewing":
        target = "done"
    # 拦截 Agent 标 done → 降级为 reviewing（执行完成、待人工验收）。
    # 仅对顶层任务降级；子任务 done 正常放行。
    elif target == "done" and not is_subtask:
        target = "reviewing"
        downgraded = True

    db = await get_connection()
    try:
        await db.execute("UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?",
                         (target, req.task_id))
        await db.commit()
    finally:
        await db.close()
    if old_status != target:
        note = "执行完成，进入验证中，等待人工验收（Agent 不能直接标记完成）" if downgraded else ""
        await log_activity(req.task_id, "status_changed", "agent", req.agent_slug,
                           {"from": old_status, "to": target, **({"note": note} if note else {})})
    # 子任务被标 done：若父任务全部子任务已完成，自动把父任务推进到「验证中」等人工验收。
    # 该 agent 是在自己的 run 执行中调 jian status done 的，其 run_queue 行此刻仍是 running，
    # 需排除自己，否则会被算成 pending 而阻止父任务收尾。
    if is_subtask and target == "done":
        import progress as _progress
        db = await get_connection()
        try:
            own = await (await db.execute(
                "SELECT id FROM run_queue WHERE task_id=? AND agent_slug=? AND status='running' "
                "ORDER BY id DESC LIMIT 1", (req.task_id, req.agent_slug))).fetchone()
        finally:
            await db.close()
        await _progress.on_execution_complete(
            req.task_id, exclude_run_id=own["id"] if own else None)
    return {"ok": True, "status": target,
            **({"note": "任务需人工验收，已置为 reviewing（验证中），不能由 Agent 直接完成"} if downgraded else {})}


@router.get("/roster/{task_id}")
async def get_roster(task_id: int):
    """查团队花名册（Markdown）。"""
    parent = await _task_project(task_id)
    if not parent:
        raise HTTPException(404, "任务不存在")
    leader = await collab.get_leader_slug(parent["project_id"])
    return {"roster": await collab.build_roster(parent["project_id"], leader)}
