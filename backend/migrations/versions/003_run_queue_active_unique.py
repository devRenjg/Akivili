"""run_queue active-run partial unique index

Revision ID: 003
Revises: 002
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '003'
down_revision: Union[str, Sequence[str], None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 数据底座 S5 · run_queue「活跃 run 唯一」DB 兜底
# collab.enqueue_run 的去重（同一 (task_id, agent_slug) 已有 queued/running 就不重复入队）
# 历史上是**应用层 SELECT-then-INSERT**——SQLite 单写串行掩盖了 TOCTOU 竞态；迁 PG 后真并发下
# 两个并发调用可能都过 SELECT、都 INSERT，进两条活跃 run（pg_concurrency 探针已把该隐患写进告警）。
# 加**部分唯一索引**把软去重升级为 DB 硬保证：同 (task_id, agent_slug) 至多一条 queued/running。
# done/failed 历史行不在索引范围内（允许同一成员在同一任务上重跑），与应用层去重口径逐字一致。
#
# 幂等：用 CREATE UNIQUE INDEX IF NOT EXISTS——全新库经 001 create_all 已按 ORM 模型
# （tables.py 的 Index 声明）建好本索引，本迁移空转；存量库（建于本索引之前）在此真正补建。
_INDEX_NAME = "uq_run_queue_active"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # S5 PG 单引擎；非 PG 不应到达此处（migration_db_url 只认 PG）。防御性跳过。
        return
    # 建索引前先清脏数据：TOCTOU 曾漏进的「同 (task,agent) 多条 queued/running」会让唯一索引建失败。
    # 保留 id 最大（最新）的一条，其余就地置 failed（不删行，保留调度流水可追溯）。
    op.execute(
        """
        UPDATE run_queue rq SET status = 'failed'
        WHERE rq.status IN ('queued', 'running')
          AND rq.id < (
            SELECT MAX(inner_rq.id) FROM run_queue inner_rq
            WHERE inner_rq.task_id = rq.task_id
              AND inner_rq.agent_slug = rq.agent_slug
              AND inner_rq.status IN ('queued', 'running')
          )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
        "ON run_queue (task_id, agent_slug) "
        "WHERE status IN ('queued', 'running')"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
