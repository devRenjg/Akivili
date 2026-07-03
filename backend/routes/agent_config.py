"""Agent 档案：按 slug 的接入模型 + 启用的 Skills（跨项目共享）。

模型/记忆/Skills 都绑在 Agent 身份（slug）上：同一 Agent 无论在哪个项目，
读写的都是这一份，从而天然跨项目互通。persona 仍按项目实例独立（见 project_agents）。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from database import get_connection
from agent_memory_sync import sync_agent_memory
from auth import require_admin

router = APIRouter(prefix="/api/agent-config", tags=["agent-config"])


@router.get("/{slug}")
async def get_config(slug: str):
    """返回某 Agent 的接入模型、启用 skills、昵称、头像。"""
    db = await get_connection()
    try:
        prof = await (await db.execute(
            "SELECT provider_id, nickname, avatar FROM agent_profiles WHERE slug=?", (slug,))).fetchone()
        rows = await (await db.execute(
            "SELECT skill_slug FROM agent_skills WHERE agent_slug=?", (slug,))).fetchall()
        return {
            "slug": slug,
            "provider_id": prof["provider_id"] if prof else "",
            "nickname": prof["nickname"] if prof else "",
            "avatar": prof["avatar"] if prof else "",
            "skill_slugs": [r["skill_slug"] for r in rows],
        }
    finally:
        await db.close()


@router.get("/taken/list")
async def taken(exclude: str = ""):
    """已被占用的头像与昵称（供编辑资料时过滤/查重）。exclude=当前 slug（排除自己）。"""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT slug, nickname, avatar FROM agent_profiles WHERE slug != ?", (exclude,))).fetchall()
        avatars = sorted({r["avatar"] for r in rows if r["avatar"]})
        nicknames = sorted({r["nickname"] for r in rows if r["nickname"]})
        return {"avatars": avatars, "nicknames": nicknames}
    finally:
        await db.close()


class SetProfileRequest(BaseModel):
    nickname: str = ""
    avatar: str = ""


@router.put("/{slug}/profile", dependencies=[Depends(require_admin)])
async def set_profile(slug: str, req: SetProfileRequest):
    """设置昵称 + 头像（按身份 slug 跨项目共享）。仅管理员。昵称不可重复。"""
    from fastapi import HTTPException
    nickname = req.nickname.strip()[:40]
    avatar = req.avatar.strip()
    db = await get_connection()
    try:
        if nickname:
            dup = await (await db.execute(
                "SELECT slug FROM agent_profiles WHERE nickname=? AND slug!=?", (nickname, slug))).fetchone()
            if dup:
                raise HTTPException(409, f"昵称「{nickname}」已被占用，请换一个")
        await db.execute(
            """INSERT INTO agent_profiles (slug, nickname, avatar, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(slug) DO UPDATE SET nickname=excluded.nickname,
                                               avatar=excluded.avatar,
                                               updated_at=datetime('now')""",
            (slug, nickname, avatar))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


class SetModelRequest(BaseModel):
    provider_id: str = ""


@router.put("/{slug}/model", dependencies=[Depends(require_admin)])
async def set_model(slug: str, req: SetModelRequest):
    db = await get_connection()
    try:
        await db.execute(
            """INSERT INTO agent_profiles (slug, provider_id, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(slug) DO UPDATE SET provider_id=excluded.provider_id,
                                               updated_at=datetime('now')""",
            (slug, req.provider_id))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


class SetSkillsRequest(BaseModel):
    skill_slugs: list[str] = []


@router.put("/{slug}/skills", dependencies=[Depends(require_admin)])
async def set_skills(slug: str, req: SetSkillsRequest):
    """重写该 Agent 启用的 Skill 集合。"""
    db = await get_connection()
    try:
        await db.execute("DELETE FROM agent_skills WHERE agent_slug=?", (slug,))
        for ss in dict.fromkeys(req.skill_slugs):   # 去重保序
            await db.execute(
                "INSERT INTO agent_skills (agent_slug, skill_slug) VALUES (?,?)", (slug, ss))
        await db.commit()
    finally:
        await db.close()
    await sync_agent_memory(slug)   # 把 Skills 使用说明写进该 Agent 记忆
    return {"ok": True}
