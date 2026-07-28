"""数据底座 S3.1 · 全表 ORM 模型（对齐 001_baseline）。

**逐字对齐** migrations/versions/001_baseline.py：18 张表、列名/类型/可空/默认值/
主外键一一对应。字段顺序按基线原始 CREATE（含末尾补列写法）保留。

对齐要点（经 run_orm_schema_parity_probe 逐列校验）：
- **可空性**：基线里带 `DEFAULT` 但未写 `NOT NULL` 的列在 SQLite 中是**可空**的，
  故这些列显式 `nullable=True`（Python 类型仍标 `Mapped[str]`——server_default 始终
  填值、读出不会是 None）。仅 `users.role` 是 `NOT NULL DEFAULT`，保持非空。
- **AUTOINCREMENT**：16 张 `INTEGER PRIMARY KEY AUTOINCREMENT` 表加
  `sqlite_autoincrement=True`（禁止 ID 复用，与基线一致）；agent_profiles(文本主键)、
  agent_skills(复合主键) 无自增。
- **方言（S3.1 原样保留，不收敛——收敛是 S3.3）**：`datetime('now')` 默认值 →
  server_default=text("(datetime('now'))")，与基线逐字一致；布尔仍是 INTEGER 0/1。

本文件只声明，不接运行期。见 base.py 说明。
"""
from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# server_default 的时间字面量：**保持 001 基线的 DDL 原文**（datetime('now')）。
# 这是「建表默认值」，由 001 迁移冻结、必须与库结构逐字节一致，不走 func.now()——
# 若改为 func.now() 会让 create_all 的 DDL 偏离基线（parity 探针会拦截）。
# 运行期 INSERT/UPDATE 写「当前时间」的收敛是另一回事：S3.4 用 dialect.now_expr()
# （= func.now()，SQLite 下编译为 CURRENT_TIMESTAMP，与 datetime('now') 同格式同 UTC）。
# 二者分属「DDL 默认值」与「运行期取值表达式」，刻意分离。见 dialect.py。
_NOW = text("(datetime('now'))")
# 自增主键表的 __table_args__（SQLite AUTOINCREMENT：禁止 rowid 复用，对齐基线）
_AUTOINC = {"sqlite_autoincrement": True}


# ── 基表（无外键）────────────────────────────────────────────────

class Project(Base):
    __tablename__ = "projects"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    local_path: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'active'"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    git_url: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))


class AgentTemplate(Base):
    __tablename__ = "agent_templates"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    division: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    description: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    emoji: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    color: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    source_path: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    body: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    imported_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    tags: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    origin: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'scan'"))


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_id: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    updated_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    nickname: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    avatar: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))


class AgentSkill(Base):
    __tablename__ = "agent_skills"

    agent_slug: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    skill_slug: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_salt: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user'"))
    token: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    last_seen: Mapped[str | None] = mapped_column(Text, nullable=True)


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    source_path: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    body: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    imported_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    is_dir: Mapped[int] = mapped_column(Integer, nullable=True, server_default=text("0"))
    downloadable: Mapped[int] = mapped_column(Integer, nullable=True, server_default=text("1"))


# ── 引用表（含外键）─────────────────────────────────────────────

class SkillDownload(Base):
    __tablename__ = "skill_downloads"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    ip: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    ts: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("project_agents.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)


class ProjectAgent(Base):
    __tablename__ = "project_agents"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    template_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agent_templates.id"), nullable=True
    )
    slug: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    emoji: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    color: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    persona: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    provider_id: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    enabled: Mapped[int] = mapped_column(Integer, nullable=True, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    is_leader: Mapped[int] = mapped_column(Integer, nullable=True, server_default=text("0"))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    author_slug: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    author_name: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'planning'"))
    assignee_slug: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    conversation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    order_idx: Mapped[int] = mapped_column(Integer, nullable=True, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    priority: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'none'"))
    parent_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    definition_json: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'draft'"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'pending'"))
    state_json: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'{}'"))
    started_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    ended_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'system'"))
    actor_name: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'{}'"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_slug: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'running'"))
    provider_id: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    ended_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    fail_reason: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))


class RunLog(Base):
    __tablename__ = "run_logs"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    channel: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'event'"))
    content: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    tool: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    tool_input: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    tool_output: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_queue_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_slug: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    event: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'{}'"))
    ts: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)


class RunQueue(Base):
    __tablename__ = "run_queue"
    __table_args__ = _AUTOINC

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    agent_slug: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'mention'"))
    is_leader: Mapped[int] = mapped_column(Integer, nullable=True, server_default=text("0"))
    prompt: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=True, server_default=text("'queued'"))
    created_at: Mapped[str] = mapped_column(Text, nullable=True, server_default=_NOW)
    attempts: Mapped[int] = mapped_column(Integer, nullable=True, server_default=text("0"))
    next_retry_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
