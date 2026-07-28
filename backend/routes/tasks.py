"""任务接口：CRUD + 状态流转 + 归档 + 看板分组。创建任务时建关联对话 Thread。"""
from fastapi import APIRouter, HTTPException, Depends

from auth import require_admin
from pydantic import BaseModel

import projects as projects_mod
from sqlalchemy import delete as sa_delete, distinct, literal_column, select, update as sa_update
from sqlalchemy.orm import aliased

from activity import log_activity, timeline
from models import Conversation, Task, TaskRun, get_session_factory, now_expr
from timeutil import to_beijing

router = APIRouter(prefix="/api/projects", tags=["tasks"])

# 看板状态（有序）+ 中文标签
STATUSES = ["backlog", "in_progress", "reviewing", "done", "blocked"]
PRIORITIES = ["urgent", "high", "medium", "low", "none"]

# tasks 物理列序（对齐 001 基线），SELECT t.* → dict 保持键集合/顺序
_TASK_COLS = (
    "id", "project_id", "title", "description", "status", "assignee_slug",
    "conversation_id", "order_idx", "created_at", "updated_at", "priority", "parent_task_id",
)


def _task_dict(t: Task) -> dict:
    return {c: getattr(t, c) for c in _TASK_COLS}


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
    # 5 个相关子查询逐字保留原 SQL（绑定当前行 tasks 列），用 literal_column 承载。
    run_status = literal_column(
        "(SELECT status FROM task_runs r WHERE r.task_id=tasks.id ORDER BY r.id DESC LIMIT 1)")
    last_result = literal_column(
        "(SELECT content FROM messages m WHERE m.conversation_id=tasks.conversation_id "
        "AND m.role='assistant' ORDER BY m.id DESC LIMIT 1)")
    msg_count = literal_column(
        "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=tasks.conversation_id)")
    sub_total = literal_column(
        "(SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=tasks.id)")
    sub_done = literal_column(
        "(SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=tasks.id AND c.status='done')")
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(Task,
                   run_status.label("run_status"), last_result.label("last_result"),
                   msg_count.label("msg_count"), sub_total.label("sub_total"),
                   sub_done.label("sub_done"))
            .where(Task.project_id == pid, Task.parent_task_id.is_(None))
            .order_by(Task.created_at.desc(), Task.id.desc()))).all()
        tasks = []
        for t, rs, lr, mc, st, sd in rows:
            d = _task_dict(t)
            d.update(run_status=rs, last_result=lr, msg_count=mc, sub_total=st, sub_done=sd)
            tasks.append(d)
        # 取每个顶层任务的子任务（看板卡片下方嵌套小卡展示）
        sub_run_status = literal_column(
            "(SELECT status FROM task_runs r WHERE r.task_id=tasks.id ORDER BY r.id DESC LIMIT 1)")
        active_run = literal_column(
            "(SELECT COUNT(*) FROM run_queue q WHERE q.task_id=tasks.id "
            "AND q.status IN ('queued','running'))")
        sub_rows = (await session.execute(
            select(Task.id, Task.title, Task.status, Task.priority, Task.assignee_slug,
                   Task.parent_task_id,
                   sub_run_status.label("run_status"), active_run.label("active_run"))
            .where(Task.project_id == pid, Task.parent_task_id.is_not(None))
            .order_by(Task.order_idx, Task.id))).all()
        subs_by_parent: dict = {}
        for sr in sub_rows:
            subs_by_parent.setdefault(sr.parent_task_id, []).append({
                "id": sr.id, "title": sr.title, "status": sr.status, "priority": sr.priority,
                "assignee_slug": sr.assignee_slug, "parent_task_id": sr.parent_task_id,
                "run_status": sr.run_status, "active_run": sr.active_run})
        for t in tasks:
            for col in ("created_at", "updated_at"):
                if col in t:
                    t[col] = to_beijing(t[col])
            t["subtasks"] = subs_by_parent.get(t["id"], [])
        # 按状态分组，便于看板渲染
        board = {s: [t for t in tasks if t["status"] == s] for s in STATUSES}
        return {"tasks": tasks, "board": board}


@router.post("/{pid}/tasks", dependencies=[Depends(require_admin)])
async def create_task(pid: int, req: CreateTaskRequest, user: dict = Depends(require_admin)):
    await _ensure_project(pid)
    if not req.title.strip():
        raise HTTPException(400, "任务标题不能为空")
    async with get_session_factory()() as session:
        # 建关联对话 Thread
        conv = Conversation(project_id=pid, title=req.title.strip())
        session.add(conv)
        await session.flush()   # 取 conv.id（对齐旧 lastrowid）
        task = Task(project_id=pid, title=req.title.strip(), description=req.description,
                    assignee_slug=req.assignee_slug, conversation_id=conv.id,
                    priority=req.priority if req.priority in PRIORITIES else "none",
                    parent_task_id=req.parent_task_id, status="backlog")
        session.add(task)
        await session.commit()
        await session.refresh(task)
        tid = task.id
        result = _task_dict(task)
    await log_activity(tid, "created", "user", user.get("username", ""), {"title": req.title.strip()})
    return result


@router.put("/{pid}/tasks/{task_id}", dependencies=[Depends(require_admin)])
async def update_task(pid: int, task_id: int, req: UpdateTaskRequest, user: dict = Depends(require_admin)):
    await _ensure_project(pid)
    sets = {k: v for k, v in req.model_dump().items() if v is not None}
    if not sets:
        raise HTTPException(400, "无可更新字段")
    async with get_session_factory()() as session:
        old = (await session.execute(
            select(Task.priority).where(Task.id == task_id, Task.project_id == pid))).first()
        result_upd = await session.execute(
            sa_update(Task).where(Task.id == task_id, Task.project_id == pid)
            .values(**sets, updated_at=now_expr()))
        await session.commit()
        if result_upd.rowcount == 0:
            raise HTTPException(404, "任务不存在")
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        result = _task_dict(task)
    if "priority" in sets and old and old.priority != sets["priority"]:
        await log_activity(task_id, "priority_changed", "user", user.get("username", ""),
                           {"from": old.priority, "to": sets["priority"]})
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
    async with get_session_factory()() as session:
        old = (await session.execute(
            select(Task.status).where(Task.id == task_id, Task.project_id == pid))).first()
        result_upd = await session.execute(
            sa_update(Task).where(Task.id == task_id, Task.project_id == pid)
            .values(status=req.status, updated_at=now_expr()))
        await session.commit()
        if result_upd.rowcount == 0:
            raise HTTPException(404, "任务不存在")
        old_status = old.status if old else ""
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
    async with get_session_factory()() as session:
        # 本任务 + 其子任务的 id 集合（先收集，供删库与清记忆共用）
        srows = (await session.execute(
            select(Task.id).where(Task.parent_task_id == task_id, Task.project_id == pid))).all()
        all_ids = [task_id] + [r.id for r in srows]
        # 这些任务里真正跑过 run 的成员 slug（有 run = 有记忆沉淀），删库前查出（task_runs 会级联删）
        arows = (await session.execute(
            select(distinct(TaskRun.agent_slug))
            .where(TaskRun.task_id.in_(all_ids), TaskRun.agent_slug != ""))).all()
        slugs = [r[0] for r in arows]
        # 级联删除子任务 + 本任务（task_runs/run_logs 经外键 ON DELETE CASCADE 一并清）
        await session.execute(
            sa_delete(Task).where(Task.parent_task_id == task_id, Task.project_id == pid))
        result_del = await session.execute(
            sa_delete(Task).where(Task.id == task_id, Task.project_id == pid))
        await session.commit()
        if result_del.rowcount == 0:
            raise HTTPException(404, "任务不存在")

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
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(Task).where(Task.parent_task_id == task_id)
            .order_by(Task.order_idx, Task.id))).scalars().all()
        return {"subtasks": [_task_dict(t) for t in rows]}


@router.get("/{pid}/tasks/{task_id}")
async def get_task(pid: int, task_id: int):
    """单个任务详情（顶层或子任务通用）——子任务详情页靠它加载，看板列表只含顶层任务。"""
    await _ensure_project(pid)
    sub_total = literal_column(
        "(SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=tasks.id)")
    sub_done = literal_column(
        "(SELECT COUNT(*) FROM tasks c WHERE c.parent_task_id=tasks.id AND c.status='done')")
    parent = aliased(Task)   # 自 LEFT JOIN 取父任务标题
    async with get_session_factory()() as session:
        row = (await session.execute(
            select(Task, sub_total.label("sub_total"), sub_done.label("sub_done"),
                   parent.title.label("parent_title"))
            .select_from(Task)
            .outerjoin(parent, parent.id == Task.parent_task_id)
            .where(Task.id == task_id, Task.project_id == pid))).first()
        if not row:
            raise HTTPException(404, "任务不存在")
        t, st, sd, parent_title = row
        d = _task_dict(t)
        d.update(sub_total=st, sub_done=sd, parent_title=parent_title)
        for col in ("created_at", "updated_at"):
            if col in d:
                d[col] = to_beijing(d[col])
        return d


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
    async with get_session_factory()() as session:
        # 只允许在顶层任务下建子任务：子任务不能再有子任务（避免多层派生）
        parent = (await session.execute(
            select(Task.parent_task_id)
            .where(Task.id == task_id, Task.project_id == pid))).first()
        if not parent:
            raise HTTPException(404, "父任务不存在")
        if parent.parent_task_id is not None:
            raise HTTPException(400, "子任务下不能再创建子任务")
        prio = req.priority if req.priority in PRIORITIES else "none"
        conv = Conversation(project_id=pid, title=req.title.strip())
        session.add(conv)
        await session.flush()   # 取 conv.id
        sub = Task(project_id=pid, title=req.title.strip(), description=req.description.strip(),
                   assignee_slug=req.assignee_slug, conversation_id=conv.id,
                   parent_task_id=task_id, priority=prio, status="backlog")
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        result = _task_dict(sub)
    await log_activity(task_id, "commented", "user", user.get("username", ""),
                       {"note": f"新增子任务：{req.title.strip()}"})
    return result
