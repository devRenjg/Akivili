"""项目数据访问：CRUD + 关联 Agent 计数。"""
from pathlib import Path

from database import get_connection


async def create_project(title: str, local_path: str, description: str = "", git_url: str = "") -> dict:
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description, git_url) VALUES (?,?,?,?)",
            (title, local_path, description, git_url),
        )
        await db.commit()
        pid = cur.lastrowid
        row = await (await db.execute("SELECT * FROM projects WHERE id=?", (pid,))).fetchone()
        return dict(row)
    finally:
        await db.close()


async def list_projects() -> list[dict]:
    db = await get_connection()
    try:
        cur = await db.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM project_agents a WHERE a.project_id = p.id) AS agent_count
               FROM projects p ORDER BY p.updated_at DESC, p.id DESC""")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def get_project(pid: int) -> dict | None:
    db = await get_connection()
    try:
        cur = await db.execute("SELECT * FROM projects WHERE id=?", (pid,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def update_project(pid: int, fields: dict) -> dict | None:
    allowed = {"title", "local_path", "description", "status", "git_url"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return await get_project(pid)
    cols = ", ".join(f"{k}=?" for k in sets)
    db = await get_connection()
    try:
        await db.execute(
            f"UPDATE projects SET {cols}, updated_at=datetime('now') WHERE id=?",
            (*sets.values(), pid),
        )
        await db.commit()
    finally:
        await db.close()
    return await get_project(pid)


async def delete_project(pid: int) -> None:
    db = await get_connection()
    try:
        await db.execute("DELETE FROM projects WHERE id=?", (pid,))
        await db.commit()
    finally:
        await db.close()


def path_exists_dir(local_path: str) -> bool:
    try:
        return bool(local_path) and Path(local_path).is_dir()
    except OSError:
        return False
