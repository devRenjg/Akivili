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
from redact import redact_secrets
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


@router.post("/tasks/{task_id}/dispatch")
async def dispatch(task_id: int, req: DispatchRequest, request: Request,
                   user: dict = Depends(require_admin)):
    task, agent = await _load_task_and_agent(task_id, req.assignee_slug)
    if not task:
        raise HTTPException(404, "任务不存在")
    if not agent:
        raise HTTPException(400, "任务未指定有效负责人，请先 @ 一位团队成员或为任务设负责人")

    # thread 里人手输入的指令：落成 user 消息，署当前登录用户名
    user_name = user.get("username", "")

    async def event_stream():
        try:
            async for ev in runner.execute_dispatch(task, agent, req.prompt,
                                                     persist_user_msg=True, user_name=user_name):
                if await request.is_disconnected():
                    break
                payload = {"type": ev.type, "text": ev.text, "meta": ev.meta}
                # 工具事件带上完整命令/输出，供前端实时展示（脱敏后）
                if ev.tool or ev.tool_input or ev.tool_output:
                    payload["tool"] = ev.tool
                    payload["tool_input"] = {
                        k: (redact_secrets(v) if isinstance(v, str) else v)
                        for k, v in (ev.tool_input or {}).items()
                    }
                    payload["tool_output"] = redact_secrets(ev.tool_output or "")
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
            # prompt 来自任务描述（非真人在 thread 输入），不落 user 消息避免以「我」复述任务
            async for _ in runner.execute_dispatch(task, agent, prompt, persist_user_msg=False):
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
    """任务的执行历史列表。每条附一行 summary（命令缩略版）供执行日志区紧凑展示。"""
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
            d["summary"] = await _run_summary(db, r["id"])
            out.append(d)
        return {"runs": out}
    finally:
        await db.close()


async def _run_summary(db, run_id: int) -> str:
    """一行运行摘要：取这次会话开始的几句话（Agent 的开场发言/文本），而非工具命令。

    优先该 run 的首条助手文本（stdout / thinking）；都没有时才回退到首个工具动作。
    """
    # 首条会话文本（助手流式发言优先，其次思考）——即"会话开始的前几句话"
    trow = await (await db.execute(
        "SELECT content FROM run_logs WHERE run_id=? AND channel IN ('stdout','thinking') "
        "ORDER BY id LIMIT 1", (run_id,))).fetchone()
    if trow and (trow["content"] or "").strip():
        s = trow["content"].strip().replace("\n", " ")
        return redact_secrets(s[:80])
    # 回退：首个工具动作（无任何文本时）
    tool = await (await db.execute(
        "SELECT tool, tool_input FROM run_logs WHERE run_id=? AND channel='tool' ORDER BY id LIMIT 1",
        (run_id,))).fetchone()
    if tool:
        import json as _json
        name = tool["tool"] or "工具"
        try:
            inp = _json.loads(tool["tool_input"]) if tool["tool_input"] else {}
        except (ValueError, TypeError):
            inp = {}
        key = ""
        for k in ("command", "file_path", "path", "pattern", "query", "description"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                key = v.strip().replace("\n", " ")
                break
        return redact_secrets((f"{name}: {key}" if key else f"调用 {name}")[:80])
    return ""


@router.get("/runs/{run_id}/logs")
async def get_logs(run_id: int):
    """精简日志（右侧执行日志区用）：只回 ts/channel/content，向后兼容旧调用。"""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT ts, channel, content FROM run_logs WHERE run_id=? ORDER BY id", (run_id,))).fetchall()
        return {"logs": [{**dict(r), "ts": to_beijing(r["ts"])} for r in rows]}
    finally:
        await db.close()


@router.get("/runs/{run_id}/transcript")
async def get_transcript(run_id: int):
    """日志详情：结构化事件序列，含每条工具调用的完整命令(tool_input)与输出(tool_output)。

    seq 用行 id（时间序）；tool_input 落库为 JSON 字符串，这里解析回对象。
    所有对外文本（content/tool_input/tool_output）统一脱敏后返回。
    """
    import json as _json
    db = await get_connection()
    try:
        run = await (await db.execute(
            "SELECT id, task_id, agent_slug, status, provider_id, started_at, ended_at "
            "FROM task_runs WHERE id=?", (run_id,))).fetchone()
        rows = await (await db.execute(
            "SELECT id, ts, channel, content, tool, tool_input, tool_output "
            "FROM run_logs WHERE run_id=? ORDER BY id", (run_id,))).fetchall()
    finally:
        await db.close()

    items = []
    for r in rows:
        d = dict(r)
        ti = {}
        if d.get("tool_input"):
            try:
                ti = _json.loads(d["tool_input"])
            except (ValueError, TypeError):
                ti = {"_raw": d["tool_input"]}
        # 脱敏 tool_input 各字段值
        if isinstance(ti, dict):
            ti = {k: (redact_secrets(v) if isinstance(v, str) else v) for k, v in ti.items()}
        items.append({
            "seq": d["id"],
            "ts": to_beijing(d["ts"]),
            "channel": d["channel"],
            "content": redact_secrets(d.get("content") or ""),
            "tool": d.get("tool") or "",
            "tool_input": ti,
            "tool_output": redact_secrets(d.get("tool_output") or ""),
        })

    meta = {}
    if run:
        m = dict(run)
        # 把内部 provider_id（hash）解析成人类可读的「供应商名 · 模型」
        prov_label = ""
        pid_str = m["provider_id"] or ""
        if pid_str:
            from config import load_settings
            for p in load_settings().providers:
                if p.id == pid_str:
                    prov_label = f"{p.name} · {p.model}" if p.model else p.name
                    break
            if not prov_label:
                prov_label = "（供应商已删除）"
        meta = {
            "run_id": m["id"], "task_id": m["task_id"], "agent_slug": m["agent_slug"],
            "status": m["status"], "provider_id": pid_str, "provider_label": prov_label,
            "started_at": to_beijing(m["started_at"]), "ended_at": to_beijing(m["ended_at"]),
        }
    return {"meta": meta, "items": items}
