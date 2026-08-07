"""run_queue session 列 — Session Resume 阶段一 attempt 间续跑传递载体

给 run_queue 加 2 列，作为「同一 queue item 跨 attempt 续跑」的传递载体：
  - cli_session_id            上次 attempt 起的 CLI 会话 id；下次 attempt 读它 → --resume 续跑
  - session_committed_msg_id  增量回灌水位：上次成功已消费到的最高 message id（成功才推进）

架构现实：execute_dispatch 每次执行（含重试）新建 task_runs 行、重试是同 item 下一次 attempt
（全新 run_id），故「同 run 续跑」= 同一 run_queue item 跨 attempt 续跑，session 指针须挂在
run_queue（attempt 间不变的载体）而非仅 task_runs（每 attempt 一条）。

对齐 change openspec/changes/agent-session-resume-minimal（tasks S2.0）。

Revision ID: 006
Revises: 005
Create Date: 2026-07-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = '006'
down_revision: Union[str, Sequence[str], None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "run_queue"
_COLUMNS: list[tuple[str, str]] = [
    ("cli_session_id", "TEXT"),
    ("session_committed_msg_id", "INTEGER"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for col, coltype in _COLUMNS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = '{_TABLE}' AND column_name = '{col}'
                ) THEN
                    ALTER TABLE {_TABLE} ADD COLUMN {col} {coltype};
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    for col, _ in _COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {col}")
