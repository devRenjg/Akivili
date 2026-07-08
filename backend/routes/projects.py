"""项目接口：CRUD，创建/更新时校验 local_path 为已存在目录。"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

import projects as projects_mod
from auth import require_admin

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    title: str
    local_path: str
    description: str = ""
    git_url: str = ""


class UpdateProjectRequest(BaseModel):
    title: str | None = None
    local_path: str | None = None
    description: str | None = None
    status: str | None = None
    git_url: str | None = None


@router.post("", dependencies=[Depends(require_admin)])
async def create_project(req: CreateProjectRequest):
    if not req.title.strip():
        raise HTTPException(400, "项目标题不能为空")
    if not projects_mod.path_exists_dir(req.local_path):
        raise HTTPException(400, f"本地文件夹不存在：{req.local_path}")
    proj = await projects_mod.create_project(
        req.title.strip(), req.local_path, req.description, req.git_url.strip())
    # 不自动拉入默认负责人：新项目从空团队开始，由用户自己挑选/导入负责人并设为 Leader，
    # 避免所有项目都默认塞同一个「项目负责人（星）」。
    return proj


@router.get("")
async def list_projects():
    return {"projects": await projects_mod.list_projects()}


@router.get("/{pid}")
async def get_project(pid: int):
    proj = await projects_mod.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    return proj


@router.put("/{pid}", dependencies=[Depends(require_admin)])
async def update_project(pid: int, req: UpdateProjectRequest):
    if not await projects_mod.get_project(pid):
        raise HTTPException(404, "项目不存在")
    if req.local_path is not None and not projects_mod.path_exists_dir(req.local_path):
        raise HTTPException(400, f"本地文件夹不存在：{req.local_path}")
    return await projects_mod.update_project(pid, req.model_dump())


@router.delete("/{pid}", dependencies=[Depends(require_admin)])
async def delete_project(pid: int):
    if not await projects_mod.get_project(pid):
        raise HTTPException(404, "项目不存在")
    await projects_mod.delete_project(pid)
    return {"ok": True}
