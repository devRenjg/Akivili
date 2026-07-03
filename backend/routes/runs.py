"""执行接口：@分派（SSE 流式）、kill、日志查询、对话历史。"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import projects as projects_mod
from database import get_connection
from executor import runner
from auth import require_admin
from timeutil import to_beijing
import collab

router = APIRouter(prefix="/api", tags=["runs"])


class DispatchRequest(BaseModel):
    prompt: str
    assignee_slug: str = ""   # 可临时指定（@某人）；空则用任务负责人


async def _load_task_and_agent(task_id: int, override_slug: str):
    """取任务（含项目路径）与负责 Agent（含 persona + 生效 provider_id）。"""
    db = await get_connection()
    try:
        task = await (await db.execute(
            """SELECT t.*, p.local_path AS project_dir
               FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id=?""", (task_id,))).fetchone()
        if not task:
            return None, None
        task = dict(task)
        slug = override_slug or task["assignee_slug"]
        if not slug:
            return task, None
        agent = await (await db.execute(
            "SELECT * FROM project_agents WHERE project_id=? AND slug=? LIMIT 1",
            (task["project_id"], slug))).fetchone()
        if not agent:
            return task, None
        agent = dict(agent)
        # 生效 provider：按 slug 从 agent_profiles 取（跨项目共享的接入模型）
        prof = await (await db.execute(
            "SELECT provider_id FROM agent_profiles WHERE slug=?", (slug,))).fetchone()
        agent["provider_id_effective"] = prof["provider_id"] if prof else ""
        return task, agent
    finally:
        await db.close()


@router.post("/tasks/{task_id}/dispatch", dependencies=[Depends(require_admin)])
async def dispatch(task_id: int, req: DispatchRequest, request: Request):
    task, agent = await _load_task_and_agent(task_id, req.assignee_slug)
    if not task:
        raise HTTPException(404, "任务不存在")
    if not agent:
        raise HTTPException(400, "任务未指定有效负责人，请先 @ 一位团队成员或为任务设负责人")

    async def event_stream():
        try:
            async for ev in runner.execute_dispatch(task, agent, req.prompt):
                if await request.is_disconnected():
                    break
                payload = {"type": ev.type, "text": ev.text, "meta": ev.meta}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type':'error','text':str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _first_mentioned_slug(project_id: int, text: str) -> str:
    """从任务描述里解析首个 @ 成员，按项目成员名匹配，返回其 slug。"""
    if not text or "@" not in text:
        return ""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT slug, name FROM project_agents WHERE project_id=?", (project_id,))).fetchall()
    finally:
        await db.close()
    members = [(r["slug"], r["name"]) for r in rows]
    # 按名字长度降序匹配，避免短名误命中
    members.sort(key=lambda x: len(x[1]), reverse=True)
    import re
    for m in re.finditer(r"@([^\s@]+)", text):
        token = m.group(1)
        for slug, name in members:
            if token.startswith(name) or name.startswith(token):
                return slug
    return ""


@router.post("/tasks/{task_id}/auto-dispatch", dependencies=[Depends(require_admin)])
async def auto_dispatch(task_id: int):
    """拖到「进行中」触发：唤醒该任务的负责人 Owner 统筹（对结果负责、拉人协调）。"""
    db = await get_connection()
    try:
        task = await (await db.execute(
            """SELECT t.*, p.local_path AS project_dir
               FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?""", (task_id,))).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在")
        task = dict(task)
    finally:
        await db.close()

    # 任务 Owner 统筹：以负责人身份唤醒（注入协作协议+花名册，可拉人协调）
    owner = task.get("assignee_slug")
    if owner:
        prompt = (task.get("description") or task.get("title") or "").strip()
        await collab.enqueue_run(task_id, owner, prompt, "collaborate", is_leader=True)
        return {"ok": True, "mode": "collaborate", "owner": owner}

    # 兜底：无 Owner（历史任务）→ 描述首个 @ 成员单跑
    slug = await _first_mentioned_slug(task["project_id"], task.get("description", ""))
    if not slug:
        raise HTTPException(400, "任务未指定负责人 Owner，请先编辑任务指定一位")
    _, agent = await _load_task_and_agent(task_id, slug)
    if not agent:
        raise HTTPException(400, "被 @ 的成员不在项目团队中")
    prompt = (task.get("description") or task.get("title") or "").strip()

    async def _run_bg():
        try:
            async for _ in runner.execute_dispatch(task, agent, prompt):
                pass
        except Exception:  # noqa: BLE001
            pass

    asyncio.create_task(_run_bg())
    return {"ok": True, "mode": "single", "assignee": slug}


class KillRequest(BaseModel):
    run_id: int


@router.post("/runs/kill", dependencies=[Depends(require_admin)])
async def kill(req: KillRequest):
    ok = runner.kill_run(req.run_id)
    return {"ok": ok}


@router.get("/tasks/{task_id}/messages")
async def get_messages(task_id: int):
    db = await get_connection()
    try:
        task = await (await db.execute("SELECT conversation_id FROM tasks WHERE id=?", (task_id,))).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在")
        rows = await (await db.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY id",
            (task["conversation_id"],))).fetchall()
        return {"messages": [{**dict(r), "created_at": to_beijing(r["created_at"])} for r in rows]}
    finally:
        await db.close()


@router.get("/tasks/{task_id}/runs")
async def get_runs(task_id: int):
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC", (task_id,))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for col in ("started_at", "ended_at"):
                if col in d:
                    d[col] = to_beijing(d[col])
            out.append(d)
        return {"runs": out}
    finally:
        await db.close()


@router.get("/runs/{run_id}/logs")
async def get_logs(run_id: int):
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT ts, channel, content FROM run_logs WHERE run_id=? ORDER BY id", (run_id,))).fetchall()
        return {"logs": [{**dict(r), "ts": to_beijing(r["ts"])} for r in rows]}
    finally:
        await db.close()
