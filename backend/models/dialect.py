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

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ColumnElement


# 时间字符串统一格式：'YYYY-MM-DD HH:MM:SS'（UTC、秒级、无时区后缀）。
# 两个引擎的时间列都是 TEXT 存这个格式——SQLite 的 datetime('now')/CURRENT_TIMESTAMP 天生如此，
# PG 侧用 to_char(... AT TIME ZONE 'UTC', ...) 强制归一化到同格式，保证跨引擎逐字节一致
# （字典序比较、timeutil.to_beijing 解析、S4.6 数据迁移全无缝）。见 openspec S4.4 决策。
_PG_TS_NORMALIZE = "'YYYY-MM-DD HH24:MI:SS'"


class now_expr(ColumnElement):
    """跨方言「当前 UTC 时间」运行期表达式（用于 INSERT/UPDATE 的时间列赋值）。

    - SQLite→ `CURRENT_TIMESTAMP`（与旧 datetime('now') 逐字节同格式、同 UTC 语义）。
    - PostgreSQL→ `to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')`——归一化到
      SQLite 同格式（秒级 text、无时区后缀），保证时间列跨引擎逐字节一致。

    历史上是 func.now()；S4.4 起改为方言感知元素，因 PG 原生 now() 带微秒+时区、格式与
    SQLite 不符。sqlite 侧渲染不变，S3.4 的 41 处调用行为一致。
    """

    inherit_cache = True


@compiles(now_expr, "sqlite")
def _now_expr_sqlite(element, compiler, **kw):  # noqa: ANN001, ARG001
    # 与历史 func.now() 在 sqlite 下的渲染一致，保证 S3.4 各处逐字节不变。
    return "CURRENT_TIMESTAMP"


@compiles(now_expr, "postgresql")
def _now_expr_pg(element, compiler, **kw):  # noqa: ANN001, ARG001
    return f"to_char(now() AT TIME ZONE 'UTC', {_PG_TS_NORMALIZE})"


class now_default_ddl(ColumnElement):
    """建表 DDL 的「当前时间」server_default，按方言编译（S4 双引擎）。

    - SQLite→ `(datetime('now'))`：与 001 基线 DDL 逐字节一致，存量库/parity 探针零影响。
    - PostgreSQL→ `(to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'))`：归一化到
      SQLite 同格式（秒级 text），PG 无 datetime() 函数、原生 now() 格式也不符，故用 to_char。

    仅用于 mapped_column(server_default=now_default_ddl())——即建表时冻结进 schema 的
    默认值。运行期 INSERT/UPDATE 取「当前时间」用 now_expr()，二者分属「DDL 默认值」与
    「运行期取值」，刻意分离（见 tables.py 说明）。
    """

    inherit_cache = True


@compiles(now_default_ddl, "sqlite")
def _now_default_sqlite(element, compiler, **kw):  # noqa: ANN001, ARG001
    # 括号写法与 001 基线逐字一致：server_default=text("(datetime('now'))") 的等价渲染。
    return "(datetime('now'))"


@compiles(now_default_ddl, "postgresql")
def _now_default_pg(element, compiler, **kw):  # noqa: ANN001, ARG001
    return f"(to_char(now() AT TIME ZONE 'UTC', {_PG_TS_NORMALIZE}))"


class now_offset(ColumnElement):
    """「当前时间 ± N 秒」的方言感知表达式，产出与时间列同格式的 UTC text。

    替代原 `func.datetime(now_expr(), '+N seconds' / '-N hours' / '-N days')`——那是 SQLite
    方言（datetime 的相对修饰符），PG 无此函数。hours/days 由调用方换算成秒统一走本元素。

    - SQLite→ `datetime('now', '±N seconds')`（与旧 SQL 逐字节一致）。
    - PostgreSQL→ `to_char((now() AT TIME ZONE 'UTC') + N * interval '1 second', 'YYYY-...')`，
      归一化到同格式。

    delta_seconds 为整数（正=未来，负=过去），非用户输入（调用方由 backoff/hours/days 换算）。
    """

    inherit_cache = True

    def __init__(self, delta_seconds: int):
        self.delta_seconds = int(delta_seconds)


@compiles(now_offset, "sqlite")
def _now_offset_sqlite(element, compiler, **kw):  # noqa: ANN001, ARG001
    n = element.delta_seconds
    sign = "+" if n >= 0 else "-"
    return f"datetime('now', '{sign}{abs(n)} seconds')"


@compiles(now_offset, "postgresql")
def _now_offset_pg(element, compiler, **kw):  # noqa: ANN001, ARG001
    n = element.delta_seconds
    return (f"to_char((now() AT TIME ZONE 'UTC') + ({n} * interval '1 second'), "
            f"{_PG_TS_NORMALIZE})")


class elapsed_seconds(ColumnElement):
    """两个时间列相差的秒数（end - start），方言感知。

    替代原 `(func.julianday(end) - func.julianday(start)) * 86400`——julianday 是 SQLite
    方言。时间列在两引擎都是 TEXT，PG 侧先 ::timestamp 再 EXTRACT EPOCH。

    - SQLite→ `(julianday(end) - julianday(start)) * 86400`（与旧 SQL 逐字节一致）。
    - PostgreSQL→ `EXTRACT(EPOCH FROM (end::timestamp - start::timestamp))`。

    start_expr / end_expr 为 SQLAlchemy 列表达式（ORM 列或标量子查询）。
    """

    inherit_cache = True

    def __init__(self, end_expr, start_expr):
        self.end_expr = end_expr
        self.start_expr = start_expr


@compiles(elapsed_seconds, "sqlite")
def _elapsed_seconds_sqlite(element, compiler, **kw):  # noqa: ANN001
    end_sql = compiler.process(element.end_expr, **kw)
    start_sql = compiler.process(element.start_expr, **kw)
    return f"(julianday({end_sql}) - julianday({start_sql})) * 86400"


@compiles(elapsed_seconds, "postgresql")
def _elapsed_seconds_pg(element, compiler, **kw):  # noqa: ANN001
    end_sql = compiler.process(element.end_expr, **kw)
    start_sql = compiler.process(element.start_expr, **kw)
    return f"EXTRACT(EPOCH FROM ({end_sql}::timestamp - {start_sql}::timestamp))"


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
