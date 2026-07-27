# S2 执行方案（迁移框架 Alembic · Schema 版本化，结构不变）

> 状态：**方案定稿，待执行**（S1 已合入 master + 回滚锚点 A 就位）。
> 边界：S2 **结构零改动**——把当前 schema 原样固化成 `001_baseline`，只搬家不改表；不写 ORM 模型、不用 SQLAlchemy 做业务查询（那是 S3）。
> 关键决策见 `design.md` 决策 2（依赖边界 / 迁移 driver / 001 基线 / stamp）。

## 目的

给数据层装上「版本化、可重放、可回滚」的 schema 管理：
- 建 Alembic 编号迁移体系 + `alembic_version` 版本表 + 启动自动 `upgrade head`。
- 把现有手工建表（`database.py` 的 `SCHEMA` + `_migrate` 补列）**原样**固化成单个 `001_baseline`。
- 空库能从 001 可重放重建出当前结构；存量库 `stamp 001` 标记已应用、不重跑、不丢数据。
- 为 S3（ORM）/ S4-S5（PG）以及被阻塞的三个 change（平滑重启等）提供「schema 只由迁移定义」的地基。

---

## 核实到的现状（代码已确认 @ 2026-07-27 master）

| 事实 | 位置 | 对 S2 的含义 |
|---|---|---|
| 建表用 `SCHEMA` 常量 + `executescript`（18 张 `CREATE TABLE IF NOT EXISTS`） | `database.py:19` SCHEMA / `init_db:228` | 001 基线的表结构来源 |
| 旧库补列走 `_migrate()`：`PRAGMA table_info` 探测 + `ALTER ADD COLUMN` | `database.py:233` | S2.2 要"冻结"的隐式演进逻辑 |
| **`baseline_schema.sql`（S0.1 快照）已含全部补列的最终形态** | change 目录 | ✅ 001 直接照它写即可，无需重演 `_migrate` 的历史 `if not in cols` |
| 实测 18 表、0 显式索引（隐式 PK/UNIQUE 索引 sql 为 NULL） | S0.1 已核 | 001 = 18 张 CREATE TABLE，无 CREATE INDEX |
| requirements.txt 仅 5 项，无 alembic/sqlalchemy | `requirements.txt` | S2.1 新增 alembic（连带 sqlalchemy Core） |
| 后端运行用 py 3.12（进程 34188），另有 3.14 | — | alembic 装进 3.12 环境（后端实际解释器） |
| `init_db()` 已在 startup 调用，早于 `reclaim_orphan_runs`（main.py:96） | `main.py` | S2.4 的 `upgrade head` 插在此处、早于 init_db |

## 已定决策（design 决策 2）

1. **依赖边界**：装 `alembic`（连带 `sqlalchemy` Core），S2 只用它跑迁移 DDL，**不写 ORM、不做业务查询**。
2. **迁移 driver**：同步 `sqlite3`(pysqlite)，与运行期 `aiosqlite` 解耦；迁移连接也置 `PRAGMA journal_mode=WAL`。
3. **001 基线**：照 `baseline_schema.sql` 原样固化，18 表逐字段对齐，不改结构。
4. **存量库**：`alembic stamp 001` 标记已应用（不跑建表 DDL）；空库才真跑 001。
5. **存量库处理前先备份**（用户拍板）：`jianagency.db.bak_pre_s2_<YYYYMMDD>`（gitignore `*.db.bak*` 已忽略），多一层数据兜底。

---

## 逐任务施工清单

### S2.1 — 引入 alembic + 初始化迁移环境
- `requirements.txt` 新增 `alembic`（显式 pin 版本；`sqlalchemy` 作传递依赖，可一并 pin）。装进后端 py 3.12 环境。
- `alembic init backend/migrations` 生成环境骨架。
- 配置 `alembic.ini` / `migrations/env.py`：DB URL 从 `config.py` 的 `db_path` 读取（不硬编码），driver 用同步 `sqlite:///`（pysqlite）；`env.py` 迁移连接执行 `PRAGMA journal_mode=WAL`，与运行期一致。
- 不引入 ORM `target_metadata`（S2 无模型，autogenerate 不用；001 手写）。

### S2.2 — 冻结 database.py 的隐式演进（只标记，不切换）
- 在 `_migrate()` / `SCHEMA` 处加注释标记「schema 自 001 起由 Alembic 唯一定义，此处补列逻辑 S2.4 接入后冻结」。
- **本任务只标记 + 规划**，实际停用 `init_db`/`_migrate` 的建表责任在 S2.4 验证通过后（避免中途空窗）。

### S2.3 — 编写 001_baseline 迁移
- 照 `baseline_schema.sql` 把 18 张 `CREATE TABLE`（含所有补列的最终形态）逐字段写进 `001_baseline` 的 `upgrade()`。
- `downgrade()` 写对应的 `DROP TABLE`（逆序，尊重外键依赖）。
- 保持 SQLite 方言原样（`AUTOINCREMENT`/`datetime('now')` 不动——方言收敛是 S3）。

### S2.4 — 启动接入 alembic upgrade head
- `main.py` startup：在 `init_db()`/`reclaim_orphan_runs()` **之前**执行 `alembic upgrade head`（编程式 `command.upgrade` 或等价 API）。
- 接入后 `init_db` 的建表责任移交迁移；保留 `init_db` 里非建表的必要初始化（若有）。

### S2.5 — 空库可重放验证
- 全新空库路径启动 → 自动 apply 001 → 用 S0.1 同法导出结构 → 与 `baseline_schema.sql` **逐字段 diff 一致**（18 表、列、默认值、约束全对齐）。

### S2.6 — 存量库幂等验证
- **先备份** `jianagency.db.bak_pre_s2_<YYYYMMDD>`（gitignore 忽略）。
- 对现网 `jianagency.db` 执行 `alembic stamp 001`（只写 `alembic_version`，不跑 DDL）。
- 重启 → `upgrade head` 见 001 已应用 → 不重复建表、不丢数据（表数仍 18、projects/tasks/task_runs 行数不变）。

### S2.7 — 回滚验证
- `alembic downgrade base`（在**临时副本库**上，不碰真实库）验证 001 的 `down` 可用、能干净 DROP 全部表；再 `upgrade head` 重建。仅验证 up/down 双向可用，不用于生产数据。

---

## S2.V 验收门

1. 空库重建结构 == `baseline_schema.sql` 逐字段一致（S2.5）。
2. 存量库 `stamp 001` + 重启幂等无损：表数 18、关键表行数不变（S2.6）。
3. `alembic upgrade`/`downgrade` 双向可用（临时库验证，S2.7）。
4. **回归 243/243 全绿**（主套件 31/31 + 23 隔离 probe，含 S1 新增 `run_wal_concurrency_probe` 8/8；行为零变更）。
5. 通过 → 提交，**回滚锚点 B**。

**重启红线**：S2.4/S2.6 需重启 8100 验证 `upgrade head`。严格遵守 `backend-restart-single-instance`——停下等用户授权，杀净所有 8100 监听进程（含可能的脱管 CLI 子进程）+ 确认端口空闲后再起。

**新增回归项建议**：S2 可加 `run_migration_probe.py`（隔离临时库验证：空库 upgrade→结构对齐基线、stamp 幂等、up/down 双向），纳入基线清单，给 S3+ 每次动 schema 提供护栏。执行时与用户确认。
