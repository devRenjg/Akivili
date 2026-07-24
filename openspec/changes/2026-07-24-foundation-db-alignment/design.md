# Design — 数据底座对标 Multica

> 目标：把 Akivili 的手工 SQLite 数据层抬到 Multica 的工程化水位——版本化迁移 + 类型安全访问层 + PostgreSQL 引擎——并保持单进程架构不变。本文记录选型、方言差异与阶段边界，不含代码。

## 对标映射：Multica → Akivili

| 底层能力 | Multica | Akivili 现状 | 本 change 落点 |
|---|---|---|---|
| 连接管理 | pgxpool 池化 | `get_connection()` 中心入口（123 处），11 处旁路，无 WAL/busy_timeout | S1 收口 + PRAGMA；S3 engine 层池化 |
| Schema 版本化 | 编号迁移 001→221，up/down 成对，启动 apply | 41 处 `CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 兜底，无版本 | S2 Alembic 编号迁移 + 001 基线 |
| SQL 访问层 | sqlc（.sql → 类型安全 Go） | 398 处手写字符串 SQL，散 15 文件 | S3 SQLAlchemy 2.0 ORM 收敛 |
| 引擎 | PostgreSQL | SQLite 3.49.1 | S4 双跑 + S5 切默认 |

## 决策 1：ORM 选 SQLAlchemy 2.0（async），不选 SQLModel / 裸重写

- **sqlc 的等价物**：sqlc 的价值是「SQL 定义 → 类型安全代码 + 编译期校验」。Python 无编译期，最接近的工程化替代是 SQLAlchemy 2.0 的 typed ORM（`Mapped[]` 注解 + mypy 检查）。
- **为何不用 SQLModel**：SQLModel 是 SQLAlchemy + Pydantic 的薄封装，成熟度与 async 支持不如直接用 SQLAlchemy 2.0 Core/ORM；对标「工程化底座」应用最稳的一层。
- **为何不裸重写数据访问类**：用户已明确「ORM 也对标」，且裸重写等于自己造半个 ORM，维护成本更高。
- **双引擎要求**：SQLAlchemy 原生支持 SQLite（`aiosqlite` driver）与 PostgreSQL（`asyncpg` driver）同码切换——这是 S4 双跑的技术前提，也是选它的决定性理由。

## 决策 2：迁移框架选 Alembic

- Alembic 是 SQLAlchemy 官方迁移工具，与决策 1 天然配套：可从 ORM 模型 autogenerate 迁移，也可手写。
- **对标编号迁移**：Alembic 的 revision 链 = Multica 的 001→221 编号链；`alembic_version` 表 = Multica 迁移已应用记录；`upgrade head` = 启动自动 apply。
- **001 基线策略**：不追求「把历史 41 处兜底拆成 N 个迁移」，而是把**当前实际 schema 快照**（S0.1 导出）固化成单个 `001_baseline`。历史怎么长出来的不重要，重要的是「从今天起 schema 由迁移唯一定义」。存量库用 `alembic stamp 001` 标记已应用，不重跑建表。

## 决策 3：SQLite / PostgreSQL 方言差异清单（S3/S4 收敛目标）

本清单是 S3「方言隔离」与 S4「双跑修正」的 checklist。收敛后这些差异只存在于 ORM/helper 一层：

| 差异点 | SQLite | PostgreSQL | ORM 层如何抹平 |
|---|---|---|---|
| 当前时间 | `datetime('now')`（41 处） | `now()` / `CURRENT_TIMESTAMP` | ORM `func.now()` / 统一 helper，业务层不写字面量 |
| 自增主键 | `INTEGER ... AUTOINCREMENT`（17 处） | `SERIAL` / `IDENTITY` | ORM `Mapped[int] = mapped_column(primary_key=True)`，由方言生成 |
| 占位符 | `?` 位置参数 | `$1` / `:name` 命名 | ORM 参数绑定，不手写占位符 |
| upsert | `INSERT OR IGNORE`（1 处） | `ON CONFLICT DO NOTHING` | SQLAlchemy `insert().on_conflict_*` 方言分支 |
| 布尔 | 0/1 整数 | 原生 `boolean` | ORM `Mapped[bool]`，driver 转换 |
| 时间存储 | TEXT（UTC 字符串） | `TIMESTAMPTZ` | ORM `DateTime(timezone=True)`，统一 UTC 语义 |
| 并发控制 | 单写者 + WAL + busy_timeout | 行锁 + `SKIP LOCKED`/`FOR UPDATE` | **本 change 不依赖 SKIP LOCKED**（见决策 5） |
| 大小写/引号 | 宽松 | 标识符大小写敏感 | ORM 统一小写命名，不裸拼标识符 |

## 决策 4：阶段边界——本 change 只做底座，不做执行协议

明确切割，防止范围蔓延到三个被阻塞 change：

- **做**：连接调优、Schema 版本化、SQL→ORM、引擎迁移。这些是「怎么存、怎么访问」。
- **不做**：原子 CAS claim 强化、partial unique index 业务约束、`run_queue`/`task_runs` 加协议列、drain/resume/fencing/containment、多 worker、SSE 续传。这些是「执行协议」，属于 [platform-graceful-restart] / [agent-session-resume] / [platform-concurrency-scaling]。
- **交接契约**：本 change 完成 S1-S2 后，上述三 change 的**所有 schema 变更改用 Alembic 迁移编写**（不再 `PRAGMA table_info` 兜底），所有新查询用 ORM。本 change 全部完成（S5）后，三 change 才启动编码。

## 决策 5：为何单进程下不需要 PostgreSQL 的并发原语

- Multica 用 PG 的 `SKIP LOCKED`/`FOR UPDATE`，是因为它是**多进程（daemon + server）+ 多 runtime 抢单**。Akivili 是**单进程 asyncio**，并发是协程不是多进程，写并发本就串行。
- 因此迁 PG 的动机**不是**当下的并发原语需求，而是**与 Multica 底层一致 + 为未来并发扩展铺路**（用户诉求）。当前收益主要来自 S2（版本化）+ S3（收敛），S4/S5（引擎）是面向未来的对齐投资。
- 一旦未来做多 worker / 拆 daemon（[platform-concurrency-scaling]），PG 的 `SKIP LOCKED` 立刻用得上，届时底座已就位，无需二次大改。

## 风险与回滚

- **S3 是最大工程量**（398 处），风险最高。缓解：按表分批（S3.4a-j），每批独立提交 + 独立回归，任一批出问题只回退该批。
- **S4 双跑期两引擎行为差异**（时间时区、布尔、大小写）是经典坑。缓解：S4.6 一致性校验脚本逐表比对，不靠肉眼。
- **回滚锚点**：每个 `S*.V` 是一个稳定提交。S5 出问题回 D（双跑态，SQLite 仍默认）；S4 回 C（纯 ORM + SQLite）；S3 回 B（迁移框架 + 手写 SQL）；S2 回 A（收口 + 调优）；S1 回基线。
- **重启红线**：S1/S4/S5 涉及重启验证，严格遵守 `backend-restart-single-instance`——杀净 8100 监听、确认端口空闲、改代码前不擅自重启。
