"""Agent 模版库接口：列表 / 详情 / 分类 / 重新扫描。"""
from fastapi import APIRouter, HTTPException, Depends

from auth import require_admin

import agents as agents_mod
from database import get_connection

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("/templates")
async def list_templates(division: str = "", q: str = ""):
    """模版列表。支持按 division 与关键词 q（匹配 name/description）过滤。不含 body。
    默认按「已加入的项目数」降序排（热门人才在前），其次分类、名字。"""
    sql = ("SELECT t.id, t.slug, t.name, t.division, t.description, t.emoji, t.color, "
           "p.nickname AS nickname, p.avatar AS avatar, "
           "(SELECT COUNT(DISTINCT pa.project_id) FROM project_agents pa WHERE pa.slug = t.slug) AS project_count "
           "FROM agent_templates t LEFT JOIN agent_profiles p ON p.slug = t.slug WHERE 1=1")
    params: list = []
    if division:
        sql += " AND t.division = ?"
        params.append(division)
    if q:
        sql += " AND (t.name LIKE ? OR t.description LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY project_count DESC, t.division, t.name"
    db = await get_connection()
    try:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        return {"templates": [dict(r) for r in rows], "count": len(rows)}
    finally:
        await db.close()


@router.get("/divisions")
async def list_divisions():
    """分类列表 + 各自数量。"""
    db = await get_connection()
    try:
        cur = await db.execute(
            "SELECT division, COUNT(*) AS n FROM agent_templates "
            "GROUP BY division ORDER BY division")
        rows = await cur.fetchall()
        return {"divisions": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.get("/templates/{template_id}")
async def get_template(template_id: int):
    """模版详情，含人格正文 body。"""
    db = await get_connection()
    try:
        cur = await db.execute(
            """SELECT t.*, p.nickname AS nickname, p.avatar AS avatar
               FROM agent_templates t LEFT JOIN agent_profiles p ON p.slug = t.slug
               WHERE t.id = ?""", (template_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "模版不存在")
        return dict(row)
    finally:
        await db.close()


@router.get("/templates/{template_id}/projects")
async def template_projects(template_id: int):
    """该人才已加入的项目 + 仍可邀请加入的项目（按 slug）。"""
    db = await get_connection()
    try:
        t = await (await db.execute("SELECT slug FROM agent_templates WHERE id=?", (template_id,))).fetchone()
        if not t:
            raise HTTPException(404, "模版不存在")
        slug = t["slug"]
        joined = await (await db.execute(
            """SELECT DISTINCT p.id, p.title FROM project_agents pa
               JOIN projects p ON p.id = pa.project_id
               WHERE pa.slug=? ORDER BY p.id""", (slug,))).fetchall()
        joined_ids = {r["id"] for r in joined}
        all_p = await (await db.execute("SELECT id, title FROM projects ORDER BY id")).fetchall()
        joinable = [dict(r) for r in all_p if r["id"] not in joined_ids]
        return {"joined": [dict(r) for r in joined], "joinable": joinable}
    finally:
        await db.close()


@router.post("/rescan", dependencies=[Depends(require_admin)])
async def rescan():
    """重新扫描库目录，幂等同步到 agent_templates。"""
    return await agents_mod.rescan()
