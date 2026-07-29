from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 数据底座 S2：迁移框架。本 env 只跑迁移 DDL，不引入 ORM 模型，故 target_metadata=None
# （autogenerate 不用；001 及后续迁移手写）。见 openspec 决策 2。
target_metadata = None

# DB URL 不写死在 alembic.ini，运行时从 backend/config.py 单一构造（migration_db_url）。
# 数据底座 S5：PG 单引擎，迁移用同步 psycopg v3 driver（与运行期异步 asyncpg 解耦——
# 迁移是启动一次性 DDL、无需 async）。URL 逻辑集中在 config.migration_db_url()。
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from config import migration_db_url  # noqa: E402

_MIGRATION_URL = migration_db_url()
config.set_main_option("sqlalchemy.url", _MIGRATION_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # 数据底座 S5：PG 单引擎。PostgreSQL 的 WAL 是服务端常开、由 postgresql.conf 管，
    # 客户端无 journal_mode PRAGMA，故不再有 SQLite 时代的 WAL 置位步骤。
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
