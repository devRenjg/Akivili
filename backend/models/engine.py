"""数据底座 S3.2 · 统一 async engine + session 工厂。

给 ORM 提供一个「能连库、且连接调优与 S1 的 get_connection() 完全一致」的入口，
为 S3.4 把业务查询迁到 ORM 铺路。

**与 S1 get_connection() 的调优对齐（逐条一致）**：
    database.py get_connection() 每条连接执行：
        PRAGMA journal_mode = WAL
        PRAGMA busy_timeout = {db_busy_timeout_ms}
        PRAGMA foreign_keys = ON
    本模块用 SQLAlchemy 的 connect 事件监听器，对 engine 每条底层连接执行同样三条
    PRAGMA（busy_timeout 同样每连接读一次 config，与 S1 行为一致）。二者走同一套调优，
    不因 ORM/手写路径而分叉。

**现状（S3.4 已完成）**：业务/路由/执行层的数据访问已全量走 session（本 engine）；
database.py 仅保留 get_connection() 连接工厂（供部分测试 seed）。建表唯一走 Alembic
（S3.6 已下线 init_db 建表职责）。

driver 选型：运行期业务用 async（aiosqlite driver），与 S1 的 aiosqlite 同底层驱动；
迁移仍走 S2 的同步 sqlite3（见决策 2，二者分离）。
"""
from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import load_settings

# 进程级单例：engine 与 session 工厂懒建一次复用（连接池在此维护）。
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _apply_pragmas(dbapi_conn, _connection_record) -> None:
    """每条底层连接建立时执行 S1 同款 PRAGMA（WAL + busy_timeout + foreign_keys）。

    用 DBAPI 游标直接执行，避免走 SQLAlchemy 事务层（WAL 需无活动事务，与 S2 env.py
    的教训一致）。busy_timeout 每连接读一次 config，对齐 S1 get_connection 的行为。
    """
    timeout_ms = load_settings().db_busy_timeout_ms
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        cur.execute("PRAGMA foreign_keys = ON")
    finally:
        cur.close()


def _build_engine() -> AsyncEngine:
    """按当前 config 建 async engine（aiosqlite driver），挂 PRAGMA 监听器。"""
    db_path = load_settings().db_path
    url = "sqlite+aiosqlite:///" + db_path.replace("\\", "/")
    engine = create_async_engine(url, future=True)
    # sync_engine 上挂 connect 监听：每条新连接执行 PRAGMA
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


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
