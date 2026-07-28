"""项目内 Agent 团队：从模版导入 / 列表 / 自建 / 改造 / 移除。

导入时把模版的 name/emoji/color/body 复制成项目内实例的可编辑 persona，
此后改造只影响该项目实例，不动原模版、不影响其他项目。
"""
import re

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

import projects as projects_mod
from sqlalchemy import delete as sa_delete, literal_column, select, update as sa_update

from agent_memory_sync import sync_agent_memory
from auth import require_admin
from models import AgentProfile, AgentTemplate, ProjectAgent, get_session_factory

router = APIRouter(prefix="/api/projects", tags=["project-agents"])

# project_agents 物理列序（对齐 001 基线），SELECT pa.* → dict 保持键集合/顺序
_PA_COLS = (
    "id", "project_id", "template_id", "slug", "name", "emoji", "color",
    "persona", "provider_id", "enabled", "created_at", "is_leader",
)


def _pa_dict(pa: ProjectAgent) -> dict:
    return {c: getattr(pa, c) for c in _PA_COLS}


async def _ensure_project(pid: int):
    if not await projects_mod.get_project(pid):
        raise HTTPException(404, "项目不存在")


class ImportAgentRequest(BaseModel):
    template_id: int


class CreateAgentRequest(BaseModel):
    name: str
    persona: str = ""
    emoji: str = "🤖"
    color: str = ""
    provider_id: str = ""


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    persona: str | None = None
    provider_id: str | None = None
    emoji: str | None = None
    color: str | None = None
    enabled: int | None = None


@router.get("/{pid}/agents")
async def list_project_agents(pid: int):
    await _ensure_project(pid)
    # solved_tasks 相关子查询：逐字保留原 SQL 语义（嵌套 EXISTS + DISTINCT count），
    # 用 literal_column 承载原始标量子查询、绑定当前行 project_agents.slug，
    # 避免翻译嵌套逻辑出偏差。（text() 不支持 .label，故用 literal_column）
    solved = literal_column(
        "(SELECT COUNT(DISTINCT tr.task_id) FROM task_runs tr JOIN tasks tk ON tk.id = tr.task_id "
        "WHERE tr.agent_slug = project_agents.slug AND tr.status = 'succeeded' AND tk.status = 'done' "
        "AND (tk.parent_task_id IS NULL OR EXISTS "
        "(SELECT 1 FROM tasks pt WHERE pt.id = tk.parent_task_id)))"
    )
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(ProjectAgent,
                   AgentProfile.nickname.label("nickname"),
                   AgentProfile.avatar.label("avatar"),
                   solved.label("solved_tasks"))
            .select_from(ProjectAgent)
            .outerjoin(AgentProfile, AgentProfile.slug == ProjectAgent.slug)
            .where(ProjectAgent.project_id == pid)
            .order_by(ProjectAgent.is_leader.desc(), ProjectAgent.id))).all()
        agents = []
        for pa, nickname, avatar, solved_tasks in rows:
            d = _pa_dict(pa)
            d["nickname"] = nickname
            d["avatar"] = avatar
            d["solved_tasks"] = solved_tasks
            agents.append(d)
        return {"agents": agents}


@router.post("/{pid}/agents/import", dependencies=[Depends(require_admin)])
async def import_agent(pid: int, req: ImportAgentRequest):
    await _ensure_project(pid)
    async with get_session_factory()() as session:
        tpl = (await session.execute(
            select(AgentTemplate).where(AgentTemplate.id == req.template_id))).scalar_one_or_none()
        if not tpl:
            raise HTTPException(404, "模版不存在")
        pa = ProjectAgent(project_id=pid, template_id=tpl.id, slug=tpl.slug, name=tpl.name,
                          emoji=tpl.emoji, color=tpl.color, persona=tpl.body)
        session.add(pa)
        await session.commit()
        await session.refresh(pa)
        result = _pa_dict(pa)
    await sync_agent_memory(result["slug"])   # 把工作区写进该 Agent 记忆
    return result


@router.post("/{pid}/agents", dependencies=[Depends(require_admin)])
async def create_agent(pid: int, req: CreateAgentRequest):
    await _ensure_project(pid)
    if not req.name.strip():
        raise HTTPException(400, "Agent 名称不能为空")
    # 自建 Agent 的记忆 slug：custom-<项目>-<安全化名称>-<行号>，保证全局唯一且合法
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", req.name.strip()).strip("-") or "agent"
    async with get_session_factory()() as session:
        pa = ProjectAgent(project_id=pid, template_id=None, slug="", name=req.name.strip(),
                          emoji=req.emoji, color=req.color, persona=req.persona,
                          provider_id=req.provider_id)
        session.add(pa)
        await session.flush()   # 取 pa.id（对齐旧 lastrowid），再用它拼 slug
        aid = pa.id
        pa.slug = f"custom-{pid}-{safe}-{aid}"
        await session.commit()
        await session.refresh(pa)
        result = _pa_dict(pa)
    await sync_agent_memory(result["slug"])
    return result


@router.put("/{pid}/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def update_agent(pid: int, agent_id: int, req: UpdateAgentRequest):
    await _ensure_project(pid)
    allowed = {"name", "persona", "provider_id", "emoji", "color", "enabled"}
    sets = {k: v for k, v in req.model_dump().items() if k in allowed and v is not None}
    if not sets:
        raise HTTPException(400, "无可更新字段")
    async with get_session_factory()() as session:
        result = await session.execute(
            sa_update(ProjectAgent)
            .where(ProjectAgent.id == agent_id, ProjectAgent.project_id == pid)
            .values(**sets))
        await session.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "该项目下不存在此 Agent")
        pa = (await session.execute(
            select(ProjectAgent).where(ProjectAgent.id == agent_id))).scalar_one()
        return _pa_dict(pa)


@router.delete("/{pid}/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def remove_agent(pid: int, agent_id: int):
    await _ensure_project(pid)
    async with get_session_factory()() as session:
        row = (await session.execute(
            select(ProjectAgent.slug)
            .where(ProjectAgent.id == agent_id, ProjectAgent.project_id == pid))).first()
        result = await session.execute(
            sa_delete(ProjectAgent)
            .where(ProjectAgent.id == agent_id, ProjectAgent.project_id == pid))
        await session.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "该项目下不存在此 Agent")
        slug = row.slug
    await sync_agent_memory(slug)   # 从剩余项目重建工作区段落
    return {"ok": True}


@router.put("/{pid}/agents/{agent_id}/leader", dependencies=[Depends(require_admin)])
async def set_leader(pid: int, agent_id: int):
    """把某成员设为团队总负责人（Team Leader）；自动取消原负责人。每项目至多一个。"""
    await _ensure_project(pid)
    async with get_session_factory()() as session:
        exists = (await session.execute(
            select(ProjectAgent.id)
            .where(ProjectAgent.id == agent_id, ProjectAgent.project_id == pid))).first()
        if not exists:
            raise HTTPException(404, "该项目下不存在此 Agent")
        # 先清本项目所有负责人标记，再设当前为负责人（每项目至多一个）
        await session.execute(
            sa_update(ProjectAgent).where(ProjectAgent.project_id == pid).values(is_leader=0))
        await session.execute(
            sa_update(ProjectAgent).where(ProjectAgent.id == agent_id).values(is_leader=1))
        await session.commit()
        return {"ok": True}
