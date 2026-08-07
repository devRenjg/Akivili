"""agent_sessions 表 — Session Resume 阶段二（跨 task 续接的 best-effort 会话缓存）

新建 agent_sessions 表，缓存同一 (conversation, agent) 的 CLI 会话指针 + 增量水位：
run 成功收尾时 upsert（唯一键 (conversation_id, agent_slug)，后写覆盖，不加锁不校验 owner）；
同一 (conversation, agent) 的下一个 task 全新起 run 时查它命中上个 task 的会话 → resume 续接，
省历史重放 token。best-effort 缓存：并发写互盖最坏 miss 一次 → 降级全量（S4 兜底），无数据损坏。

对齐 change openspec/changes/agent-session-resume-minimal（tasks S5.1）。
PG 单引擎（S5 全仓零 sqlite）：非 postgresql 直接返回；CREATE TABLE IF NOT EXISTS 幂等。

Revision ID: 007
Revises: 006
Create Date: 2026-07-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = '007'
down_revision: Union[str, Sequence[str], None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "agent_sessions"


def upgrade() -> None:
    """建 agent_sessions 表。走 ORM metadata.create_all（单表 checkfirst）而非手写 DDL——
    与 001 baseline 的 PG 分支同法：SQLAlchemy 按 PG 方言生成 DDL（SERIAL/now()），
    与模型声明零漂移，由 schema_parity 探针守护。手写 DDL 易与 ORM 的 id 序列/默认值渲染
    不一致（如 IDENTITY vs SERIAL）而破坏 parity。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    import sys
    from pathlib import Path
    _backend = Path(__file__).resolve().parents[2]
    if str(_backend) not in sys.path:
        sys.path.insert(0, str(_backend))
    from models.tables import AgentSession  # noqa: PLC0415
    # checkfirst=True：表已存在则跳过（幂等，对齐 005/006 的 IF NOT EXISTS 语义）。
    AgentSession.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
