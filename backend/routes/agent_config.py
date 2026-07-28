"""Agent 档案：按 slug 的接入模型 + 启用的 Skills（跨项目共享）。

模型/记忆/Skills 都绑在 Agent 身份（slug）上：同一 Agent 无论在哪个项目，
读写的都是这一份，从而天然跨项目互通。persona 仍按项目实例独立（见 project_agents）。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sqlalchemy import select

from agent_memory_sync import sync_agent_memory
from auth import require_admin
from models import AgentProfile, AgentSkill, get_session_factory, now_expr, upsert

router = APIRouter(prefix="/api/agent-config", tags=["agent-config"])


@router.get("/{slug}")
async def get_config(slug: str):
    """返回某 Agent 的接入模型、启用 skills、昵称、头像。"""
    async with get_session_factory()() as session:
        prof = (await session.execute(
            select(AgentProfile.provider_id, AgentProfile.nickname, AgentProfile.avatar)
            .where(AgentProfile.slug == slug))).first()
        rows = (await session.execute(
            select(AgentSkill.skill_slug).where(AgentSkill.agent_slug == slug))).all()
        return {
            "slug": slug,
            "provider_id": prof.provider_id if prof else "",
            "nickname": prof.nickname if prof else "",
            "avatar": prof.avatar if prof else "",
            "skill_slugs": [r.skill_slug for r in rows],
        }


@router.get("/taken/list")
async def taken(exclude: str = ""):
    """已被占用的头像与昵称（供编辑资料时过滤/查重）。exclude=当前 slug（排除自己）。"""
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(AgentProfile.slug, AgentProfile.nickname, AgentProfile.avatar)
            .where(AgentProfile.slug != exclude))).all()
        avatars = sorted({r.avatar for r in rows if r.avatar})
        nicknames = sorted({r.nickname for r in rows if r.nickname})
        return {"avatars": avatars, "nicknames": nicknames}


class SetProfileRequest(BaseModel):
    nickname: str = ""
    avatar: str = ""


@router.put("/{slug}/profile", dependencies=[Depends(require_admin)])
async def set_profile(slug: str, req: SetProfileRequest):
    """设置昵称 + 头像（按身份 slug 跨项目共享）。仅管理员。昵称不可重复。"""
    from fastapi import HTTPException
    nickname = req.nickname.strip()[:40]
    avatar = req.avatar.strip()
    async with get_session_factory()() as session:
        if nickname:
            dup = (await session.execute(
                select(AgentProfile.slug)
                .where(AgentProfile.nickname == nickname, AgentProfile.slug != slug))).first()
            if dup:
                raise HTTPException(409, f"昵称「{nickname}」已被占用，请换一个")
        # upsert：冲突(slug)则更新 nickname/avatar/updated_at（对齐旧 ON CONFLICT DO UPDATE）
        await session.execute(upsert(
            AgentProfile, ["slug"],
            insert_values={"slug": slug, "nickname": nickname, "avatar": avatar,
                           "updated_at": now_expr()},
            update_values={"nickname": nickname, "avatar": avatar, "updated_at": now_expr()}))
        await session.commit()
        return {"ok": True}


class SetModelRequest(BaseModel):
    provider_id: str = ""


@router.put("/{slug}/model", dependencies=[Depends(require_admin)])
async def set_model(slug: str, req: SetModelRequest):
    async with get_session_factory()() as session:
        # upsert：冲突(slug)则更新 provider_id/updated_at
        await session.execute(upsert(
            AgentProfile, ["slug"],
            insert_values={"slug": slug, "provider_id": req.provider_id,
                           "updated_at": now_expr()},
            update_values={"provider_id": req.provider_id, "updated_at": now_expr()}))
        await session.commit()
        return {"ok": True}


class SetSkillsRequest(BaseModel):
    skill_slugs: list[str] = []


@router.put("/{slug}/skills", dependencies=[Depends(require_admin)])
async def set_skills(slug: str, req: SetSkillsRequest):
    """重写该 Agent 启用的 Skill 集合。"""
    from sqlalchemy import delete as sa_delete
    async with get_session_factory()() as session:
        await session.execute(sa_delete(AgentSkill).where(AgentSkill.agent_slug == slug))
        for ss in dict.fromkeys(req.skill_slugs):   # 去重保序
            session.add(AgentSkill(agent_slug=slug, skill_slug=ss))
        await session.commit()
    await sync_agent_memory(slug)   # 把 Skills 使用说明写进该 Agent 记忆
    return {"ok": True}
