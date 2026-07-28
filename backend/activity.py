"""任务活动时间线：记录与查询。activity + 对话消息按时序合并展示。"""
import json

from sqlalchemy import select

from models import (
    Activity,
    AgentProfile,
    Message,
    ProjectAgent,
    Task,
    get_session_factory,
)
from timeutil import to_beijing


async def log_activity(task_id: int, action: str, actor_type: str = "system",
                       actor_name: str = "", detail: dict | None = None) -> None:
    async with get_session_factory()() as session:
        session.add(Activity(
            task_id=task_id, actor_type=actor_type, actor_name=actor_name,
            action=action, detail=json.dumps(detail or {}, ensure_ascii=False)))
        await session.commit()


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
    async with get_session_factory()() as session:
        # 任务所属项目的成员表：slug/name → 展示信息（昵称优先）
        prow = (await session.execute(
            select(Task.project_id).where(Task.id == task_id))).first()
        project_id = prow.project_id if prow else 0
        members = (await session.execute(
            select(ProjectAgent.slug, ProjectAgent.name, ProjectAgent.emoji,
                   ProjectAgent.is_leader,
                   AgentProfile.nickname.label("nickname"),
                   AgentProfile.avatar.label("avatar"))
            .select_from(ProjectAgent)
            .outerjoin(AgentProfile, AgentProfile.slug == ProjectAgent.slug)
            .where(ProjectAgent.project_id == project_id))).all()
        by_slug, by_name = {}, {}
        for m in members:
            info = {"slug": m.slug, "name": m.name, "emoji": m.emoji or "",
                    "nickname": (m.nickname or "").strip(), "avatar": m.avatar or "",
                    "is_leader": bool(m.is_leader)}
            by_slug[m.slug] = info
            by_name[m.name] = info

        acts = (await session.execute(
            select(Activity.actor_type, Activity.actor_name, Activity.action,
                   Activity.detail, Activity.created_at)
            .where(Activity.task_id == task_id).order_by(Activity.id))).all()
        conv = (await session.execute(
            select(Task.conversation_id).where(Task.id == task_id))).first()
        msgs = []
        if conv and conv.conversation_id:
            msgs = (await session.execute(
                select(Message.role, Message.content, Message.author_slug,
                       Message.author_name, Message.created_at)
                .where(Message.conversation_id == conv.conversation_id)
                .order_by(Message.id))).all()
        # 任务创建者名（供无 author_name 的历史 user 消息回退显示）
        crow = (await session.execute(
            select(Activity.actor_name)
            .where(Activity.task_id == task_id, Activity.action == "created",
                   Activity.actor_type == "user")
            .order_by(Activity.id).limit(1))).first()
        creator_name = (crow.actor_name if crow else "") or ""

    def member_author(slug: str = "", name: str = "") -> dict | None:
        return by_slug.get(slug) or by_name.get(name)

    items = []
    for a in acts:
        # 活动作者：agent 的 actor_name 可能是角色名，也可能是 slug（历史/部分埋点）→ 两种都试着匹配成员，拿昵称/头像
        author = member_author(slug=a.actor_name, name=a.actor_name) if a.actor_type == "agent" else None
        items.append({
            "kind": "activity",
            "actor_type": a.actor_type, "actor_name": a.actor_name,
            "actor_display": _actor_display(a.actor_type, a.actor_name),
            "author": author,
            "action": a.action, "detail": json.loads(a.detail or "{}"),
            "created_at": to_beijing(a.created_at),
        })
    for m in msgs:
        author = member_author(slug=m.author_slug or "") if m.role != "user" else None
        # user 消息的发送者名：优先存的 author_name，其次回退任务创建者
        user_name = (m.author_name or "").strip() or creator_name if m.role == "user" else ""
        items.append({
            "kind": "message",
            "role": m.role, "content": m.content,
            "author_slug": m.author_slug or "", "author": author,
            "user_name": user_name,
            "created_at": to_beijing(m.created_at),
        })
    # 按时间排序（created_at 已转北京时间，同格式字符串，可直接比较）
    items.sort(key=lambda x: x["created_at"] or "")
    return items
