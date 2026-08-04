"""执行接口：@分派（SSE 流式）、kill、日志查询、对话历史。"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import projects as projects_mod
from sqlalchemy import distinct, func, select, update as sa_update

from executor import runner
from auth import require_admin
from models import (
    AgentProfile,
    Message,
    Project,
    ProjectAgent,
    RunEvent,
    RunLog,
    RunQueue,
    Task,
    TaskRun,
    get_session_factory,
    now_expr,
    now_offset,
    elapsed_seconds,
)
from timeutil import to_beijing
from redact import redact_secrets
import collab

router = APIRouter(prefix="/api", tags=["runs"])

# task_runs 物理列序（对齐 001 基线），SELECT * → dict 保持键集合/顺序
_TASK_RUN_COLS = (
    "id", "task_id", "conversation_id", "agent_slug", "status", "provider_id",
    "pid", "started_at", "ended_at", "fail_reason",
)
# project_agents 物理列序（_load_task_and_agent 的 SELECT * 用）
_PA_COLS = (
    "id", "project_id", "template_id", "slug", "name", "emoji", "color",
    "persona", "provider_id", "enabled", "created_at", "is_leader",
)
# tasks 物理列序（_load_task_and_agent / auto_dispatch 的 SELECT t.* 用）
_TASK_COLS = (
    "id", "project_id", "title", "description", "status", "assignee_slug",
    "conversation_id", "order_idx", "created_at", "updated_at", "priority", "parent_task_id",
)


class DispatchRequest(BaseModel):
    prompt: str
    assignee_slug: str = ""   # 可临时指定（@某人）；空则用任务负责人


async def _load_task_and_agent(task_id: int, override_slug: str):
    """取任务（含项目路径）与负责 Agent（含 persona + 生效 provider_id）。"""
    async with get_session_factory()() as session:
        trow = (await session.execute(
            select(Task, Project.local_path.label("project_dir"))
            .join(Project, Project.id == Task.project_id)
            .where(Task.id == task_id))).first()
        if not trow:
            return None, None
        t_obj, project_dir = trow
        task = {c: getattr(t_obj, c) for c in _TASK_COLS}
        task["project_dir"] = project_dir
        slug = override_slug or task["assignee_slug"]
        if not slug:
            return task, None
        pa = (await session.execute(
            select(ProjectAgent)
            .where(ProjectAgent.project_id == task["project_id"], ProjectAgent.slug == slug)
            .limit(1))).scalar_one_or_none()
        if not pa:
            return task, None
        agent = {c: getattr(pa, c) for c in _PA_COLS}
        # 生效 provider：按 slug 从 agent_profiles 取（跨项目共享的接入模型）
        prof = (await session.execute(
            select(AgentProfile.provider_id).where(AgentProfile.slug == slug))).first()
        agent["provider_id_effective"] = prof.provider_id if prof else ""
        return task, agent


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
    # worker-split-minimal 组 1：入队即可，队列由独立 worker 进程的 _loop 领取执行。
    # API 进程不再 start_loop（执行面已剥离到 worker.py）——上面 @ 入队的成员 run 由 worker 领。

    async def event_stream():
        # 显式持有底层生成器：客户端断连时 aclose() 它，触发 execute_dispatch 的中断兜底
        # 及时把 task_runs 补落终态，杜绝孤儿 running（run#183/#185 泄漏事故：break 后生成器被丢弃、
        # 收尾不确定性执行，run 长期假 running）。
        agen = runner.execute_dispatch(task, agent, req.prompt,
                                       persist_user_msg=True, user_name=user_name)
        try:
            async for ev in agen:
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
        finally:
            # 无论正常结束、断连 break、还是异常，都确定性关闭生成器：
            # 若生成器仍停在 yield（断连 break 的情形），aclose() 抛入 GeneratorExit，
            # 触发 execute_dispatch 里的中断兜底补落终态。已自然收尾则是无害空操作。
            await agen.aclose()

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
    async with get_session_factory()() as session:
        if parent_id:
            prow = (await session.execute(
                select(Task.status).where(Task.id == parent_id))).first()
            if prow and prow.status in ("done", "reviewing"):
                targets.append(parent_id)
        for tid in targets:
            await session.execute(sa_update(Task).where(Task.id == tid).values(
                status="in_progress", updated_at=now_expr()))
        if targets:
            await session.commit()
    for tid in targets:
        await log_activity(tid, "status_changed", "system", "",
                           {"to": "in_progress", "note": "重新触发执行，回到进行中"})


async def _first_mentioned_slug(project_id: int, text: str) -> str:
    """从任务描述里解析首个 @ 成员，按项目成员名匹配，返回其 slug。"""
    if not text or "@" not in text:
        return ""
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(ProjectAgent.slug, ProjectAgent.name)
            .where(ProjectAgent.project_id == project_id))).all()
    members = [(r.slug, r.name) for r in rows]
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
    async with get_session_factory()() as session:
        trow = (await session.execute(
            select(Task, Project.local_path.label("project_dir"))
            .join(Project, Project.id == Task.project_id)
            .where(Task.id == task_id))).first()
        if not trow:
            raise HTTPException(404, "任务不存在")
        t_obj, project_dir = trow
        task = {c: getattr(t_obj, c) for c in _TASK_COLS}
        task["project_dir"] = project_dir

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
    """终止一个 run。worker-split-minimal 组 1 · D 类跨进程 kill：

    执行面剥离后，run 的 CLI 子进程可能在两个不同进程名下：
      - **直连路径**（做法 A，仍在本 API 进程）：run_id 在本进程 runner._RUN_PIDS 里 →
        kill_run 直接杀，立即生效。
      - **队列路径**（在独立 worker 进程）：本进程 _RUN_PIDS 无此 run，kill_run 返回 False →
        落 DB 信号 task_runs.kill_requested_at（对标 Multica「状态即信号」），worker 周期 sweep
        扫到后在其进程内 kill_run + finalize。前端收到 accepted=True 表示信号已投递（异步生效）。
    """
    # ① 先试本进程（直连路径的 run 能立即杀）
    killed_local = runner.kill_run(req.run_id)
    if killed_local:
        return {"ok": True, "mode": "local"}
    # ② 本进程没有 → 落 DB 信号，交给 worker 进程消费（队列路径）。
    #    仅对仍 running 的 run 落信号；已终态的 run 无需 kill（幂等、避免给历史 run 打标记）。
    async with get_session_factory()() as session:
        res = await session.execute(
            sa_update(TaskRun)
            .where(TaskRun.id == req.run_id, TaskRun.status == "running")
            .values(kill_requested_at=now_expr()))
        await session.commit()
    signaled = res.rowcount > 0
    # ok=True 表示「已受理」：本进程直杀成功，或已给在跑的 run 投递跨进程 kill 信号。
    # signaled=False（且非本地杀）通常是 run 已不在 running（早已结束）——对前端等价于「已终止」。
    return {"ok": True, "mode": "signal" if signaled else "noop", "signaled": signaled}


@router.get("/tasks/{task_id}/messages")
async def get_messages(task_id: int):
    async with get_session_factory()() as session:
        task = (await session.execute(
            select(Task.conversation_id).where(Task.id == task_id))).first()
        if not task:
            raise HTTPException(404, "任务不存在")
        rows = (await session.execute(
            select(Message.role, Message.content, Message.created_at)
            .where(Message.conversation_id == task.conversation_id)
            .order_by(Message.id))).all()
        return {"messages": [{"role": r.role, "content": r.content,
                              "created_at": to_beijing(r.created_at)} for r in rows]}


@router.get("/tasks/{task_id}/runs")
async def get_runs(task_id: int):
    """任务的执行历史列表。每条附一行 summary（命令缩略版）供执行日志区紧凑展示。"""
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(TaskRun).where(TaskRun.task_id == task_id)
            .order_by(TaskRun.id.desc()))).scalars().all()
        out = []
        for r in rows:
            d = {c: getattr(r, c) for c in _TASK_RUN_COLS}
            for col in ("started_at", "ended_at"):
                d[col] = to_beijing(d[col])
            d["summary"] = await _run_summary(session, r.id)
            out.append(d)
        return {"runs": out}


async def _run_summary(session, run_id: int) -> str:
    """一行运行摘要：取这次会话开始的几句话（Agent 的开场发言/文本），而非工具命令。

    优先该 run 的首条助手文本（stdout / thinking）；都没有时才回退到首个工具动作。
    """
    # 首条会话文本（助手流式发言优先，其次思考）——即"会话开始的前几句话"
    trow = (await session.execute(
        select(RunLog.content)
        .where(RunLog.run_id == run_id, RunLog.channel.in_(("stdout", "thinking")))
        .order_by(RunLog.id).limit(1))).first()
    if trow and (trow.content or "").strip():
        s = trow.content.strip().replace("\n", " ")
        return redact_secrets(s[:80])
    # 回退：首个工具动作（无任何文本时）
    tool = (await session.execute(
        select(RunLog.tool, RunLog.tool_input)
        .where(RunLog.run_id == run_id, RunLog.channel == "tool")
        .order_by(RunLog.id).limit(1))).first()
    if tool:
        import json as _json
        name = tool.tool or "工具"
        try:
            inp = _json.loads(tool.tool_input) if tool.tool_input else {}
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
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(RunLog.ts, RunLog.channel, RunLog.content)
            .where(RunLog.run_id == run_id).order_by(RunLog.id))).all()
        return {"logs": [{"ts": to_beijing(r.ts), "channel": r.channel, "content": r.content}
                         for r in rows]}


@router.get("/runs/{run_id}/transcript")
async def get_transcript(run_id: int):
    """日志详情：结构化事件序列，含每条工具调用的完整命令(tool_input)与输出(tool_output)。

    seq 用行 id（时间序）；tool_input 落库为 JSON 字符串，这里解析回对象。
    所有对外文本（content/tool_input/tool_output）统一脱敏后返回。
    """
    import json as _json
    async with get_session_factory()() as session:
        run = (await session.execute(
            select(TaskRun.id, TaskRun.task_id, TaskRun.agent_slug, TaskRun.status,
                   TaskRun.provider_id, TaskRun.started_at, TaskRun.ended_at)
            .where(TaskRun.id == run_id))).first()
        rows = (await session.execute(
            select(RunLog.id, RunLog.ts, RunLog.channel, RunLog.content,
                   RunLog.tool, RunLog.tool_input, RunLog.tool_output)
            .where(RunLog.run_id == run_id).order_by(RunLog.id))).all()

    items = []
    for r in rows:
        ti = {}
        if r.tool_input:
            try:
                ti = _json.loads(r.tool_input)
            except (ValueError, TypeError):
                ti = {"_raw": r.tool_input}
        # 脱敏 tool_input 各字段值
        if isinstance(ti, dict):
            ti = {k: (redact_secrets(v) if isinstance(v, str) else v) for k, v in ti.items()}
        items.append({
            "seq": r.id,
            "ts": to_beijing(r.ts),
            "channel": r.channel,
            "content": redact_secrets(r.content or ""),
            "tool": r.tool or "",
            "tool_input": ti,
            "tool_output": redact_secrets(r.tool_output or ""),
        })

    meta = {}
    if run:
        # 把内部 provider_id（hash）解析成人类可读的「供应商名 · 模型」
        prov_label = ""
        pid_str = run.provider_id or ""
        if pid_str:
            from config import load_settings
            for p in load_settings().providers:
                if p.id == pid_str:
                    prov_label = f"{p.name} · {p.model}" if p.model else p.name
                    break
            if not prov_label:
                prov_label = "（供应商已删除）"
        meta = {
            "run_id": run.id, "task_id": run.task_id, "agent_slug": run.agent_slug,
            "agent_display": (await collab.resolve_agent_displays([run.agent_slug])).get(
                run.agent_slug, run.agent_slug),
            "status": run.status, "provider_id": pid_str, "provider_label": prov_label,
            "started_at": to_beijing(run.started_at), "ended_at": to_beijing(run.ended_at),
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
    async with get_session_factory()() as session:
        # 本任务 + 子任务全集
        trows = (await session.execute(
            select(Task.id, Task.title, Task.status, Task.parent_task_id)
            .where((Task.id == task_id) | (Task.parent_task_id == task_id)))).all()
        if not trows:
            raise HTTPException(404, "任务不存在")
        tids = [r.id for r in trows]
        # run_queue 项（含关联 task_run 的执行信息，经 task_run_id 打通）
        qrows = (await session.execute(
            select(RunQueue.id.label("rq_id"), RunQueue.task_id, RunQueue.agent_slug,
                   RunQueue.trigger, RunQueue.is_leader,
                   RunQueue.status.label("queue_status"), RunQueue.attempts,
                   RunQueue.created_at.label("enqueued_at"),
                   RunQueue.task_run_id, RunQueue.source_run_id, RunQueue.source_message_id,
                   TaskRun.status.label("run_status"), TaskRun.fail_reason,
                   TaskRun.started_at, TaskRun.ended_at)
            .select_from(RunQueue)
            .outerjoin(TaskRun, TaskRun.id == RunQueue.task_run_id)
            .where(RunQueue.task_id.in_(tids)).order_by(RunQueue.id))).all()
        # run_events 调度流水（按 run_queue 分组）
        erows = (await session.execute(
            select(RunEvent.run_queue_id, RunEvent.event, RunEvent.detail, RunEvent.ts)
            .where(RunEvent.task_id.in_(tids)).order_by(RunEvent.id))).all()

    events_by_rq: dict = {}
    for e in erows:
        events_by_rq.setdefault(e.run_queue_id, []).append(
            {"event": e.event, "detail": e.detail, "ts": to_beijing(e.ts)})

    # 统一展示名：slug → 「昵称（角色名）」，杜绝前端露 slug
    displays = await collab.resolve_agent_displays([q.agent_slug for q in qrows])

    chain = []
    total_run_seconds = 0.0
    for q in qrows:
        dur = _dur_seconds(q.started_at, q.ended_at)
        if dur:
            total_run_seconds += dur
        chain.append({
            "run_queue_id": q.rq_id, "task_id": q.task_id, "agent_slug": q.agent_slug,
            "agent_display": displays.get(q.agent_slug, q.agent_slug),
            "trigger": q.trigger, "is_leader": bool(q.is_leader),
            "queue_status": q.queue_status, "attempts": q.attempts,
            "enqueued_at": to_beijing(q.enqueued_at),
            "task_run_id": q.task_run_id, "run_status": q.run_status,
            "fail_reason": q.fail_reason or "",
            "started_at": to_beijing(q.started_at), "ended_at": to_beijing(q.ended_at),
            "duration_seconds": dur,
            "source_run_id": q.source_run_id, "source_message_id": q.source_message_id,
            "events": events_by_rq.get(q.rq_id, []),
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
    # 窗口下界：now - N 小时。now_offset 方言感知（SQLite→datetime('now','-N seconds')、
    # PG→now()-interval），产出与时间列同格式的 UTC text，供 ended_at>=window 的 text 比较。
    window = now_offset(-hours * 3600)
    async with get_session_factory()() as session:
        total = (await session.execute(
            select(func.count()).select_from(TaskRun)
            .where(TaskRun.ended_at.is_not(None), TaskRun.ended_at >= window))).scalar_one()
        failed = (await session.execute(
            select(func.count()).select_from(TaskRun)
            .where(TaskRun.status == "failed", TaskRun.ended_at.is_not(None),
                   TaskRun.ended_at >= window))).scalar_one()
        rl = (await session.execute(
            select(func.count()).select_from(TaskRun)
            .where(TaskRun.fail_reason == "rate_limited", TaskRun.ended_at.is_not(None),
                   TaskRun.ended_at >= window))).scalar_one()
        fr_col = func.coalesce(func.nullif(TaskRun.fail_reason, ""), "(none)").label("fr")
        dist_rows = (await session.execute(
            select(fr_col, func.count().label("c"))
            .where(TaskRun.status == "failed", TaskRun.ended_at.is_not(None),
                   TaskRun.ended_at >= window)
            .group_by(fr_col).order_by(func.count().desc()))).all()
    return {
        "window_hours": hours,
        "total_runs": total,
        "failed_runs": failed,
        "rate_limited_runs": rl,
        "rate_limit_hit_rate": round(rl / total, 4) if total else 0.0,
        "rate_limit_fail_share": round(rl / failed, 4) if failed else 0.0,
        "by_fail_reason": {r.fr: r.c for r in dist_rows},
    }


@router.get("/runs/agents-overview")
async def agents_overview(days: int = 30):
    """实时 Agent 总览：与「按任务筛选看历史链路」并存，提供一个全局实时视角。

    - stats：选定时间窗口（最近 `days` 天，按 run 的 started_at 过滤）内的累计口径——
      跑过多少个 run、多少失败、涉及多少个（去重）Agent、所有 Agent 累计运行总时长
      （已结束 run 的 ended_at-started_at 求和，秒）。不暴露限流/429（暂无需求）。
    - running：当前正在执行的 Agent（task_runs.status='running'，覆盖并发池 + 直接 @ 两条
      路径），带项目/任务信息与开始时间。**实时态，不受时间窗口影响。**
    - idle：启用中的团队成员里，此刻没有 running run 的，显示为 idle。**实时态，不受窗口影响。**
      身份粒度为 (project_id, slug)——同一花名册成员在不同项目算不同在岗实例。

    `days`：时间窗口天数，clamp 到 1..365（覆盖最近一个月=30、最近半年=180、最近一年=365，及用户自填）。
    """
    days = max(1, min(int(days), 365))
    # 窗口下界：now - N 天（now_offset 方言感知，同 rate_limit_metrics）。
    window = now_offset(-days * 86400)
    async with get_session_factory()() as session:
        # 累计 stats：限定 started_at 落在最近 days 天内
        total_runs = (await session.execute(
            select(func.count()).select_from(TaskRun)
            .where(TaskRun.started_at.is_not(None), TaskRun.started_at >= window))).scalar_one()
        failed_runs = (await session.execute(
            select(func.count()).select_from(TaskRun)
            .where(TaskRun.status == "failed", TaskRun.started_at.is_not(None),
                   TaskRun.started_at >= window))).scalar_one()
        distinct_agents = (await session.execute(
            select(func.count(distinct(TaskRun.agent_slug)))
            .where(TaskRun.started_at.is_not(None), TaskRun.started_at >= window))).scalar_one()
        # 累计运行总时长：已结束 run 的 (ended_at - started_at) 求和（秒）；
        # elapsed_seconds 方言感知（SQLite→julianday 差、PG→EXTRACT EPOCH）。
        total_seconds = (await session.execute(
            select(func.coalesce(
                func.sum(elapsed_seconds(TaskRun.ended_at, TaskRun.started_at)),
                0))
            .where(TaskRun.ended_at.is_not(None), TaskRun.started_at.is_not(None),
                   TaskRun.started_at >= window))).scalar_one()

        # 正在运行：task_runs.status='running' → 关联任务、项目
        run_rows = (await session.execute(
            select(TaskRun.id.label("task_run_id"), TaskRun.agent_slug, TaskRun.task_id,
                   TaskRun.started_at, Task.title.label("task_title"), Task.parent_task_id,
                   Task.project_id, Project.title.label("project_title"))
            .select_from(TaskRun)
            .outerjoin(Task, Task.id == TaskRun.task_id)
            .outerjoin(Project, Project.id == Task.project_id)
            .where(TaskRun.status == "running")
            .order_by(TaskRun.started_at))).all()

        # 在岗成员花名册：启用中的 (project_id, slug)，附项目名 + 角色名 + is_leader
        roster_rows = (await session.execute(
            select(ProjectAgent.project_id, ProjectAgent.slug, ProjectAgent.name,
                   ProjectAgent.is_leader, Project.title.label("project_title"))
            .select_from(ProjectAgent)
            .outerjoin(Project, Project.id == ProjectAgent.project_id)
            .where(ProjectAgent.enabled == 1)
            .order_by(ProjectAgent.project_id, ProjectAgent.is_leader.desc(),
                      ProjectAgent.slug))).all()

    slugs = list({r.agent_slug for r in run_rows} | {r.slug for r in roster_rows})
    displays = await collab.resolve_agent_displays(slugs)

    running = []
    busy_keys = set()   # (project_id, slug) 正在忙的在岗实例
    for r in run_rows:
        busy_keys.add((r.project_id, r.agent_slug))
        running.append({
            "task_run_id": r.task_run_id,
            "agent_slug": r.agent_slug,
            "agent_display": displays.get(r.agent_slug, r.agent_slug),
            "project_id": r.project_id, "project_title": r.project_title,
            "task_id": r.task_id, "task_title": r.task_title,
            "is_subtask": r.parent_task_id is not None,
            "parent_task_id": r.parent_task_id,
            "started_at": to_beijing(r.started_at),
        })

    idle = []
    for r in roster_rows:
        if (r.project_id, r.slug) in busy_keys:
            continue
        idle.append({
            "agent_slug": r.slug,
            "agent_display": displays.get(r.slug, r.slug),
            "project_id": r.project_id, "project_title": r.project_title,
            "is_leader": bool(r.is_leader),
        })

    return {
        "window_days": days,
        "stats": {
            "total_runs": total_runs,
            "failed_runs": failed_runs,
            "distinct_agents": distinct_agents,
            "total_run_seconds": round(total_seconds, 1),
        },
        "running_count": len(running),
        "idle_count": len(idle),
        "running": running,
        "idle": idle,
    }
