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
    note = (f"委派子任务给 {owner}：{req.title.strip()}（子任务#{sub_id}）" if delegate
            else f"创建子任务并完成：{req.title.strip()}（子任务#{sub_id}）")
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
    """Agent 改当前任务状态。若把有未完成子任务的父任务标 done，则拦截并降级为 reviewing。"""
    STATUSES = ["backlog", "in_progress", "reviewing", "done", "blocked"]
    if req.status not in STATUSES:
        raise HTTPException(400, f"非法状态：{req.status}")
    # 闭环规则：子任务没全完成，父任务不能 done
    if req.status == "done":
        from progress import blocking_subtasks
        pending = await blocking_subtasks(req.task_id)
        if pending:
            names = "、".join(f"#{s['id']}{s['title']}" for s in pending[:5])
            await log_activity(req.task_id, "commented", "system", "",
                               {"note": f"父任务暂不能完成：还有 {len(pending)} 个子任务未完成（{names}）。"
                                        f"待全部完成后再汇总收尾。"})
            return {"ok": False, "status": "blocked_by_subtasks",
                    "pending_subtasks": pending,
                    "message": f"还有 {len(pending)} 个子任务未完成，父任务不能标记完成"}
    db = await get_connection()
    try:
        old = await (await db.execute("SELECT status FROM tasks WHERE id=?", (req.task_id,))).fetchone()
        await db.execute("UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?",
                         (req.status, req.task_id))
        await db.commit()
        old_status = old["status"] if old else ""
    finally:
        await db.close()
    if old_status != req.status:
        await log_activity(req.task_id, "status_changed", "agent", req.agent_slug,
                           {"from": old_status, "to": req.status})
        # 子任务完成 → 检查是否该推进父任务（并唤醒负责人收尾）
        if req.status == "done":
            from progress import maybe_advance_parent
            await maybe_advance_parent(req.task_id)
            # 任务收尾 → 参与角色各自复盘沉淀 Know-how（后台异步，不阻塞）
            import asyncio
            from reflect import reflect_on_task_done
            asyncio.create_task(reflect_on_task_done(req.task_id))
    return {"ok": True, "status": req.status}


@router.get("/roster/{task_id}")
async def get_roster(task_id: int):
    """查团队花名册（Markdown）。"""
    parent = await _task_project(task_id)
    if not parent:
        raise HTTPException(404, "任务不存在")
    leader = await collab.get_leader_slug(parent["project_id"])
    return {"roster": await collab.build_roster(parent["project_id"], leader)}
