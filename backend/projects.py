"""项目数据访问：CRUD + 关联 Agent 计数。"""
from pathlib import Path

from sqlalchemy import delete as sa_delete, func, select, update as sa_update

from models import Project, ProjectAgent, get_session_factory, now_expr

# projects 表列顺序（对齐 001 基线），供 SELECT * → dict 保持原 dict(row) 键序与集合
_PROJECT_COLS = (
    "id", "title", "local_path", "description", "status",
    "created_at", "updated_at", "git_url",
)


def _project_dict(p: Project) -> dict:
    """把 Project ORM 对象转成与旧 dict(row) 等价的 dict（键集合/顺序对齐 SELECT *）。"""
    return {c: getattr(p, c) for c in _PROJECT_COLS}


async def create_project(title: str, local_path: str, description: str = "", git_url: str = "") -> dict:
    async with get_session_factory()() as session:
        p = Project(title=title, local_path=local_path, description=description, git_url=git_url)
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return _project_dict(p)


async def list_projects() -> list[dict]:
    async with get_session_factory()() as session:
        # 相关子查询：每个项目的成员数（对齐旧 SELECT (SELECT COUNT(*) ...) AS agent_count）
        agent_count = (
            select(func.count())
            .select_from(ProjectAgent)
            .where(ProjectAgent.project_id == Project.id)
            .scalar_subquery()
        )
        result = await session.execute(
            select(Project, agent_count.label("agent_count"))
            .order_by(Project.updated_at.desc(), Project.id.desc())
        )
        out = []
        for p, cnt in result.all():
            d = _project_dict(p)
            d["agent_count"] = cnt
            out.append(d)
        return out


async def get_project(pid: int) -> dict | None:
    async with get_session_factory()() as session:
        p = (await session.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
        return _project_dict(p) if p else None


async def update_project(pid: int, fields: dict) -> dict | None:
    allowed = {"title", "local_path", "description", "status", "git_url"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return await get_project(pid)
    # updated_at 用 now_expr()（SQLite→CURRENT_TIMESTAMP，与旧 datetime('now') 同 UTC 同格式）
    sets["updated_at"] = now_expr()
    async with get_session_factory()() as session:
        await session.execute(sa_update(Project).where(Project.id == pid).values(**sets))
        await session.commit()
    return await get_project(pid)


async def delete_project(pid: int) -> None:
    async with get_session_factory()() as session:
        await session.execute(sa_delete(Project).where(Project.id == pid))
        await session.commit()


def path_exists_dir(local_path: str) -> bool:
    try:
        return bool(local_path) and Path(local_path).is_dir()
    except OSError:
        return False
