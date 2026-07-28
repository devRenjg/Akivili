"""数据底座 S3.1 · ORM 声明基类。

SQLAlchemy 2.0 typed 声明式基类，所有表模型继承 Base。

本阶段（S3.1）仅定义模型、不接入运行期（不建 engine/session、不改业务查询）——
纯声明，零行为变更。engine/session 工厂见 S3.2，业务查询迁移见 S3.4。

模型逐字对齐 migrations/versions/001_baseline.py（= 真实库 sqlite_master 快照）：
列名 / 类型 / 可空 / 默认值 / 主外键一一对应。方言字面量（datetime('now') 等）
此阶段原样保留，收敛留待 S3.3（见 openspec 决策 3）。
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类（对齐 001 基线）。"""

    pass
