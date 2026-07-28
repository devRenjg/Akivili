"""数据底座 S2：启动时的 Alembic 迁移接入（编程式 upgrade head）。

在 main.py startup 早期调用 `run_migrations()`，让 schema 由 Alembic 唯一定义、
启动自动 apply。三种库状态都要健壮处理：

1. 全新空库（无表、无 alembic_version）→ upgrade head 建全部基线表。
2. 存量库已纳管（有表 + alembic_version=head）→ upgrade head 见已是 head，空跑。
3. 存量库未纳管（有表、无 alembic_version）→ **自动 stamp 到 head**（不重跑建表 DDL），
   把老库一次性纳入 Alembic 管理。等价于 S2.6 的手工 `alembic stamp 001`，
   但内建为兜底，避免"忘了先 stamp 就 upgrade"导致撞表已存在、启动崩溃。

迁移用同步 driver（与运行期异步 driver 解耦）：sqlite 走 pysqlite、PostgreSQL 走
psycopg v3（asyncpg 纯异步跑不了 Alembic 同步引擎）。迁移 URL 由 config.migration_db_url()
单一构造，见 openspec s2-plan / 决策 2 / S4.1。
"""
import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from config import migration_db_url

# 本文件所在目录 = backend/，alembic.ini 与 migrations/ 都在其下。
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ALEMBIC_INI = os.path.join(_BACKEND_DIR, "alembic.ini")
_MIGRATIONS_DIR = os.path.join(_BACKEND_DIR, "migrations")


def _alembic_config() -> Config:
    cfg = Config(_ALEMBIC_INI)
    # 用绝对路径覆盖脚本位置，保证从任意 cwd 调用都能找到 migrations/。
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    return cfg


def run_migrations() -> str:
    """把当前库带到最新 schema 版本。返回执行动作（'upgrade' | 'stamp' | 'noop'）供日志。

    同步执行（Alembic 本身同步）；在 startup 里用 asyncio.to_thread 或直接调用皆可，
    因为它只在启动早期跑一次、耗时极短。
    """
    url = migration_db_url()
    engine = create_engine(url)
    try:
        # 判定库状态：有无业务表、有无 alembic_version 记录。
        insp = inspect(engine)
        table_names = [t for t in insp.get_table_names() if t != "alembic_version"]
        has_business_tables = len(table_names) > 0
        with engine.connect() as conn:
            mc = MigrationContext.configure(conn)
            current_rev = mc.get_current_revision()
    finally:
        engine.dispose()

    cfg = _alembic_config()

    if has_business_tables and current_rev is None:
        # 状态 3：存量库未纳管——stamp 到 head，不重跑建表 DDL。
        command.stamp(cfg, "head")
        return "stamp"

    # 状态 1（空库）或状态 2（已纳管）：正常 upgrade（空库建表；已 head 则空跑）。
    command.upgrade(cfg, "head")
    return "upgrade" if current_rev is None else "noop"
