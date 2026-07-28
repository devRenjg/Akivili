"""Skill 库接口：列表 / 详情 / 下载 / 重扫 / 新建·编辑。"""
import io
import re
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import Response, FileResponse

from auth import require_admin
from pydantic import BaseModel

import skills as skills_mod
from models import Skill, SkillDownload, get_session_factory
from sqlalchemy import func, or_, select

router = APIRouter(prefix="/api/skills", tags=["skills"])

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# skills 表物理列序（对齐 001 基线），供 SELECT * → dict 保持键集合/顺序
_SKILL_COLS = (
    "id", "slug", "name", "description", "source_path",
    "body", "imported_at", "is_dir", "downloadable",
)


def _skill_dict(s: Skill) -> dict:
    return {c: getattr(s, c) for c in _SKILL_COLS}


@router.get("")
async def list_skills(q: str = ""):
    # 相关子查询：每个 skill 的下载数（对齐旧 (SELECT COUNT(*) ...) AS download_count）
    download_count = (
        select(func.count())
        .select_from(SkillDownload)
        .where(SkillDownload.skill_id == Skill.id)
        .scalar_subquery()
    )
    stmt = select(
        Skill.id, Skill.slug, Skill.name, Skill.description,
        Skill.is_dir, Skill.downloadable,
        download_count.label("download_count"),
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Skill.name.like(like), Skill.description.like(like)))
    # 按下载量降序（热门在前）；仅集成/无下载的默认 0，自然沉底；同下载量按名字稳定兜底。
    stmt = stmt.order_by(download_count.desc(), Skill.name)
    async with get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
        skills = [{"id": r.id, "slug": r.slug, "name": r.name, "description": r.description,
                   "is_dir": r.is_dir, "downloadable": r.downloadable,
                   "download_count": r.download_count} for r in rows]
        return {"skills": skills, "count": len(skills)}


@router.get("/{skill_id}")
async def get_skill(skill_id: int):
    async with get_session_factory()() as session:
        s = (await session.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
        if not s:
            raise HTTPException(404, "Skill 不存在")
        return _skill_dict(s)


@router.get("/{skill_id}/download")
async def download_skill(skill_id: int, request: Request):
    """下载 Skill：目录型打包成 zip（含 SKILL.md + scripts + references）；单文件型下 .md。
    记录下载日志（IP + 时间）。"""
    async with get_session_factory()() as session:
        s = (await session.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
        if not s:
            raise HTTPException(404, "Skill 不存在")
        row = _skill_dict(s)
        # 禁止下载的 Skill（downloadable=0）：仅展示、供 Agent 集成，服务端硬拦截（防绕过前端直接打接口）
        if not row.get("downloadable", 1):
            raise HTTPException(403, "该 Skill 不提供下载（仅供 Agent 集成使用）")
        # 记录下载：客户端 IP（优先 X-Forwarded-For，兜底直连 IP）
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else ""))
        session.add(SkillDownload(skill_id=skill_id, ip=ip))
        await session.commit()

    src = Path(row["source_path"])
    if row.get("is_dir") and src.is_dir():
        # 打包整个目录为 zip（内存流）
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=f"{row['slug']}/{f.relative_to(src)}")
        buf.seek(0)
        from urllib.parse import quote
        fn = quote(f"{row['slug']}.zip")
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
    # 单文件型
    if src.is_file():
        return FileResponse(str(src), filename=f"{row['slug']}.md")
    raise HTTPException(404, "Skill 源文件不存在")


@router.get("/{skill_id}/downloads", dependencies=[Depends(require_admin)])
async def download_logs(skill_id: int):
    """某 Skill 的下载记录（时间 + IP），仅管理员。"""
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(SkillDownload.ip, SkillDownload.ts)
            .where(SkillDownload.skill_id == skill_id)
            .order_by(SkillDownload.id.desc()).limit(200))).all()
        total = (await session.execute(
            select(func.count()).select_from(SkillDownload)
            .where(SkillDownload.skill_id == skill_id))).scalar_one()
        return {"total": total, "logs": [{"ip": r.ip, "ts": r.ts} for r in rows]}


@router.post("/rescan", dependencies=[Depends(require_admin)])
async def rescan():
    return await skills_mod.rescan()


class SaveSkillRequest(BaseModel):
    slug: str
    name: str
    description: str = ""
    body: str = ""


@router.post("", dependencies=[Depends(require_admin)])
async def create_skill(req: SaveSkillRequest):
    slug = req.slug.strip()
    if ".." in slug or not _SLUG_RE.match(slug):
        raise HTTPException(400, "slug 只能含字母/数字/._-，且不能含 ..")
    if not req.name.strip():
        raise HTTPException(400, "名称不能为空")
    try:
        skills_mod.save_skill_file(slug, req.name.strip(), req.description, req.body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await skills_mod.rescan()
    return {"ok": True, "slug": slug}
