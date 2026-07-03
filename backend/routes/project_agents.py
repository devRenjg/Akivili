"""项目内 Agent 团队：从模版导入 / 列表 / 自建 / 改造 / 移除。

导入时把模版的 name/emoji/color/body 复制成项目内实例的可编辑 persona，
此后改造只影响该项目实例，不动原模版、不影响其他项目。
"""
import re

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

import projects as projects_mod
from database import get_connection
from agent_memory_sync import sync_agent_memory
from auth import require_admin

router = APIRouter(prefix="/api/projects", tags=["project-agents"])


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
    db = await get_connection()
    try:
        cur = await db.execute(
            """SELECT pa.*, p.nickname AS nickname, p.avatar AS avatar
               FROM project_agents pa
               LEFT JOIN agent_profiles p ON p.slug = pa.slug
               WHERE pa.project_id=? ORDER BY pa.is_leader DESC, pa.id""", (pid,))
        return {"agents": [dict(r) for r in await cur.fetchall()]}
    finally:
        await db.close()


@router.post("/{pid}/agents/import", dependencies=[Depends(require_admin)])
async def import_agent(pid: int, req: ImportAgentRequest):
    await _ensure_project(pid)
    db = await get_connection()
    try:
        tpl = await (await db.execute(
            "SELECT * FROM agent_templates WHERE id=?", (req.template_id,))).fetchone()
        if not tpl:
            raise HTTPException(404, "模版不存在")
        cur = await db.execute(
            """INSERT INTO project_agents
               (project_id, template_id, slug, name, emoji, color, persona)
               VALUES (?,?,?,?,?,?,?)""",
            (pid, tpl["id"], tpl["slug"], tpl["name"], tpl["emoji"], tpl["color"], tpl["body"]),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT * FROM project_agents WHERE id=?", (cur.lastrowid,))).fetchone()
        result = dict(row)
    finally:
        await db.close()
    await sync_agent_memory(result["slug"])   # 把工作区写进该 Agent 记忆
    return result


@router.post("/{pid}/agents", dependencies=[Depends(require_admin)])
async def create_agent(pid: int, req: CreateAgentRequest):
    await _ensure_project(pid)
    if not req.name.strip():
        raise HTTPException(400, "Agent 名称不能为空")
    # 自建 Agent 的记忆 slug：custom-<项目>-<安全化名称>-<行号>，保证全局唯一且合法
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", req.name.strip()).strip("-") or "agent"
    db = await get_connection()
    try:
        cur = await db.execute(
            """INSERT INTO project_agents
               (project_id, template_id, slug, name, emoji, color, persona, provider_id)
               VALUES (?, NULL, ?,?,?,?,?,?)""",
            (pid, "", req.name.strip(), req.emoji, req.color, req.persona, req.provider_id),
        )
        aid = cur.lastrowid
        slug = f"custom-{pid}-{safe}-{aid}"
        await db.execute("UPDATE project_agents SET slug=? WHERE id=?", (slug, aid))
        await db.commit()
        row = await (await db.execute(
            "SELECT * FROM project_agents WHERE id=?", (cur.lastrowid,))).fetchone()
        result = dict(row)
    finally:
        await db.close()
    await sync_agent_memory(result["slug"])
    return result


@router.put("/{pid}/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def update_agent(pid: int, agent_id: int, req: UpdateAgentRequest):
    await _ensure_project(pid)
    allowed = {"name", "persona", "provider_id", "emoji", "color", "enabled"}
    sets = {k: v for k, v in req.model_dump().items() if k in allowed and v is not None}
    if not sets:
        raise HTTPException(400, "无可更新字段")
    cols = ", ".join(f"{k}=?" for k in sets)
    db = await get_connection()
    try:
        cur = await db.execute(
            f"UPDATE project_agents SET {cols} WHERE id=? AND project_id=?",
            (*sets.values(), agent_id, pid),
        )
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "该项目下不存在此 Agent")
        row = await (await db.execute(
            "SELECT * FROM project_agents WHERE id=?", (agent_id,))).fetchone()
        return dict(row)
    finally:
        await db.close()


@router.delete("/{pid}/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def remove_agent(pid: int, agent_id: int):
    await _ensure_project(pid)
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT slug FROM project_agents WHERE id=? AND project_id=?", (agent_id, pid))).fetchone()
        cur = await db.execute(
            "DELETE FROM project_agents WHERE id=? AND project_id=?", (agent_id, pid))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "该项目下不存在此 Agent")
        slug = row["slug"]
    finally:
        await db.close()
    await sync_agent_memory(slug)   # 从剩余项目重建工作区段落
    return {"ok": True}


@router.put("/{pid}/agents/{agent_id}/leader", dependencies=[Depends(require_admin)])
async def set_leader(pid: int, agent_id: int):
    """把某成员设为团队总负责人（Team Leader）；自动取消原负责人。每项目至多一个。"""
    await _ensure_project(pid)
    db = await get_connection()
    try:
        exists = await (await db.execute(
            "SELECT id FROM project_agents WHERE id=? AND project_id=?", (agent_id, pid))).fetchone()
        if not exists:
            raise HTTPException(404, "该项目下不存在此 Agent")
        await db.execute("UPDATE project_agents SET is_leader=0 WHERE project_id=?", (pid,))
        await db.execute("UPDATE project_agents SET is_leader=1 WHERE id=?", (agent_id,))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()
