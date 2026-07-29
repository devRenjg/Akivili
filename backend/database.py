"""测试 seed 连接工厂（数据底座 S5：PostgreSQL 单引擎）。

⚠️ 遗留边界：业务/路由/执行层 S3.4 已全量走 ORM（models/），运行期**零调用**本模块。
唯一使用者是 TestReport 的测试 seed（造数/断言）。历史上返回 aiosqlite 连接；S5 去 sqlite
后，本模块改为返回一个**保留 aiosqlite 式 API 的 PostgreSQL 适配器**——让 25+ 探针的
`db.execute(sql, tuple)` / `cur.lastrowid` / `commit` / `close` / `fetchone` / `fetchall`
几乎零改即可跑在 PG 上（见 .claude/plans/s5-postgres-only.md 的"两层收敛"）。

适配要点：
  - 占位符 `?` → asyncpg 的 `$1,$2…`；
  - `INSERT`（目标表含 id 列）自动补 `RETURNING id` 以支撑 `cur.lastrowid`；
  - 行用 asyncpg Record，天然支持 row[0] 与 row["col"] 两种取值（同 aiosqlite.Row）；
  - asyncpg 默认自动提交，故 `commit()` 为空操作、`close()` 关连接（seed 无回滚需求）。
"""
from __future__ import annotations

import re

import asyncpg

from config import load_settings

# 无 id 自增列的表：INSERT 不能补 RETURNING id（agent_skills 复合主键、agent_profiles 文本主键）。
_NO_ID_TABLES = {"agent_skills", "agent_profiles"}
_INSERT_TABLE_RE = re.compile(r"^\s*INSERT\s+(?:OR\s+(?:IGNORE|REPLACE)\s+)?INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)

# seed SQL 里的 sqlite `INSERT OR IGNORE` / `INSERT OR REPLACE` → PG 语义等价改写。
# PG 无 `INSERT OR ...` 语法。探针 seed 用它做「幂等造数」（冲突则忽略），语义 = ON CONFLICT DO NOTHING。
#   - INSERT OR IGNORE  → INSERT ... ON CONFLICT DO NOTHING（冲突整行忽略，与 sqlite 一致）
#   - INSERT OR REPLACE → 同上按「冲突忽略」处理：探针 seed 的 REPLACE 都是「已存在就别重复插」造数意图，
#     无「用新值覆盖」诉求；bare ON CONFLICT DO NOTHING 无需指定冲突列（PG 允许省略 target）。
# 业务代码运行期零调用本模块、且 upsert 一律走 models.dialect.insert_or_ignore/upsert，
# 故此改写只作用于探针 seed，不可能触及任何生产写路径。
_INSERT_OR_RE = re.compile(r"^(\s*INSERT)\s+OR\s+(?:IGNORE|REPLACE)\s+(INTO\b)", re.IGNORECASE)


def _translate_insert_or(sql: str) -> tuple[str, bool]:
    """`INSERT OR IGNORE/REPLACE INTO …` → `INSERT INTO …`，返回 (改写后 SQL, 是否命中)。
    命中时调用方需在语句尾补 `ON CONFLICT DO NOTHING`（且跳过 RETURNING id——冲突时无行返回）。"""
    new_sql, n = _INSERT_OR_RE.subn(r"\1 \2", sql)
    return new_sql, bool(n)


def _asyncpg_dsn() -> str:
    """把运行期 db_url（postgresql+asyncpg://…）转成 asyncpg.connect 接受的 DSN（去掉 +asyncpg）。"""
    return load_settings().db_url.replace("+asyncpg", "", 1)


def _qmark_to_dollar(sql: str) -> str:
    """`?` 占位符 → asyncpg 的 `$1,$2,…`。seed SQL 无字符串字面量含 `?`，逐个替换即可。"""
    out, n = [], 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


# seed SQL 里内联的 sqlite `datetime('now'[, 修饰符]…)` → PG 归一化 to_char（秒级 UTC text，
# 与运行期 now_expr/now_offset 同格式）。修饰符可以是：
#   - 字符串字面量 '±N unit'（如 '-5 seconds' / '+1 hour'）→ + interval 'N unit'
#   - 占位符 ?（值形如 '-30 seconds'，运行时绑定）→ + (?)::interval，? 由后续 ?→$N 编号
# 覆盖探针 seed 的全部形式，含 datetime('now', ?) 与 datetime('now', ?, '+N seconds')。
# 只匹配到 `get_connection()` 适配器路径的 SQL；走 SQLAlchemy text() 的 seed 不经此，需在源改写。
_DATETIME_NOW_RE = re.compile(r"datetime\(\s*'now'\s*((?:,\s*(?:'[^']*'|\?)\s*)*)\)")


def _translate_sqlite_datetime(sql: str) -> str:
    def _repl(m):
        deltas = ""
        # 逐个修饰符：字面量 '…' 或 占位符 ?
        for tok in re.findall(r"'[^']*'|\?", m.group(1)):
            if tok == "?":
                # ?::text 强制按字符串绑定（asyncpg 对裸 ::interval 会要 timedelta 对象），
                # 再由 PG 把文本转 interval。
                deltas += " + (?::text)::interval"
            else:
                deltas += f" + interval {tok}"
        return f"to_char((now() AT TIME ZONE 'UTC'){deltas}, 'YYYY-MM-DD HH24:MI:SS')"
    return _DATETIME_NOW_RE.sub(_repl, sql)


class _PGCursor:
    """aiosqlite cursor 的最小替身：lastrowid + fetchone/fetchall。"""

    def __init__(self, lastrowid, rows):
        self.lastrowid = lastrowid
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class _PGConnection:
    """aiosqlite Connection 的最小替身，底层是一条 asyncpg 连接。"""

    def __init__(self, raw: asyncpg.Connection):
        self._raw = raw

    async def execute(self, sql: str, params: tuple = ()):
        # 先把 sqlite `INSERT OR IGNORE/REPLACE` 收敛为 `INSERT INTO`（命中则语句尾补 ON CONFLICT DO NOTHING）。
        sql2, is_insert_or = _translate_insert_or(sql)
        q = _qmark_to_dollar(_translate_sqlite_datetime(sql2))
        head = sql2.lstrip()[:6].upper()
        if head == "INSERT" and "RETURNING" not in sql2.upper():
            if is_insert_or:
                # 冲突忽略：无「取自增 id」诉求（探针 INSERT OR IGNORE 目标都是 _NO_ID_TABLES），
                # 且冲突时无行返回，故不补 RETURNING id，只补 ON CONFLICT DO NOTHING。
                await self._raw.execute(q + " ON CONFLICT DO NOTHING", *params)
                return _PGCursor(None, [])
            m = _INSERT_TABLE_RE.match(sql2)
            table = m.group(1).lower() if m else ""
            if table not in _NO_ID_TABLES:
                row = await self._raw.fetchrow(q + " RETURNING id", *params)
                return _PGCursor(row["id"] if row else None, [])
            await self._raw.execute(q, *params)
            return _PGCursor(None, [])
        if head == "SELECT":
            rows = await self._raw.fetch(q, *params)
            return _PGCursor(None, rows)
        # UPDATE / DELETE / 其它
        await self._raw.execute(q, *params)
        return _PGCursor(None, [])

    async def commit(self):
        # asyncpg 默认自动提交（每条语句独立事务），seed 无回滚需求，此处为空操作。
        return None

    async def close(self):
        await self._raw.close()


async def get_connection() -> _PGConnection:
    """获取一条 PG 连接（aiosqlite 式 API 适配器）。调用方负责关闭（try/finally: await db.close()）。"""
    raw = await asyncpg.connect(_asyncpg_dsn())
    return _PGConnection(raw)
