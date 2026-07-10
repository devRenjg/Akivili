"""Agent 模版库接口：列表 / 详情 / 分类 / 标签 / 手动新增 / 重新扫描。"""
import re
import uuid

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from auth import require_admin

import agents as agents_mod
from database import get_connection

router = APIRouter(prefix="/api/agents", tags=["agents"])


class CreateTalentRequest(BaseModel):
    name: str
    description: str = ""
    division: str = ""
    tags: list[str] = []          # 自定义标签
    body: str = ""                # 人格定义正文
    nickname: str = ""            # 昵称（写 agent_profiles）
    avatar: str = ""              # 头像文件名（写 agent_profiles）
    provider_id: str = ""         # 接入模型（写 agent_profiles）
    skill_slugs: list[str] = []   # 绑定的 Skills（写 agent_skills，按 slug 跨项目共享）
    emoji: str = ""
    color: str = ""


@router.get("/templates")
async def list_templates(division: str = "", q: str = "", tag: str = ""):
    """模版列表。支持按 division、关键词 q（匹配 name/description）、tag（标签精确匹配）过滤。不含 body。
    默认按「已加入的项目数」降序排（热门人才在前），其次分类、名字。"""
    sql = ("SELECT t.id, t.slug, t.name, t.division, t.description, t.emoji, t.color, "
           "t.tags, t.origin, "
           "p.nickname AS nickname, p.avatar AS avatar, "
           "(SELECT COUNT(DISTINCT pa.project_id) FROM project_agents pa WHERE pa.slug = t.slug) AS project_count, "
           # 已解决任务数：该身份(slug)在「已完成(done)」任务里有过成功执行(succeeded run)，按任务去重。
           # 排除：① 已删除的任务卡片（JOIN tasks 天然排除）；② 孤儿子任务——父任务已被删、
           # 子任务残留的不算（要求顶层任务，或其父任务仍存在）。
           "(SELECT COUNT(DISTINCT tr.task_id) FROM task_runs tr JOIN tasks tk ON tk.id = tr.task_id "
           " WHERE tr.agent_slug = t.slug AND tr.status = 'succeeded' AND tk.status = 'done' "
           " AND (tk.parent_task_id IS NULL OR EXISTS "
           "      (SELECT 1 FROM tasks pt WHERE pt.id = tk.parent_task_id))) AS solved_tasks "
           "FROM agent_templates t LEFT JOIN agent_profiles p ON p.slug = t.slug WHERE 1=1")
    params: list = []
    if division:
        sql += " AND t.division = ?"
        params.append(division)
    if q:
        sql += " AND (t.name LIKE ? OR t.description LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    if tag:
        # tags 逗号分隔存储，用带分隔符包裹的 LIKE 精确匹配单个标签，避免子串误命中
        sql += " AND (',' || REPLACE(t.tags, ', ', ',') || ',') LIKE ?"
        params.append(f"%,{tag},%")
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


@router.get("/tags")
async def list_tags():
    """所有已用过的自定义标签 + 各自人才数（供筛选下拉与新增时联想）。"""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT tags FROM agent_templates WHERE tags <> ''")).fetchall()
    finally:
        await db.close()
    counter: dict[str, int] = {}
    for r in rows:
        for tg in (r["tags"] or "").split(","):
            tg = tg.strip()
            if tg:
                counter[tg] = counter.get(tg, 0) + 1
    tags = [{"tag": k, "n": v} for k, v in sorted(counter.items(), key=lambda x: (-x[1], x[0]))]
    return {"tags": tags}


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
    tags = ",".join(t.strip() for t in req.tags if t.strip())
    nickname = req.nickname.strip()[:40]

    db = await get_connection()
    try:
        # 昵称唯一（与 agent_config.set_profile 口径一致）
        if nickname:
            dup = await (await db.execute(
                "SELECT slug FROM agent_profiles WHERE nickname=?", (nickname,))).fetchone()
            if dup:
                raise HTTPException(409, f"昵称「{nickname}」已被占用，请换一个")
        await db.execute(
            """INSERT INTO agent_templates
               (slug, name, division, description, emoji, color, source_path, body, tags, origin)
               VALUES (?,?,?,?,?,?,?,?,?, 'manual')""",
            (slug, name, req.division.strip(), req.description.strip(),
             req.emoji.strip(), req.color.strip(), "", req.body, tags))
        # agent_profiles：昵称/头像/模型（有任一非空才写）
        if nickname or req.avatar.strip() or req.provider_id.strip():
            await db.execute(
                """INSERT INTO agent_profiles (slug, provider_id, nickname, avatar, updated_at)
                   VALUES (?,?,?,?, datetime('now'))
                   ON CONFLICT(slug) DO UPDATE SET
                     provider_id=excluded.provider_id, nickname=excluded.nickname,
                     avatar=excluded.avatar, updated_at=datetime('now')""",
                (slug, req.provider_id.strip(), nickname, req.avatar.strip()))
        # agent_skills：绑定 Skills（按 slug 跨项目共享）
        for ss in dict.fromkeys(s for s in req.skill_slugs if s.strip()):
            await db.execute(
                "INSERT OR IGNORE INTO agent_skills (agent_slug, skill_slug) VALUES (?,?)",
                (slug, ss))
        row = await (await db.execute(
            "SELECT id FROM agent_templates WHERE slug=?", (slug,))).fetchone()
        await db.commit()
        new_id = row["id"] if row else None
    finally:
        await db.close()
    # 把绑定的 Skills 使用说明写进该 Agent 记忆（与项目内配 Skills 行为一致）
    if req.skill_slugs:
        try:
            from agent_memory_sync import sync_agent_memory
            await sync_agent_memory(slug)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "id": new_id, "slug": slug}
