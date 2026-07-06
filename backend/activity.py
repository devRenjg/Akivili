"""任务活动时间线：记录与查询。activity + 对话消息按时序合并展示。"""
import json

from database import get_connection
from timeutil import to_beijing


async def log_activity(task_id: int, action: str, actor_type: str = "system",
                       actor_name: str = "", detail: dict | None = None) -> None:
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO activities (task_id, actor_type, actor_name, action, detail) VALUES (?,?,?,?,?)",
            (task_id, actor_type, actor_name, action, json.dumps(detail or {}, ensure_ascii=False)))
        await db.commit()
    finally:
        await db.close()


def _actor_display(actor_type: str, actor_name: str) -> str:
    """人类可读的操作者名。user 无名回退「管理员」，agent 用其名，system 用「系统」。"""
    name = (actor_name or "").strip()
    if name:
        return name
    if actor_type == "user":
        return "管理员"
    if actor_type == "agent":
        return "Agent"
    return "系统"


async def timeline(task_id: int) -> list[dict]:
    """活动 + 对话消息 合并成一条按时间排序的时间线。
    每条附带 author 信息（slug/昵称/名字/emoji/avatar），供前端按成员显示头像+昵称。"""
    db = await get_connection()
    try:
        # 任务所属项目的成员表：slug/name → 展示信息（昵称优先）
        prow = await (await db.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,))).fetchone()
        project_id = prow["project_id"] if prow else 0
        members = await (await db.execute(
            """SELECT pa.slug, pa.name, pa.emoji, pa.is_leader, p.nickname AS nickname, p.avatar AS avatar
               FROM project_agents pa LEFT JOIN agent_profiles p ON p.slug = pa.slug
               WHERE pa.project_id=?""", (project_id,))).fetchall()
        by_slug, by_name = {}, {}
        for m in members:
            info = {"slug": m["slug"], "name": m["name"], "emoji": m["emoji"] or "",
                    "nickname": (m["nickname"] or "").strip(), "avatar": m["avatar"] or "",
                    "is_leader": bool(m["is_leader"])}
            by_slug[m["slug"]] = info
            by_name[m["name"]] = info

        acts = await (await db.execute(
            "SELECT actor_type, actor_name, action, detail, created_at FROM activities WHERE task_id=? ORDER BY id",
            (task_id,))).fetchall()
        conv = await (await db.execute(
            "SELECT conversation_id FROM tasks WHERE id=?", (task_id,))).fetchone()
        msgs = []
        if conv and conv["conversation_id"]:
            msgs = await (await db.execute(
                "SELECT role, content, author_slug, author_name, created_at FROM messages "
                "WHERE conversation_id=? ORDER BY id",
                (conv["conversation_id"],))).fetchall()
        # 任务创建者名（供无 author_name 的历史 user 消息回退显示）
        crow = await (await db.execute(
            "SELECT actor_name FROM activities WHERE task_id=? AND action='created' AND actor_type='user' "
            "ORDER BY id LIMIT 1", (task_id,))).fetchone()
        creator_name = (crow["actor_name"] if crow else "") or ""
    finally:
        await db.close()

    def member_author(slug: str = "", name: str = "") -> dict | None:
        return by_slug.get(slug) or by_name.get(name)

    items = []
    for a in acts:
        # 活动作者：agent 的 actor_name 可能是角色名，也可能是 slug（历史/部分埋点）→ 两种都试着匹配成员，拿昵称/头像
        author = member_author(slug=a["actor_name"], name=a["actor_name"]) if a["actor_type"] == "agent" else None
        items.append({
            "kind": "activity",
            "actor_type": a["actor_type"], "actor_name": a["actor_name"],
            "actor_display": _actor_display(a["actor_type"], a["actor_name"]),
            "author": author,
            "action": a["action"], "detail": json.loads(a["detail"] or "{}"),
            "created_at": to_beijing(a["created_at"]),
        })
    for m in msgs:
        author = member_author(slug=m["author_slug"] or "") if m["role"] != "user" else None
        # user 消息的发送者名：优先存的 author_name，其次回退任务创建者
        user_name = (m["author_name"] or "").strip() or creator_name if m["role"] == "user" else ""
        items.append({
            "kind": "message",
            "role": m["role"], "content": m["content"],
            "author_slug": m["author_slug"] or "", "author": author,
            "user_name": user_name,
            "created_at": to_beijing(m["created_at"]),
        })
    # 按时间排序（created_at 已转北京时间，同格式字符串，可直接比较）
    items.sort(key=lambda x: x["created_at"] or "")
    return items
