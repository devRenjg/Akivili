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
from database import get_connection

router = APIRouter(prefix="/api/skills", tags=["skills"])

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@router.get("")
async def list_skills(q: str = ""):
    sql = ("SELECT s.id, s.slug, s.name, s.description, s.is_dir, s.downloadable, "
           "(SELECT COUNT(*) FROM skill_downloads d WHERE d.skill_id=s.id) AS download_count "
           "FROM skills s WHERE 1=1")
    params: list = []
    if q:
        sql += " AND (s.name LIKE ? OR s.description LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY s.name"
    db = await get_connection()
    try:
        rows = await (await db.execute(sql, params)).fetchall()
        return {"skills": [dict(r) for r in rows], "count": len(rows)}
    finally:
        await db.close()


@router.get("/{skill_id}")
async def get_skill(skill_id: int):
    db = await get_connection()
    try:
        row = await (await db.execute("SELECT * FROM skills WHERE id=?", (skill_id,))).fetchone()
        if not row:
            raise HTTPException(404, "Skill 不存在")
        return dict(row)
    finally:
        await db.close()


@router.get("/{skill_id}/download")
async def download_skill(skill_id: int, request: Request):
    """下载 Skill：目录型打包成 zip（含 SKILL.md + scripts + references）；单文件型下 .md。
    记录下载日志（IP + 时间）。"""
    db = await get_connection()
    try:
        row = await (await db.execute("SELECT * FROM skills WHERE id=?", (skill_id,))).fetchone()
        if not row:
            raise HTTPException(404, "Skill 不存在")
        row = dict(row)
        # 禁止下载的 Skill（downloadable=0）：仅展示、供 Agent 集成，服务端硬拦截（防绕过前端直接打接口）
        if not row.get("downloadable", 1):
            raise HTTPException(403, "该 Skill 不提供下载（仅供 Agent 集成使用）")
        # 记录下载：客户端 IP（优先 X-Forwarded-For，兜底直连 IP）
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else ""))
        await db.execute("INSERT INTO skill_downloads (skill_id, ip) VALUES (?,?)", (skill_id, ip))
        await db.commit()
    finally:
        await db.close()

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
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT ip, ts FROM skill_downloads WHERE skill_id=? ORDER BY id DESC LIMIT 200", (skill_id,))).fetchall()
        total = await (await db.execute(
            "SELECT COUNT(*) c FROM skill_downloads WHERE skill_id=?", (skill_id,))).fetchone()
        return {"total": total["c"] if total else 0, "logs": [dict(r) for r in rows]}
    finally:
        await db.close()


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
