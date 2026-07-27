# S1 执行方案（连接收口 + PRAGMA 调优）

> 状态：**方案定稿，待执行**（等用户确认 `busy_timeout` 默认值后开工）。
> 前置：S0 三份基准已交付（`baseline_schema.sql` / `baseline_regression.md` / `sql_surface_checklist.md`）。
> 边界：S1 **零业务行为变更**——只动连接入口与旁路收口，不碰任何业务 SQL、不碰方言（`datetime('now')`/`AUTOINCREMENT` 留给 S3）、不改 schema、不加列、不引新依赖。
> WAL 机制说明见同目录 `design.md` 决策 6。

## 目的

1. 给 `get_connection()` 补 `WAL + busy_timeout`，消除多 Agent 并发写的 `database is locked` 偶发失败。
2. 把 9 处旁路 `aiosqlite.connect` 收口到统一入口，使全库连接行为单点可控——为 S2 迁移框架 / S4 driver 抽象把「改 130 处」降到「改 1 处」。

---

## 核实到的现状（代码已确认，行号零漂移 @ 2026-07-24 master）

| 事实 | 位置 | 对 S1 的含义 |
|---|---|---|
| `get_connection()` 只开 `foreign_keys`，无 WAL/busy_timeout | `database.py:321-326` | S1.1 改造点 |
| 107 处调用**全是**裸调用 `db = await get_connection()` + `try/finally: await db.close()` | collab.py 等全库 | 关闭模式已统一；S1.1 加 PRAGMA 后天然生效，无需改调用方 |
| 9 处旁路用 `async with aiosqlite.connect(...) as db:`（上下文管理器自动关闭） | 见 checklist B1 | ⚠️ **关闭语义与 get_connection 不同**，收口须改成 try/finally |
| 旁路里 4 处显式 `db.row_factory = aiosqlite.Row`，5 处未设 | routes/auth.py:23/53、auth.py:58 等 | `get_connection()` 已内建 row_factory：收口后冗余行可删；未设的处会「获得」Row，需确认下标取值不破坏 |
| `Settings` 是 pydantic BaseSettings，env 覆盖模式统一 | `config.py:40` | busy_timeout 配置字段照现有模式加 |

**核心技术点**：旁路是 `async with connect()`（块结束自动关），`get_connection()` 返回裸连接、「调用方负责关闭」。收口不是换函数名，而是：

```python
# 旧（旁路）
async with aiosqlite.connect(get_db_path()) as db:
    ...
# 新（收口，与现网 107 处对齐）
db = await get_connection()
try:
    ...
finally:
    await db.close()
```

---

## 待用户拍板

- **`db_busy_timeout_ms` 默认值**：建议 **5000（5 秒）**——足够扛 WAL 下短暂写锁竞争，又不会在真死锁时无限挂起。全新配置项，不自作主张，等用户确认后写入 `config.py`。

---

## 逐任务施工清单

### S1.1 — 改造 `get_connection()`（database.py:321-326）

在现有 `foreign_keys = ON` 基础上补两条 PRAGMA（值走 config，不硬编码）：

```python
async def get_connection() -> aiosqlite.Connection:
    db = await aiosqlite.connect(get_db_path())
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute(f"PRAGMA busy_timeout = {load_settings().db_busy_timeout_ms}")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    return db
```

`config.py` 加一行（照现有 env 覆盖模式）：

```python
db_busy_timeout_ms: int = int(os.environ.get("AKIVILI_DB_BUSY_TIMEOUT_MS", "5000"))
```

### S1.2 — routes/auth.py 3 处（:22 login、:40 logout、:52 me）
改走 `get_connection()` + try/finally；删 :23、:53 冗余 `db.row_factory`（工厂已内建）。

### S1.3 — auth.py 2 处（:42 seed_admin、:57 _user_from_token）
同上；删 :58 冗余 row_factory。

### S1.4 — skills.py 2 处（:111 rescan、:132 count_skills）
收口 + try/finally；count_skills 用 `row[0]` 下标取值，Row 兼容下标，行为不变。

### S1.5 — agents.py 2 处（:95 rescan、:122 count_templates）
同 skills.py，`row[0]` 下标取值，Row 兼容。

### S1.6 — database.py 2 处 connect 审查
- `:226 init_db()` — 建库路径**保留**裸 connect；**并加 `PRAGMA journal_mode = WAL`**，让新建库从第一次即 WAL 模式，消除「首个 get_connection 才切 WAL」的时序空窗（见 design.md 决策 6）。
- `:321 get_connection()` — 即 S1.1 改造的工厂本体，收口目标出口，非待消除旁路。

### S1.7 — 连接生命周期审查
- 已确认 107 处调用方 + 收口后 9 处均为 `try/finally: await db.close()`，全库关闭语义一致。
- WAL 下 `-wal`/`-shm` 随主库常驻属正常；审查确认无「开了不关」路径。

---

## S1.V 验收门

1. 全库 `grep aiosqlite.connect` 仅剩 `database.py` 2 处（init + 工厂）→ 对照 `sql_surface_checklist.md` B1 从 9 → **0**。
2. 启动后 `backend/` 下出现 `jianagency.db-wal` / `jianagency.db-shm`。
3. 并发写压测不再 `database is locked`。
4. **回归 235/235 全绿**（主套件 31/31 + 22 probe 204/204，数字与基线完全一致，证明行为零变更）。
5. 通过 → 提交，**回滚锚点 A**。

**重启红线**：验收项 2/3 需重启 8100 生效。严格遵守 `backend-restart-single-instance`——**停下等用户授权**，杀净所有 8100 监听进程 + 确认端口空闲后再起，绝不擅自重启。
