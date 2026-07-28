"""Agent 模版库接口：列表 / 详情 / 分类 / 标签 / 手动新增 / 重新扫描。"""
import re
import uuid

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from sqlalchemy import distinct, func, literal_column, or_, select, update as sa_update

from auth import require_admin

import agents as agents_mod
from models import (
    AgentProfile,
    AgentSkill,
    AgentTemplate,
    Project,
    ProjectAgent,
    Skill,
    get_session_factory,
    insert_or_ignore,
    now_expr,
    upsert,
)

router = APIRouter(prefix="/api/agents", tags=["agents"])

# agent_templates 物理列序（对齐 001 基线），SELECT t.* → dict 保持键集合/顺序
_TEMPLATE_COLS = (
    "id", "slug", "name", "division", "description", "emoji", "color",
    "source_path", "body", "imported_at", "tags", "origin",
)


class CreateTalentRequest(BaseModel):
    name: str
    description: str = ""
    division: str = ""            # 分类（复用现有 division 字段；输入新名即新增分类）
    body: str = ""                # 人格定义正文
    nickname: str = ""            # 昵称（写 agent_profiles）
    avatar: str = ""              # 头像文件名（写 agent_profiles）
    provider_id: str = ""         # 接入模型（写 agent_profiles）
    skill_slugs: list[str] = []   # 绑定的 Skills（写 agent_skills，按 slug 跨项目共享）
    emoji: str = ""
    color: str = ""


class SetDivisionRequest(BaseModel):
    division: str = ""            # 目标分类（空=归入「其他」）


class RenameDivisionRequest(BaseModel):
    old_name: str
    new_name: str


@router.get("/templates")
async def list_templates(division: str = "", q: str = ""):
    """模版列表。支持按 division 与关键词 q（匹配 name/description）过滤。不含 body。
    默认按「已加入的项目数」降序排（热门人才在前），其次分类、名字。"""
    # 两个相关子查询逐字保留原 SQL（绑定当前行 agent_templates.slug），
    # 用 literal_column 承载，避免翻译嵌套逻辑出偏差（与 project_agents 同款）。
    project_count = literal_column(
        "(SELECT COUNT(DISTINCT pa.project_id) FROM project_agents pa "
        "WHERE pa.slug = agent_templates.slug)")
    # 已解决任务数：该身份(slug)在「已完成(done)」任务里有过成功执行(succeeded run)，按任务去重。
    # 排除：① 已删除的任务卡片（JOIN tasks 天然排除）；② 孤儿子任务——父任务已被删、
    # 子任务残留的不算（要求顶层任务，或其父任务仍存在）。
    solved_tasks = literal_column(
        "(SELECT COUNT(DISTINCT tr.task_id) FROM task_runs tr JOIN tasks tk ON tk.id = tr.task_id "
        " WHERE tr.agent_slug = agent_templates.slug AND tr.status = 'succeeded' AND tk.status = 'done' "
        " AND (tk.parent_task_id IS NULL OR EXISTS "
        "      (SELECT 1 FROM tasks pt WHERE pt.id = tk.parent_task_id)))")
    stmt = select(
        AgentTemplate.id, AgentTemplate.slug, AgentTemplate.name, AgentTemplate.division,
        AgentTemplate.description, AgentTemplate.emoji, AgentTemplate.color, AgentTemplate.origin,
        AgentProfile.nickname.label("nickname"), AgentProfile.avatar.label("avatar"),
        project_count.label("project_count"), solved_tasks.label("solved_tasks"),
    ).select_from(AgentTemplate).outerjoin(
        AgentProfile, AgentProfile.slug == AgentTemplate.slug)
    if division:
        stmt = stmt.where(AgentTemplate.division == division)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(AgentTemplate.name.like(like), AgentTemplate.description.like(like)))
    # 排序：项目数优先；项目数相同看已完成任务数（越多越靠前）；再按分类、名字兜底。
    stmt = stmt.order_by(
        literal_column("project_count").desc(), literal_column("solved_tasks").desc(),
        AgentTemplate.division, AgentTemplate.name)
    _cols = ("id", "slug", "name", "division", "description", "emoji", "color", "origin",
             "nickname", "avatar", "project_count", "solved_tasks")
    async with get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
        templates = [{c: getattr(r, c) for c in _cols} for r in rows]
        return {"templates": templates, "count": len(templates)}


@router.get("/divisions")
async def list_divisions():
    """分类列表 + 各自数量。"""
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(AgentTemplate.division, func.count().label("n"))
            .group_by(AgentTemplate.division)
            .order_by(AgentTemplate.division))).all()
        return {"divisions": [{"division": r.division, "n": r.n} for r in rows]}


@router.get("/templates/{template_id}")
async def get_template(template_id: int):
    """模版详情，含人格正文 body。"""
    async with get_session_factory()() as session:
        row = (await session.execute(
            select(AgentTemplate,
                   AgentProfile.nickname.label("nickname"),
                   AgentProfile.avatar.label("avatar"))
            .select_from(AgentTemplate)
            .outerjoin(AgentProfile, AgentProfile.slug == AgentTemplate.slug)
            .where(AgentTemplate.id == template_id))).first()
        if not row:
            raise HTTPException(404, "模版不存在")
        tpl, nickname, avatar = row
        data = {c: getattr(tpl, c) for c in _TEMPLATE_COLS}
        data["nickname"] = nickname
        data["avatar"] = avatar
        # 该人才（按 slug）已集成的 Skills，带上 Skill 名称/描述用于界面展示。
        skill_rows = (await session.execute(
            select(Skill.slug, Skill.name, Skill.description)
            .join(AgentSkill, AgentSkill.skill_slug == Skill.slug)
            .where(AgentSkill.agent_slug == data["slug"])
            .order_by(Skill.name))).all()
        data["skills"] = [{"slug": r.slug, "name": r.name, "description": r.description}
                          for r in skill_rows]
        return data


@router.get("/templates/{template_id}/projects")
async def template_projects(template_id: int):
    """该人才已加入的项目 + 仍可邀请加入的项目（按 slug）。"""
    async with get_session_factory()() as session:
        t = (await session.execute(
            select(AgentTemplate.slug).where(AgentTemplate.id == template_id))).first()
        if not t:
            raise HTTPException(404, "模版不存在")
        slug = t.slug
        joined = (await session.execute(
            select(distinct(Project.id).label("id"), Project.title)
            .select_from(ProjectAgent)
            .join(Project, Project.id == ProjectAgent.project_id)
            .where(ProjectAgent.slug == slug)
            .order_by(Project.id))).all()
        joined_ids = {r.id for r in joined}
        all_p = (await session.execute(
            select(Project.id, Project.title).order_by(Project.id))).all()
        joinable = [{"id": r.id, "title": r.title} for r in all_p if r.id not in joined_ids]
        return {"joined": [{"id": r.id, "title": r.title} for r in joined], "joinable": joinable}


@router.put("/templates/{template_id}/division", dependencies=[Depends(require_admin)])
async def set_talent_division(template_id: int, req: SetDivisionRequest):
    """改变某个人才的分类（空=归入「其他」）。"""
    div = req.division.strip()
    async with get_session_factory()() as session:
        row = (await session.execute(
            select(AgentTemplate.id).where(AgentTemplate.id == template_id))).first()
        if not row:
            raise HTTPException(404, "人才不存在")
        await session.execute(
            sa_update(AgentTemplate).where(AgentTemplate.id == template_id).values(division=div))
        await session.commit()
    return {"ok": True, "division": div}


@router.put("/divisions/rename", dependencies=[Depends(require_admin)])
async def rename_division(req: RenameDivisionRequest):
    """改写分类名：把该分类下所有人才的 division 批量改成新名。"""
    old = req.old_name.strip()
    new = req.new_name.strip()
    if not old:
        raise HTTPException(400, "原分类名不能为空")
    if not new:
        raise HTTPException(400, "新分类名不能为空")
    async with get_session_factory()() as session:
        result = await session.execute(
            sa_update(AgentTemplate).where(AgentTemplate.division == old).values(division=new))
        await session.commit()
        affected = result.rowcount
    return {"ok": True, "affected": affected}


@router.delete("/divisions/{name}", dependencies=[Depends(require_admin)])
async def delete_division(name: str):
    """删除一个分类：把该分类下人才的 division 清空（归入「其他」），人才本身不删。"""
    old = (name or "").strip()
    if not old:
        raise HTTPException(400, "分类名不能为空")
    async with get_session_factory()() as session:
        result = await session.execute(
            sa_update(AgentTemplate).where(AgentTemplate.division == old).values(division=""))
        await session.commit()
        affected = result.rowcount
    return {"ok": True, "affected": affected}


@router.post("/rescan", dependencies=[Depends(require_admin)])
async def rescan():
    """重新扫描库目录，幂等同步到 agent_templates。"""
    return await agents_mod.rescan()


def _slugify(name: str) -> str:
    """从名字生成 slug 主体：ASCII 保留、其余转连字符；空则用 talent。"""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "talent"


@router.post("/templates", dependencies=[Depends(require_admin)])
async def create_talent(req: CreateTalentRequest):
    """管理员手动新增一个数字人才（origin=manual，rescan 不会覆盖它）。
    同时按 slug 写 agent_profiles（昵称/头像/模型）与 agent_skills（绑定的 Skills，跨项目共享）。"""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    # manual- 前缀 + 随机后缀，保证与扫描模版 slug（取自文件名）不撞、rescan 不会误更新
    slug = f"manual-{_slugify(name)}-{uuid.uuid4().hex[:8]}"
    nickname = req.nickname.strip()[:40]

    async with get_session_factory()() as session:
        # 昵称唯一（与 agent_config.set_profile 口径一致）
        if nickname:
            dup = (await session.execute(
                select(AgentProfile.slug).where(AgentProfile.nickname == nickname))).first()
            if dup:
                raise HTTPException(409, f"昵称「{nickname}」已被占用，请换一个")
        tpl = AgentTemplate(
            slug=slug, name=name, division=req.division.strip(),
            description=req.description.strip(), emoji=req.emoji.strip(),
            color=req.color.strip(), source_path="", body=req.body, origin="manual")
        session.add(tpl)
        # agent_profiles：昵称/头像/模型（有任一非空才写）
        if nickname or req.avatar.strip() or req.provider_id.strip():
            await session.execute(upsert(
                AgentProfile, ["slug"],
                insert_values={"slug": slug, "provider_id": req.provider_id.strip(),
                               "nickname": nickname, "avatar": req.avatar.strip(),
                               "updated_at": now_expr()},
                update_values={"provider_id": req.provider_id.strip(), "nickname": nickname,
                               "avatar": req.avatar.strip(), "updated_at": now_expr()}))
        # agent_skills：绑定 Skills（按 slug 跨项目共享）
        for ss in dict.fromkeys(s for s in req.skill_slugs if s.strip()):
            await session.execute(insert_or_ignore(AgentSkill).values(
                agent_slug=slug, skill_slug=ss))
        await session.flush()   # 取 tpl.id（对齐旧 SELECT id WHERE slug）
        new_id = tpl.id
        await session.commit()
    # 把绑定的 Skills 使用说明写进该 Agent 记忆（与项目内配 Skills 行为一致）
    if req.skill_slugs:
        try:
            from agent_memory_sync import sync_agent_memory
            await sync_agent_memory(slug)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "id": new_id, "slug": slug}
