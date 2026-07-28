"""SQLite 数据库连接入口（统一调优）。

🔴 数据底座 S3.6：建表/schema 真相源**唯一为 Alembic**（migrations/）。原 SCHEMA 常量 +
init_db() + _migrate() 已随 S3.6 下线——它们自 001_baseline 起就是冗余（生产 main._startup
早已 Alembic-first），遗留的两条数据规整（planning→backlog / archived→done）落成 002 迁移。
本模块现只保留连接工厂 get_connection()（仍供部分测试 seed 造数）；业务/路由/执行层的
数据访问已在 S3.4 全量迁到 SQLAlchemy ORM（models/）。见 openspec s3.6-plan / 决策 2。
"""
import aiosqlite

from config import load_settings


def get_db_path() -> str:
    return load_settings().db_path


async def get_connection() -> aiosqlite.Connection:
    """获取统一调优的连接：WAL(读写不互斥) + busy_timeout(写锁竞争等待) + 外键约束，
    行可按列名取值。调用方负责关闭（全库统一 try/finally: await db.close()）。"""
    db = await aiosqlite.connect(get_db_path())
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute(f"PRAGMA busy_timeout = {load_settings().db_busy_timeout_ms}")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    return db
