"""数据底座 S3.2 · 统一 async engine + session 工厂（S5：PostgreSQL 单引擎）。

给 ORM 提供进程级唯一的 async engine / session 工厂。业务/路由/执行层的数据访问全量
走本 engine（S3.4 完成）；建表唯一走 Alembic（S3.6 下线 init_db 建表职责）。

**数据底座 S5：PostgreSQL 单引擎**。SQLite 已退役——无 sqlite 分支、无 PRAGMA 监听
（PG 的 WAL/MVCC/外键是服务端内建强制的，无需也不接受 journal_mode/busy_timeout 之类
PRAGMA）。运行期 driver 恒为 asyncpg（异步）；迁移期由 config._pg_sync_url 转 psycopg。
连接串来自 config.db_url（AKIVILI_DB_URL 环境变量优先，否则 _default_pg_url 拼默认）。
"""
from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from config import load_settings

# 进程级单例：engine 与 session 工厂懒建一次复用（连接池在此维护）。
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    """按当前 config 建 PostgreSQL async engine（S5：PG 单引擎）。

    db_url 恒为 postgresql+asyncpg://…（config 保证：AKIVILI_DB_URL 优先，否则默认 PG 串）。
    不再有 SQLite 分支/PRAGMA 监听。

    连接池（S5b 调优，参数全部来自 config，可环境变量覆盖，不硬编码）：生产用 SQLAlchemy
    QueuePool，显式设 pool_size/max_overflow/pool_timeout/pool_recycle + pool_pre_ping——
    pre_ping 借出前探活，扛 PG 重启/网络抖动/idle 断连；recycle 防 PG 侧 idle 超时踢连接。
    容量按本平台单进程 MAX_CONCURRENCY 规模取值（默认 5+5），非照搬 Multica 的 MaxConns=25。

    **测试**（AKIVILI_TEST_NULLPOOL=1）用 NullPool——探针常在一进程内多次 asyncio.run()
    跨事件循环，asyncpg 的池连接是 loop 绑定的，跨 loop 复用会 "Event loop is closed"；
    NullPool 每次现开现关，规避跨 loop 问题。NullPool 不接受 pool_size/overflow/timeout，
    故此分支不传这些参数。对生产零影响（生产不设该变量）。
    """
    settings = load_settings()
    url = settings.db_url
    if os.environ.get("AKIVILI_TEST_NULLPOOL") == "1":
        return create_async_engine(url, future=True, poolclass=NullPool)
    return create_async_engine(
        url,
        future=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_sec,
        pool_recycle=settings.db_pool_recycle_sec,
        pool_pre_ping=settings.db_pool_pre_ping,
    )


def get_engine() -> AsyncEngine:
    """获取进程级 async engine（懒建单例）。"""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取 async session 工厂（懒建单例）。

    用法（S3.4 起）：
        async with get_session_factory()() as session:
            ...  # ORM 查询
    expire_on_commit=False：commit 后对象仍可读属性，贴合请求-响应式短事务用法。
    """
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def dispose_engine() -> None:
    """释放 engine 连接池（关停/测试清理用）。释放后下次 get_engine 会重建。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def ping() -> int:
    """连通性自检：开一条 session 执行 SELECT 1，返回 1。"""
    async with get_session_factory()() as session:
        result = await session.execute(text("SELECT 1"))
        return int(result.scalar_one())
