"""数据底座 S3.3 · 方言 helper（跨引擎收敛的单点）。

把散落在业务代码里的 SQLite 方言字面量收敛到这一层。S3.3 只**定义基础设施**，
真正把 241 处手写 SQL 里的字面量换成这些 helper 是 S3.4 逐表迁移时做（每处只改
一次、不产生中间态）。收敛后，SQLite↔PostgreSQL 的差异只存在于本模块。

覆盖的方言点（见 openspec 决策 3 清单）：

1. **当前时间 now()**（业务 41 处 `datetime('now')`）
   - SQLite：`datetime('now')` → 文本 'YYYY-MM-DD HH:MM:SS'（UTC）
   - PostgreSQL：`now()` / `CURRENT_TIMESTAMP`
   - 收敛为 `now_expr()`（= SQLAlchemy `func.now()`）。已实测：SQLite 下
     `func.now()` 编译为 `CURRENT_TIMESTAMP`，与 `datetime('now')` **格式逐字节
     一致**（同为 UTC、同格式），故 timeutil.to_beijing 解析不受影响。

2. **自增主键 AUTOINCREMENT**（业务 0 处、schema 17 处）
   - 已在 S3.1 模型层用 `sqlite_autoincrement=True` 收口，方言由 SQLAlchemy 生成
     （SQLite→AUTOINCREMENT，PG→IDENTITY/SERIAL）。此处仅登记，无需运行期 helper。

3. **julianday 时间差**（业务 2 处，秒数计算）
   - SQLite：`(julianday(e)-julianday(s))*86400`
   - PostgreSQL：`EXTRACT(EPOCH FROM (e - s))`
   - 收敛为 `elapsed_seconds_sql(start_col, end_col)`：按 engine 方言返回对应 SQL
     片段。S4 双跑时在此加 PG 分支。

4. **upsert INSERT OR IGNORE**（业务 1 处，agent_skills）
   - SQLite：`INSERT OR IGNORE`
   - PostgreSQL：`INSERT ... ON CONFLICT DO NOTHING`
   - 用 SQLAlchemy `insert().on_conflict_do_nothing()`；S3.4 迁 agents.py 时改用
     `insert_or_ignore()`（按方言选 sqlite/pg 的 insert 构造器）。

本模块不接运行期，S3.4 起才被业务代码引用。
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement


def now_expr() -> ColumnElement:
    """跨方言「当前 UTC 时间」表达式。

    SQLite→CURRENT_TIMESTAMP（与旧 datetime('now') 逐字节同格式、同 UTC 语义），
    PostgreSQL→now()。用于 ORM insert/update 的时间列赋值与 server_default。
    """
    return func.now()


# 方言相关的 SQL 片段：按 dialect name 返回。S4 接入 PG 时在此补分支。
_ELAPSED_SECONDS = {
    # SQLite：julianday 差 * 86400
    "sqlite": "(julianday({end}) - julianday({start})) * 86400",
    # PostgreSQL：EXTRACT EPOCH（S4 双跑启用；此刻登记，未被调用）
    "postgresql": "EXTRACT(EPOCH FROM ({end} - {start}))",
}


def elapsed_seconds_sql(start_col: str, end_col: str, dialect: str = "sqlite") -> str:
    """返回「end 与 start 相差秒数」的 SQL 片段（按方言）。

    start/end 为列名（调用方保证是可信标识符，非用户输入）。当前仅 SQLite 被调用，
    PG 分支 S4 双跑时启用。
    """
    tmpl = _ELAPSED_SECONDS.get(dialect)
    if tmpl is None:
        raise ValueError(f"elapsed_seconds_sql 未覆盖方言: {dialect}")
    return tmpl.format(start=start_col, end=end_col)


def insert_or_ignore(table):
    """返回「插入冲突则忽略」的 ORM insert 构造器（按当前 engine 方言分支）。

    SQLite→sqlite.insert().on_conflict_do_nothing()，
    PostgreSQL→postgresql.insert().on_conflict_do_nothing()。
    调用方再 .values(...) 并执行。S3.4 迁 agents.py 的 INSERT OR IGNORE 时改用本函数。
    """
    from models.engine import get_engine

    name = get_engine().dialect.name
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _ins
    elif name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _ins
    else:
        raise ValueError(f"insert_or_ignore 未覆盖方言: {name}")
    return _ins(table).on_conflict_do_nothing()


def _dialect_insert(table):
    """按当前 engine 方言返回对应的 insert 构造器（支持 on_conflict_*）。"""
    from models.engine import get_engine

    name = get_engine().dialect.name
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _ins
    elif name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _ins
    else:
        raise ValueError(f"upsert 未覆盖方言: {name}")
    return _ins(table)


def upsert(table, index_elements, insert_values: dict, update_values: dict):
    """「插入冲突则按 update_values 更新」（对齐 SQLite INSERT ... ON CONFLICT DO UPDATE）。

    - index_elements：冲突判定列（如 ['slug']），对应旧 SQL 的 ON CONFLICT(slug)。
    - insert_values：无冲突时插入的完整值。
    - update_values：有冲突时更新的列（对应 DO UPDATE SET ...；旧代码里用 excluded.x，
      这里直接给最终值即可）。
    SQLite→sqlite.insert().on_conflict_do_update()，PG 同名分支，S4 无缝延续。
    """
    stmt = _dialect_insert(table).values(**insert_values)
    return stmt.on_conflict_do_update(index_elements=index_elements, set_=update_values)
