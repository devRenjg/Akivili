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

    # 人工指令里 @ 的其余成员一并唤醒（复用协同队列）：主受理人(agent)走下方流式即时执行，
    # prompt 里额外 @ 的成员由 parse_and_enqueue_mentions 各入队一个 run，由协同后台循环串行执行。
    # 把主受理人作为 author_slug 传入 → 它不会被重复入队（避免与流式那次撞车）。
    primary_slug = agent["slug"]
    leader_slug = await collab.get_leader_slug(task["project_id"])
    try:
        await collab.parse_and_enqueue_mentions(
            task_id, task["project_id"], req.prompt, primary_slug, leader_slug)
    except Exception:  # noqa: BLE001
        pass  # @ 解析失败不阻断主受理人执行
    collab.start_loop()  # 确保协同后台循环在跑，能领取上面入队的成员 run（幂等）

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


async def _reactivate_on_redispatch(task_id: int, parent_id, status: str) -> None:
    """重跑（重新触发执行）时，把已收尾的任务及其父任务即时回写 in_progress。

    否则前端要等 3 秒轮询聚合 progress 才把状态从「已完成/验证中」翻成「进行中」，出现滞后窗口。
    只在任务当前是 done/reviewing（确属重跑，而非首次执行）时回写；in_progress/backlog 不动。
    """
    from activity import log_activity
    targets = []
    if status in ("done", "reviewing"):
        targets.append(task_id)
    db = await get_connection()
    try:
        if parent_id:
            prow = await (await db.execute(
                "SELECT status FROM tasks WHERE id=?", (parent_id,))).fetchone()
            if prow and prow["status"] in ("done", "reviewing"):
                targets.append(parent_id)
        for tid in targets:
            await db.execute(
                "UPDATE tasks SET status='in_progress', updated_at=datetime('now') WHERE id=?", (tid,))
        if targets:
            await db.commit()
    finally:
        await db.close()
    for tid in targets:
        await log_activity(tid, "status_changed", "system", "",
                           {"to": "in_progress", "note": "重新触发执行，回到进行中"})


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

    # 重跑即时回写：若该任务已 done/reviewing（重新触发执行），立即把它——以及其父任务
    # （若已 done/reviewing）——回写 in_progress，不等 3 秒轮询聚合，消除「先显已完成、隔几秒才变进行中」的滞后。
    await _reactivate_on_redispatch(task_id, task.get("parent_task_id"), task.get("status"))

    # 任务 Owner 唤醒。区分叶子子任务 vs 顶层任务：
    # - 子任务（有 parent_task_id）：以**普通成员身份**执行（is_leader=False, trigger=assign）。
    #   否则 leader run 不会触发叶子任务的状态推进（_run_one 里推进条件含 `not is_leader`），
    #   子任务会「成功却卡在 in_progress」，进而拖住父任务收尾（见 task 70/77 事故）。
    # - 顶层任务：以负责人身份统筹（注入协作协议+花名册，可拉人协调、收尾汇总）。
    owner = task.get("assignee_slug")
    if owner:
        prompt = (task.get("description") or task.get("title") or "").strip()
        is_subtask = bool(task.get("parent_task_id"))
        if is_subtask:
            await collab.enqueue_run(task_id, owner, prompt, "assign", is_leader=False)
            return {"ok": True, "mode": "assign", "owner": owner}
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


def _dur_seconds(started: str | None, ended: str | None) -> float | None:
    """算 started~ended 的秒数（SQLite datetime 文本，UTC）。缺失返回 None。"""
    if not started or not ended:
        return None
    from datetime import datetime
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return (datetime.strptime(ended[:19], fmt) - datetime.strptime(started[:19], fmt)).total_seconds()
    except (ValueError, TypeError):
        return None


@router.get("/tasks/{task_id}/lineage")
async def get_lineage(task_id: int):
    """端到端链路下钻（P3-2/P3-1）：一次拼出该任务（含子任务）的完整执行链——
    每个 run_queue 项 + 关联 task_run（经 P1-1 的 task_run_id）+ 耗时 + fail_reason
    + 因果源（P1-3 source_run_id/message_id）+ run_events 调度流水（P2-1），
    并聚合链路级耗时。替代此前需人工跨 5 张表拼时间线的排查方式。"""
    db = await get_connection()
    try:
        # 本任务 + 子任务全集
        trows = await (await db.execute(
            "SELECT id, title, status, parent_task_id FROM tasks WHERE id=? OR parent_task_id=?",
            (task_id, task_id))).fetchall()
        if not trows:
            raise HTTPException(404, "任务不存在")
        tids = [r["id"] for r in trows]
        ph = ",".join("?" for _ in tids)
        # run_queue 项（含关联 task_run 的执行信息，经 task_run_id 打通）
        qrows = await (await db.execute(
            f"""SELECT q.id AS rq_id, q.task_id, q.agent_slug, q.trigger, q.is_leader,
                       q.status AS queue_status, q.attempts, q.created_at AS enqueued_at,
                       q.task_run_id, q.source_run_id, q.source_message_id,
                       tr.status AS run_status, tr.fail_reason,
                       tr.started_at, tr.ended_at
                FROM run_queue q LEFT JOIN task_runs tr ON tr.id = q.task_run_id
                WHERE q.task_id IN ({ph}) ORDER BY q.id""", tids)).fetchall()
        # run_events 调度流水（按 run_queue 分组）
        erows = await (await db.execute(
            f"SELECT run_queue_id, event, detail, ts FROM run_events "
            f"WHERE task_id IN ({ph}) ORDER BY id", tids)).fetchall()
    finally:
        await db.close()

    events_by_rq: dict = {}
    for e in erows:
        events_by_rq.setdefault(e["run_queue_id"], []).append(
            {"event": e["event"], "detail": e["detail"], "ts": to_beijing(e["ts"])})

    chain = []
    total_run_seconds = 0.0
    for q in qrows:
        d = dict(q)
        dur = _dur_seconds(d.get("started_at"), d.get("ended_at"))
        if dur:
            total_run_seconds += dur
        chain.append({
            "run_queue_id": d["rq_id"], "task_id": d["task_id"], "agent_slug": d["agent_slug"],
            "trigger": d["trigger"], "is_leader": bool(d["is_leader"]),
            "queue_status": d["queue_status"], "attempts": d["attempts"],
            "enqueued_at": to_beijing(d["enqueued_at"]),
            "task_run_id": d["task_run_id"], "run_status": d["run_status"],
            "fail_reason": d["fail_reason"] or "",
            "started_at": to_beijing(d["started_at"]), "ended_at": to_beijing(d["ended_at"]),
            "duration_seconds": dur,
            "source_run_id": d["source_run_id"], "source_message_id": d["source_message_id"],
            "events": events_by_rq.get(d["rq_id"], []),
        })

    return {
        "task_id": task_id,
        "task_count": len(tids),
        "run_count": len(chain),
        "total_run_seconds": round(total_run_seconds, 1),
        "failed_runs": [c for c in chain if c["run_status"] == "failed"],
        "chain": chain,
    }


@router.get("/runs/rate-limit-metrics")
async def rate_limit_metrics(hours: int = 24):
    """限流/429 命中率观测（判断并发是否撞上游账号限流）。

    统计最近 `hours` 小时内进入终态的 run（task_runs.ended_at 落在窗口内）：
      - total_runs：窗口内终态 run 总数
      - failed_runs：其中失败数
      - rate_limited_runs：失败归因为 rate_limited（撞 429/限流/overload/quota）的数量
      - rate_limit_hit_rate：rate_limited / total_runs（占全部执行的比例）
      - rate_limit_fail_share：rate_limited / failed_runs（占失败的比例）
      - by_fail_reason：窗口内各失败归因分布（便于对比限流 vs 其它失败）
    命中率高说明瓶颈在 CLI 账号侧，加并发只会更多撞 429——此时应考虑多账号分流而非加并发。
    """
    hours = max(1, min(int(hours), 720))   # 1h~30d
    since = f"-{hours} hours"
    db = await get_connection()
    try:
        total = (await (await db.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE ended_at IS NOT NULL "
            "AND ended_at >= datetime('now', ?)", (since,))).fetchone())["c"]
        failed = (await (await db.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE status='failed' AND ended_at IS NOT NULL "
            "AND ended_at >= datetime('now', ?)", (since,))).fetchone())["c"]
        rl = (await (await db.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE fail_reason='rate_limited' AND ended_at IS NOT NULL "
            "AND ended_at >= datetime('now', ?)", (since,))).fetchone())["c"]
        dist_rows = await (await db.execute(
            "SELECT COALESCE(NULLIF(fail_reason,''),'(none)') fr, COUNT(*) c FROM task_runs "
            "WHERE status='failed' AND ended_at IS NOT NULL AND ended_at >= datetime('now', ?) "
            "GROUP BY fr ORDER BY c DESC", (since,))).fetchall()
    finally:
        await db.close()
    return {
        "window_hours": hours,
        "total_runs": total,
        "failed_runs": failed,
        "rate_limited_runs": rl,
        "rate_limit_hit_rate": round(rl / total, 4) if total else 0.0,
        "rate_limit_fail_share": round(rl / failed, 4) if failed else 0.0,
        "by_fail_reason": {r["fr"]: r["c"] for r in dist_rows},
    }
