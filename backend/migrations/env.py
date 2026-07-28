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
# 迁移用同步 driver（sqlite→pysqlite / PostgreSQL→psycopg v3），与运行期异步 driver 解耦——
# 迁移是启动一次性 DDL、无需 async。优先级/双引擎逻辑集中在 config.migration_db_url()。
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

    # 迁移连接置 WAL，与运行期库模式一致（见 openspec 决策 2 / 决策 6）。仅 SQLite 有意义——
    # PostgreSQL 的 WAL 是服务端常开、由 postgresql.conf 管，客户端无 journal_mode PRAGMA。
    # 🔴 PRAGMA journal_mode=WAL 必须在**无活动事务**下执行才能持久生效；SQLAlchemy 2.0 的
    #    connection.execute 会 autobegin 事务，若在其中跑 PRAGMA 会扰乱 alembic 的版本 stamp
    #    事务（导致 alembic_version 写不进、每次 upgrade 重跑建表）。故用独立的 AUTOCOMMIT
    #    连接先把库切到 WAL，再用干净连接跑迁移。
    if connectable.dialect.name == "sqlite":
        from sqlalchemy import text  # noqa: PLC0415
        with connectable.connect().execution_options(isolation_level="AUTOCOMMIT") as _wal_conn:
            _wal_conn.execute(text("PRAGMA journal_mode=WAL"))

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
