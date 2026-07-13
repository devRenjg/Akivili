"""任务接口：CRUD + 状态流转 + 归档 + 看板分组。创建任务时建关联对话 Thread。"""
from fastapi import APIRouter, HTTPException, Depends

from auth import require_admin
from pydantic import BaseModel

import projects as projects_mod
from database import get_connection
from activity import log_activity, timeline
from timeutil import to_beijing

router = APIRouter(prefix="/api/projects", tags=["tasks"])

# 看板状态（有序）+ 中文标签
STATUSES = ["backlog", "in_progress", "reviewing", "done", "blocked"]
PRIORITIES = ["urgent", "high", "medium", "low", "none"]


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    assignee_slug: str = ""
    priority: str = "none"
    parent_task_id: int | None = None


class UpdateTaskRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    assignee_slug: str | None = None
    priority: str | None = None


async def _ensure_project(pid: int):
    if not await projects_mod.get_project(pid):
        raise HTTPException(404, "项目不存在")


@router.get("/{pid}/tasks")
async def list_tasks(pid: int):
    await _ensure_project(pid)
    db = await get_connection()
    try:
        rows = await (await db.execute(
            """SELECT t.*,
                      (SELECT status FROM task_runs r WHERE r.task_id=t.id ORDER BY r.id DESC LIMIT 1) AS run_status,
                      (SELECT content FROM messages m WHERE m.conversation_id=t.conversation_id AND m.role='assistant'
                         ORDER BY m.id DESC LIMIT 1) AS last_result,
                      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=t.conversation_id) AS msg_count,
                      (SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=t.id) AS sub_total,
                      (SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=t.id AND c.status='done') AS sub_done
               FROM tasks t WHERE t.project_id=? AND t.parent_task_id IS NULL
               ORDER BY t.created_at DESC, t.id DESC""", (pid,))).fetchall()
        tasks = [dict(r) for r in rows]
        # 取每个顶层任务的子任务（看板卡片下方嵌套小卡展示）
        sub_rows = await (await db.execute(
            """SELECT c.id, c.title, c.status, c.priority, c.assignee_slug, c.parent_task_id,
                      (SELECT status FROM task_runs r WHERE r.task_id=c.id ORDER BY r.id DESC LIMIT 1) AS run_status,
                      (SELECT COUNT(*) FROM run_queue q WHERE q.task_id=c.id AND q.status IN ('queued','running')) AS active_run
               FROM tasks c WHERE c.project_id=? AND c.parent_task_id IS NOT NULL
               ORDER BY c.order_idx, c.id""", (pid,))).fetchall()
        subs_by_parent: dict = {}
        for sr in sub_rows:
            subs_by_parent.setdefault(sr["parent_task_id"], []).append(dict(sr))
        for t in tasks:
            for col in ("created_at", "updated_at"):
                if col in t:
                    t[col] = to_beijing(t[col])
            t["subtasks"] = subs_by_parent.get(t["id"], [])
        # 按状态分组，便于看板渲染
        board = {s: [t for t in tasks if t["status"] == s] for s in STATUSES}
        return {"tasks": tasks, "board": board}
    finally:
        await db.close()


@router.post("/{pid}/tasks", dependencies=[Depends(require_admin)])
async def create_task(pid: int, req: CreateTaskRequest, user: dict = Depends(require_admin)):
    await _ensure_project(pid)
    if not req.title.strip():
        raise HTTPException(400, "任务标题不能为空")
    db = await get_connection()
    try:
        # 建关联对话 Thread
        conv = await db.execute(
            "INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, req.title.strip()))
        conv_id = conv.lastrowid
        cur = await db.execute(
            """INSERT INTO tasks (project_id, title, description, assignee_slug, conversation_id, priority, parent_task_id, status)
               VALUES (?,?,?,?,?,?,?, 'backlog')""",
            (pid, req.title.strip(), req.description, req.assignee_slug, conv_id,
             req.priority if req.priority in PRIORITIES else "none", req.parent_task_id))
        await db.commit()
        tid = cur.lastrowid
        row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (tid,))).fetchone()
        result = dict(row)
    finally:
        await db.close()
    await log_activity(tid, "created", "user", user.get("username", ""), {"title": req.title.strip()})
    return result


@router.put("/{pid}/tasks/{task_id}", dependencies=[Depends(require_admin)])
async def update_task(pid: int, task_id: int, req: UpdateTaskRequest, user: dict = Depends(require_admin)):
    await _ensure_project(pid)
    sets = {k: v for k, v in req.model_dump().items() if v is not None}
    if not sets:
        raise HTTPException(400, "无可更新字段")
    db = await get_connection()
    try:
        old = await (await db.execute("SELECT priority FROM tasks WHERE id=? AND project_id=?",
                                      (task_id, pid))).fetchone()
        cols = ", ".join(f"{k}=?" for k in sets)
        cur = await db.execute(
            f"UPDATE tasks SET {cols}, updated_at=datetime('now') WHERE id=? AND project_id=?",
            (*sets.values(), task_id, pid))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "任务不存在")
        row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
        result = dict(row)
    finally:
        await db.close()
    if "priority" in sets and old and old["priority"] != sets["priority"]:
        await log_activity(task_id, "priority_changed", "user", user.get("username", ""),
                           {"from": old["priority"], "to": sets["priority"]})
    return result


class StatusRequest(BaseModel):
    status: str
    force: bool = False   # 管理员可强制越过"子任务未完成不能 done"的拦截


@router.put("/{pid}/tasks/{task_id}/status", dependencies=[Depends(require_admin)])
async def set_status(pid: int, task_id: int, req: StatusRequest, user: dict = Depends(require_admin)):
    await _ensure_project(pid)
    if req.status not in STATUSES:
        raise HTTPException(400, f"非法状态：{req.status}")
    # 闭环规则：子任务没全完成，父任务不能 done（管理员可 force 覆盖）
    if req.status == "done" and not req.force:
        from progress import blocking_subtasks
        pending = await blocking_subtasks(task_id)
        if pending:
            names = "、".join(f"#{s['id']}{s['title']}" for s in pending[:5])
            raise HTTPException(
                409, f"还有 {len(pending)} 个子任务未完成（{names}），父任务不能标记完成。"
                     f"请等子任务全部完成并由负责人汇总后再收尾，或 force 强制。")
    db = await get_connection()
    try:
        old = await (await db.execute("SELECT status FROM tasks WHERE id=? AND project_id=?",
                                      (task_id, pid))).fetchone()
        cur = await db.execute(
            "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=? AND project_id=?",
            (req.status, task_id, pid))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "任务不存在")
        old_status = old["status"] if old else ""
    finally:
        await db.close()
    if old_status != req.status:
        await log_activity(task_id, "status_changed", "user", user.get("username", ""),
                           {"from": old_status, "to": req.status})
        if req.status == "done":
            from progress import maybe_advance_parent
            await maybe_advance_parent(task_id)
            # 卡片进入「已完成」→ 参与角色各自复盘、沉淀 Know-how（后台异步，不阻塞返回）
            import asyncio
            from reflect import reflect_on_task_done
            asyncio.create_task(reflect_on_task_done(task_id))
    return {"ok": True, "status": req.status}


@router.delete("/{pid}/tasks/{task_id}", dependencies=[Depends(require_admin)])
async def delete_task(pid: int, task_id: int):
    """删除任务：连同子任务一并删除，并清除各参与成员记忆里属于这些任务的条目。

    任务删了意味着其沉淀的记忆也失效（近期动态 + Know-how），故删库后按任务 ID
    从参与过执行的成员记忆里精准剔除对应条目。清理失败不影响删除主流程。
    """
    await _ensure_project(pid)
    db = await get_connection()
    try:
        # 本任务 + 其子任务的 id 集合（先收集，供删库与清记忆共用）
        srows = await (await db.execute(
            "SELECT id FROM tasks WHERE parent_task_id=? AND project_id=?", (task_id, pid))).fetchall()
        all_ids = [task_id] + [r["id"] for r in srows]
        # 这些任务里真正跑过 run 的成员 slug（有 run = 有记忆沉淀），删库前查出（task_runs 会级联删）
        ph = ",".join("?" for _ in all_ids)
        arows = await (await db.execute(
            f"SELECT DISTINCT agent_slug FROM task_runs WHERE task_id IN ({ph}) AND agent_slug<>''",
            all_ids)).fetchall()
        slugs = [r["agent_slug"] for r in arows]
        # 级联删除子任务 + 本任务（task_runs/run_logs 经外键 ON DELETE CASCADE 一并清）
        await db.execute("DELETE FROM tasks WHERE parent_task_id=? AND project_id=?", (task_id, pid))
        cur = await db.execute("DELETE FROM tasks WHERE id=? AND project_id=?", (task_id, pid))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "任务不存在")
    finally:
        await db.close()

    # 清各成员记忆里属于这些任务的条目（任务没了，沉淀也失效）
    purged = 0
    try:
        from memory import purge_task_memory
        for slug in slugs:
            purged += purge_task_memory(slug, all_ids)
    except Exception:  # noqa: BLE001 — 清记忆失败不该让删除失败
        pass
    return {"ok": True, "memory_purged": purged}


@router.get("/{pid}/tasks/{task_id}/activities")
async def get_activities(pid: int, task_id: int):
    """活动 + 对话消息 合并的时间线。"""
    return {"timeline": await timeline(task_id)}


@router.get("/{pid}/tasks/{task_id}/progress")
async def get_progress(pid: int, task_id: int):
    """父任务 + 子任务的执行进度（哪些 Agent 还在跑/排队、子任务完成数）。
    供任务详情右侧执行日志区展示「还在执行中 / 哪些子Agent在跑」。"""
    from progress import task_progress
    return await task_progress(task_id)


@router.get("/{pid}/tasks/{task_id}/subtasks")
async def list_subtasks(pid: int, task_id: int):
    await _ensure_project(pid)
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT * FROM tasks WHERE parent_task_id=? ORDER BY order_idx, id", (task_id,))).fetchall()
        return {"subtasks": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.get("/{pid}/tasks/{task_id}")
async def get_task(pid: int, task_id: int):
    """单个任务详情（顶层或子任务通用）——子任务详情页靠它加载，看板列表只含顶层任务。"""
    await _ensure_project(pid)
    db = await get_connection()
    try:
        row = await (await db.execute(
            """SELECT t.*,
                      (SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=t.id) AS sub_total,
                      (SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=t.id AND c.status='done') AS sub_done,
                      pt.title AS parent_title
               FROM tasks t LEFT JOIN tasks pt ON pt.id = t.parent_task_id
               WHERE t.id=? AND t.project_id=?""", (task_id, pid))).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        d = dict(row)
        for col in ("created_at", "updated_at"):
            if col in d:
                d[col] = to_beijing(d[col])
        return d
    finally:
        await db.close()


class SubtaskRequest(BaseModel):
    title: str
    assignee_slug: str = ""
    description: str = ""
    priority: str = "none"


@router.post("/{pid}/tasks/{task_id}/subtasks", dependencies=[Depends(require_admin)])
async def create_subtask(pid: int, task_id: int, req: SubtaskRequest, user: dict = Depends(require_admin)):
    await _ensure_project(pid)
    if not req.title.strip():
        raise HTTPException(400, "子任务标题不能为空")
    db = await get_connection()
    try:
        # 只允许在顶层任务下建子任务：子任务不能再有子任务（避免多层派生）
        parent = await (await db.execute(
            "SELECT parent_task_id FROM tasks WHERE id=? AND project_id=?", (task_id, pid))).fetchone()
        if not parent:
            raise HTTPException(404, "父任务不存在")
        if parent["parent_task_id"] is not None:
            raise HTTPException(400, "子任务下不能再创建子任务")
        prio = req.priority if req.priority in PRIORITIES else "none"
        conv = await db.execute(
            "INSERT INTO conversations (project_id, title) VALUES (?,?)", (pid, req.title.strip()))
        cur = await db.execute(
            """INSERT INTO tasks (project_id, title, description, assignee_slug, conversation_id,
                                  parent_task_id, priority, status)
               VALUES (?,?,?,?,?,?,?, 'backlog')""",
            (pid, req.title.strip(), req.description.strip(), req.assignee_slug,
             conv.lastrowid, task_id, prio))
        await db.commit()
        row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,))).fetchone()
        result = dict(row)
    finally:
        await db.close()
    await log_activity(task_id, "commented", "user", user.get("username", ""),
                       {"note": f"新增子任务：{req.title.strip()}"})
    return result
