"""数据底座 S2：启动时的 Alembic 迁移接入（编程式 upgrade head）。

在 main.py startup 早期调用 `run_migrations()`，让 schema 由 Alembic 唯一定义、
启动自动 apply。三种库状态都要健壮处理：

1. 全新空库（无表、无 alembic_version）→ upgrade head 建全部基线表。
2. 存量库已纳管（有表 + alembic_version=head）→ upgrade head 见已是 head，空跑。
3. 存量库未纳管（有表、无 alembic_version）→ **自动 stamp 到 head**（不重跑建表 DDL），
   把老库一次性纳入 Alembic 管理。等价于 S2.6 的手工 `alembic stamp 001`，
   但内建为兜底，避免"忘了先 stamp 就 upgrade"导致撞表已存在、启动崩溃。

数据底座 S5：PG 单引擎。迁移用同步 psycopg v3 driver（asyncpg 纯异步跑不了 Alembic
同步引擎）；迁移 URL 由 config.migration_db_url() 单一构造。见 openspec s2-plan / S5。
"""
import os
import time

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from config import migration_db_url

# 迁移串行化用的 PostgreSQL advisory lock key（S5b：对标 Multica 自建 migrator 的
# pg_advisory_lock 迁移串行化）。这是"固定但任意"的协议常量——类比具名互斥量/约定端口，
# 不是业务配置，取值只需全局唯一且固定。多实例并发启动（见记忆 backend-restart-single-instance：
# 曾出现多个 8100 监听并存）时都抢这把锁，防两个 command.upgrade 同时改 alembic_version
# 造成版本表竞争 / 迁移半执行。session 级锁：持锁连接不关则锁不释放，连接断开自动释放。
_MIGRATION_ADVISORY_LOCK_KEY = 0x414B564D  # 助记 'A''K''V''M'，值本身无业务含义


def _lock_timeout_sec() -> float:
    """拿迁移锁的最长等待秒数。可用环境变量覆盖；默认 30s，与 wait_for_pg 就绪探测量级一致。"""
    return float(os.environ.get("AKIVILI_MIGRATION_LOCK_TIMEOUT", "30"))


def _acquire_migration_lock(conn) -> None:
    """在给定（AUTOCOMMIT）连接上拿迁移 advisory lock；拿不到就轮询重试，超时抛错(fail-closed)。

    用 pg_try_advisory_lock（非阻塞、返回 bool）轮询而非阻塞式 pg_advisory_lock——后者的等待
    不受 lock_timeout 可靠约束，轮询可精确控超时且日志友好。拿不到锁 = 有另一实例在迁移，
    fail-closed 抛错（宁可启动失败，绝不无锁裸迁移），由单实例纪律 + start.ps1 杀端口保证正常路径不撞。
    """
    budget = _lock_timeout_sec()
    waited = 0.0
    interval = 0.5
    while True:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _MIGRATION_ADVISORY_LOCK_KEY},
        ).scalar_one()
        if got:
            return
        if waited >= budget:
            raise RuntimeError(
                f"获取迁移 advisory lock 超时（>{budget:.0f}s）：疑似有另一实例正在迁移。"
                "已 fail-closed 拒绝无锁裸迁移，请确认单实例启动后重试。"
            )
        time.sleep(interval)
        waited += interval

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
    # 持锁连接：整个"判状态 + upgrade/stamp"期间不关，保证 advisory lock（session 级）全程持有；
    # AUTOCOMMIT 让 pg_try_advisory_lock 立即生效、不被后续事务边界干扰。alembic 的 command.*
    # 内部另开自己的连接执行 DDL，与本持锁连接并存于同一 engine，锁在库级串行化它们。
    lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        _acquire_migration_lock(lock_conn)

        # 判定库状态：有无业务表、有无 alembic_version 记录。
        insp = inspect(engine)
        table_names = [t for t in insp.get_table_names() if t != "alembic_version"]
        has_business_tables = len(table_names) > 0
        mc = MigrationContext.configure(lock_conn)
        current_rev = mc.get_current_revision()

        cfg = _alembic_config()

        if has_business_tables and current_rev is None:
            # 状态 3：存量库未纳管——stamp 到 head，不重跑建表 DDL。
            command.stamp(cfg, "head")
            return "stamp"

        # 状态 1（空库）或状态 2（已纳管）：正常 upgrade（空库建表；已 head 则空跑）。
        command.upgrade(cfg, "head")
        return "upgrade" if current_rev is None else "noop"
    finally:
        # 关连接即释放 session 级 advisory lock（显式 unlock 也行，关连接更稳妥不漏）。
        lock_conn.close()
        engine.dispose()
