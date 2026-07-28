"""数据底座 S3.1 · ORM 模型层。

对齐 migrations/versions/001_baseline.py 的 18 张表，SQLAlchemy 2.0 typed 声明式模型。
本阶段仅声明、不接运行期（engine/session 见 S3.2，业务查询迁移见 S3.4）。

用法（S3.2 起）：
    from models import Base, Project, Task, ...
"""
from .base import Base
from .engine import (
    dispose_engine,
    get_engine,
    get_session_factory,
    ping,
)
from .tables import (
    Activity,
    AgentProfile,
    AgentSkill,
    AgentTemplate,
    Conversation,
    Message,
    Project,
    ProjectAgent,
    RunEvent,
    RunLog,
    RunQueue,
    Skill,
    SkillDownload,
    Task,
    TaskRun,
    User,
    Workflow,
    WorkflowRun,
)

__all__ = [
    "Base",
    # engine / session（S3.2）
    "get_engine",
    "get_session_factory",
    "dispose_engine",
    "ping",
    # 表模型（S3.1）
    "Activity",
    "AgentProfile",
    "AgentSkill",
    "AgentTemplate",
    "Conversation",
    "Message",
    "Project",
    "ProjectAgent",
    "RunEvent",
    "RunLog",
    "RunQueue",
    "Skill",
    "SkillDownload",
    "Task",
    "TaskRun",
    "User",
    "Workflow",
    "WorkflowRun",
]
