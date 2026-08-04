"""task_runs.kill_requested_at — 跨进程 kill 信号列

Revision ID: 004
Revises: 003
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '004'
down_revision: Union[str, Sequence[str], None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# worker-split-minimal 组 1 · D 类跨进程 kill 信号（对标 Multica「状态即信号」）。
# 执行面剥离到独立 worker 进程后，队列路径的 CLI 子进程挂在 worker 名下、API 进程的 _RUN_PIDS
# 是空的——前端「终止运行」经 /runs/kill 打到 API，API 已无法直接 kill 队列路径的 run。
# 加信号列：API 收到 kill 落 kill_requested_at（now_expr() 时间戳），worker 周期 sweep 扫到
# 「kill_requested_at IS NOT NULL 且本进程 _RUN_PIDS 有此 run」→ kill_run + finalize。NULL=无请求。
#
# 幂等：全新库经 001 的 ORM create_all（tables.py TaskRun 已声明本列）建好本列，本迁移空转；
# 存量库（建于本列之前）在此真正补列。用 information_schema 判存在再 ADD，避免撞列已存在。
_TABLE = "task_runs"
_COLUMN = "kill_requested_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # S5 PG 单引擎；非 PG 不应到达此处（migration_db_url 只认 PG）。防御性跳过。
        return
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = '{_TABLE}' AND column_name = '{_COLUMN}'
            ) THEN
                ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} TEXT;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
