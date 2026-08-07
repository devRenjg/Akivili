"""task_runs usage 列 — 捕获 CLI 真实 token 消耗（Session Resume 联调可观测 + 永久指标）

给 task_runs 加 3 列，落库每个 run 的真实 token 用量（从 CLI 流的 usage 事件提取）：
  - usage_input_tokens         输入 token 数（含历史/系统提示；resume run 应显著低于全量 run）
  - usage_cached_input_tokens  命中缓存的输入 token（claude cache_read / codex cached_input）
  - usage_output_tokens        输出 token 数

来源事件（实测确认）：claude `type=result` 的 usage.{input_tokens/cache_read_input_tokens/
output_tokens}；codex `type=turn.completed` 的 usage.{input_tokens/cached_input_tokens/output_tokens}。
现状两 CLI 的 usage 事件都被 _parse_line 丢弃，本 change 起提取落库，供 Session Resume token-drop
对比（全量 run vs resume run）与长期成本可观测。

PG 单引擎（S5 全仓零 sqlite）：非 PG 跳过；DO $$ IF NOT EXISTS 幂等加列（对齐 005/006）。

Revision ID: 008
Revises: 007
Create Date: 2026-07-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = '008'
down_revision: Union[str, Sequence[str], None] = '007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "task_runs"
# (列名, 类型) —— 与 models/tables.py::TaskRun 保持一致
_COLUMNS: list[tuple[str, str]] = [
    ("usage_input_tokens", "INTEGER"),
    ("usage_cached_input_tokens", "INTEGER"),
    ("usage_output_tokens", "INTEGER"),
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
