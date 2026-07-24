# 数据底座对标 Multica：ORM + 迁移框架 + PostgreSQL

## Why

Akivili 后端的数据访问层是「手工作坊」形态，与 Multica 的工程化底座差距是**结构性的**，且随功能增长只会越来越重：

- **无 Schema 版本管理**：`database.py` 用 41 处 `CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 探测后 `ALTER` 的方式兜底演进，没有版本号、无法回滚、无法在空库可重放地重建当前结构。每加一列都是往这坨兜底逻辑里再塞一段 `if 'col' not in cols`。
- **SQL 手写散落**：398 处手写字符串 SQL 分散在 15 个文件，无类型、无集中校验；41 处 `datetime('now')`、17 处 `AUTOINCREMENT` 等 SQLite 方言硬编码进业务代码，锁死引擎。
- **连接未池化、未调优**：`get_connection()` 虽是中心入口（123 处在用），但仍有 11 处旁路各自 `aiosqlite.connect`；且**全库只开了 `foreign_keys`，未开 WAL、未设 busy_timeout**——并发写随时 `database is locked`。
- **引擎与 Multica 不一致**：Multica 用 PostgreSQL + sqlc（.sql 生成类型安全代码）+ 编号迁移（001→221，up/down 成对，启动自动 apply）。Akivili 用裸 SQLite。未来要做多 worker 并发执行 / 拆 daemon 双进程时，SQLite 单写者与手工 schema 会成为硬瓶颈。

**用户诉求（已确认）**：前置底座与 Multica 保持一致，否则「越拓展越重」。**全序列对齐**——引入 ORM（对标 sqlc 的类型安全访问层）、迁移框架（对标编号迁移）、迁移到 PostgreSQL（对标引擎）。**在本底座对齐完成前，不推进平滑重启 / session resume 等进阶功能**（[platform-graceful-restart] / [agent-session-resume] / [platform-concurrency-scaling] 全部等待本 change 落地）。

**根因**：数据访问层的可演进性 = 平台的可演进性。执行协议、平滑重启、并发扩展全都写在数据层之上；数据层不工程化，上层每个 change 都要在手工 SQL 里打补丁。先把底座抬到 Multica 的水位，后续 change 才是「加功能」而非「和历史债搏斗」。

## What Changes

分五个阶段（S1→S5），下层不稳上层白做，顺序刚性。每阶段独立可回归、可回滚、可验收。

- **S1 连接收口 + PRAGMA 调优**：11 处旁路 `aiosqlite.connect` 全部改走统一入口；入口开启 `WAL + busy_timeout + foreign_keys`。零业务行为变更，纯并发正确性与收口。**这是后续所有步骤的前提**（不收口，迁 PG 要改 130 处而非 1 处）。
- **S2 引入迁移框架（Alembic）**：建 `migrations/` 编号体系 + `alembic_version` 版本表 + 启动自动 apply；把现有 41 处手工建表**原样固化成 001 基线迁移**（不改结构，只搬家）。空库能从 001 可重放重建出当前结构；二次启动幂等。
- **S3 引入 ORM（SQLAlchemy 2.0 async）+ SQL 收敛**：定义 ORM 模型层（对标 sqlc 的类型安全）；把 398 处手写 SQL 按表归拢到 ORM 查询；把 41 处 `datetime('now')` 等方言收敛到统一 helper / ORM 表达式。**方言差异（`datetime('now')`→`now()`、`AUTOINCREMENT`→序列、占位符 `?`→`:name`）从此只在一层，不是 398 处。**
- **S4 引擎抽象 + 双跑（SQLite ⇄ PostgreSQL）**：接入 `asyncpg` driver；S2 的迁移在 PG 上重放建库；写一次性数据搬迁脚本 + 一致性校验。同一套 ORM 代码 SQLite / PG 都能起。
- **S5 切 PG 为默认**：默认连 PostgreSQL，SQLite 降级为可选（本地轻量开发）。单进程架构不变。PG 跑通全回归后，更新 README + OpenSpec，底座对齐完成。

**明确不做**（本 change 边界）：不拆 daemon 双进程、不做多 worker 并发、不碰平滑重启 / session resume / SSE 续传 / 进程 containment。这些都在稳定的新底座上作为后续独立 change 推进。

## Capabilities

### New Capabilities
- `foundation-data-layer`: 平台数据底座——池化并调优的统一连接入口、版本化可回滚的 Schema 迁移（Alembic 编号迁移 + 启动自动 apply）、类型安全的 ORM 访问层（SQLAlchemy 2.0 async，收敛全部手写 SQL 与引擎方言）、双引擎抽象（SQLite / PostgreSQL 同码运行）与 PostgreSQL 默认化。综合达成：**Schema 有版本可重放可回滚；SQL 集中类型安全；引擎方言单点隔离；与 Multica 底层一致，为后续执行协议 / 平滑重启 / 并发扩展提供工程化地基。**

## Impact

- **本 change 涉及大范围机械改造，分阶段落地、每阶段独立验收后再进下一阶段。** 落实时预计涉及：
  - `backend/requirements.txt`：新增 `sqlalchemy[asyncio]`、`alembic`、`asyncpg`（S3/S4）。
  - `backend/database.py`：连接入口统一 + PRAGMA（S1）；引擎方言收敛（S3）；driver 抽象（S4）。
  - 11 处旁路连接：`routes/auth.py`(3)、`skills.py`(2)、`auth.py`(2)、`agents.py`(2)、`database.py`(2) 改走统一入口（S1）。
  - 新增 `backend/migrations/`（Alembic 环境 + 001 基线 + 后续迁移，S2）。
  - 新增 `backend/models/`（SQLAlchemy ORM 模型层，S3）。
  - 398 处手写 SQL（`collab.py` 36 / `runs.py` 28 / `progress.py` 21 / `tasks.py` 20 / `runner.py` 17 / `agents.py` 16 / … 共 15 文件）按表迁移到 ORM（S3，分批）。
  - 新增数据搬迁脚本 + SQLite→PG 一致性校验脚本（S4）。
  - `config.py`：数据库 URL / 引擎选择配置（S4/S5）。
  - `start.ps1` / README：PG 启动前置说明（S5）。
- **兼容性**：S1/S2 零行为变更（收口 + schema 固化）。S3 是等价替换（ORM 产出与手写 SQL 语义一致，靠回归保证）。S4 双跑期两引擎并存可回退。S5 切默认后 SQLite 仍可用作降级。**任一阶段可独立回滚到上一阶段的稳定态。**
- **阻塞关系（关键）**：本 change 是 [platform-graceful-restart]、[agent-session-resume]、[platform-concurrency-scaling] 的**共同前置**。三者规划的 WAL、原子 CAS claim、partial unique index、`run_queue`/`task_runs` 加列等，全部应在本 change 建立的迁移框架 + ORM 层之上实现，不再走手工 `PRAGMA table_info` 兜底。**顺序：本 change 完成 S1-S2 后，上述三 change 的 schema 变更即改用 Alembic 迁移编写；本 change 全部完成后再启动上述三 change 的编码。**
- **与 [platform-concurrency-scaling] 的 PG 判断一致**：该 change 已提出「PostgreSQL 早迁移、WAL 作桥」；本 change 的 S1(WAL) 与其阶段 0.1(WAL) 是同一件事，本 change 先做即满足；S4/S5 的 PG 迁移正是其 PG 判断的落地。
- **关联能力**：所有 specs 的持久化都落在本能力之上；直接相关 [agent-execution]、[agent-collaboration]、[task-system]（run_queue / task_runs / messages 的读写）。
- **关联记忆**：`backend-restart-single-instance`（重启前须杀净 8100 监听）——S1 收口 + S4/S5 引擎切换涉及重启，须严格遵守该流程；改代码前不擅自重启后端。
- **平台事实**：后端运行于 `py -3.12` / SQLite 3.49.1（支持 `UPDATE...RETURNING`、partial unique index，无 `SKIP LOCKED`/`FOR UPDATE`）。ORM 选型须兼容 SQLite 3.49 与目标 PostgreSQL 双引擎。
