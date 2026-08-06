"""task_runs session 列 — Session Resume 阶段一（同 run 续跑）地基

给 task_runs 加 4 列，支撑「run 被打断后用存下的 CLI 会话 id --resume 续跑」：
  - cli_session_id           CLI 原生会话 id（claude=预分配 UUID / codex=抓来的 thread_id）
  - session_backend          执行后端（claude / codex），resume 前一致性校验用
  - session_workdir          执行工作目录，resume 前一致性校验用（换目录旧 session 无意义）
  - session_committed_msg_id 增量回灌水位：上次成功已消费到的最高 message id（成功才推进）

对齐 change openspec/changes/agent-session-resume-minimal（tasks S1.1）。

Revision ID: 005
Revises: 004
Create Date: 2026-07-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = '005'
down_revision: Union[str, Sequence[str], None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "task_runs"
# (列名, 类型) —— 与 models/tables.py::TaskRun 保持一致
_COLUMNS: list[tuple[str, str]] = [
    ("cli_session_id", "TEXT"),
    ("session_backend", "TEXT"),
    ("session_workdir", "TEXT"),
    ("session_committed_msg_id", "INTEGER"),
]


def upgrade() -> None:
    bind = op.get_bind()
    # PG 单引擎：非 PG 直接跳过（对齐 004 及全仓 postgres-only 约定）
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
