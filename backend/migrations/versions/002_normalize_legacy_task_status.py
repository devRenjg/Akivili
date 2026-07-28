"""normalize legacy task status

Revision ID: 002
Revises: 001
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, Sequence[str], None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 数据底座 S3.6 · 遗留任务状态规整
# 原 database._migrate() 除补列外，还带两条**数据规整** UPDATE：把已废弃状态
# 「planning→backlog」「archived→done」就地合并。补列已全部落 001 基线，唯这两条
# 数据逻辑不属 schema。S3.6 下线 init_db/_migrate 时，把它落成一次性数据迁移，
# 保持真相源唯一（建表 + 数据规整都走 Alembic）。幂等：规整后无 planning/archived 行，
# 重跑为空更新。见 openspec s3.6-plan / 决策 2。


def upgrade() -> None:
    op.execute("UPDATE tasks SET status='backlog' WHERE status='planning'")
    op.execute("UPDATE tasks SET status='done' WHERE status='archived'")


def downgrade() -> None:
    # 不可逆：planning/archived 是已废弃状态，规整后原值语义已丢失，不做反向。
    # 与「状态枚举下线」一致——回滚 schema 不代表要复活废弃状态。
    pass
