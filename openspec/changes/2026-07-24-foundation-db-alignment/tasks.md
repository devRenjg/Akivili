# Tasks

> 执行原则：阶段刚性顺序 S1→S5，**每阶段的验收门（`V` 任务）全绿才进下一阶段**。每阶段结束是一个可回滚锚点（独立提交 / 独立分支合入）。任务粒度 = 一次可独立提交、可单独回归的改动。
> 追溯：每条任务勾选时在提交信息引用任务号（如 `S1.2`）。回滚：回退到上一阶段最后一个 `V` 任务对应的提交即可。

## S0. 准备与基线锚点

> 详细施工书见同目录 `s0-plan.md`（命令、产出、验收逐条钉死）。S0 全程零代码 / 零 schema / 零重启。

- [x] S0.1 记录当前基线：`jianagency.db` 复制为 `jianagency.db.bak_baseline-20260724`（已核对匹配 gitignore `*.db.bak*`，不入仓）；用 `py -3.12` 遍历 `sqlite_master` 导出全表结构快照到 `openspec/changes/2026-07-24-foundation-db-alignment/baseline_schema.sql`（纳入追踪）作为 S2 固化的比对基准。**实测 18 表（== database.py 的 18 处 CREATE TABLE，无漏表；s0-plan 估算的 ~41 偏高）、0 索引（代码无 CREATE INDEX，隐式 PK/UNIQUE 自动索引 sql 为 NULL 已正确排除）；补列列齐（projects/tasks/run_queue/messages/run_logs 抽查全 OK）；回归期间真实库 mtime 未变，零污染**
- [x] S0.2 建立回归基准：入口 = `TestReport/run_qa_suite.py`（主套件 **31/31**）+ 22 个隔离 probe（临时 DB 隔离、不碰真实库）。已跑通并记录每脚本实测 `N/N` 到 change 目录 `baseline_regression.md`。**隔离 probe 合计 204/204，总计 235/235 全绿零红项**；`*` 标记的 `run_collab_scenario.py` / `run_codex_cli_smoke.py`（需真实 CLI 供应商）不纳入基线
- [x] S0.3 统计并登记 SQL 访问面清单到 change 目录 `sql_surface_checklist.md`，每项带 `file:line`。**Grep 复核订正：调用点 254 / 19 文件（s0-plan 的 398/15 偏高且漏登 4 文件，以清单为准）；旁路 aiosqlite.connect 11（9 待收口 + 2 豁免：init_db 与 get_connection 本体）；datetime('now') 41（含 1 处 timeutil docstring，实际代码站点 40）；AUTOINCREMENT 17（全在 database.py）——三类核心数与估算一致**

## S1. 连接收口 + PRAGMA 调优（零行为变更）

> 详细施工书见同目录 `s1-plan.md`（逐处行号、关闭语义差异、验收门钉死）。WAL 机制与跨引擎适用性见 `design.md` 决策 6。`db_busy_timeout_ms` 默认值 = 5000ms（用户 2026-07-27 拍板）。

- [x] S1.1 `get_connection()` 统一开启 `PRAGMA journal_mode=WAL`、`PRAGMA busy_timeout=<配置>`、`PRAGMA foreign_keys=ON`（busy_timeout 值走 `config.py`，不硬编码）。**实测 get_connection 返回连接 journal_mode=wal / busy_timeout=5000 / foreign_keys=1；config.py 加 `db_busy_timeout_ms` 默认5000（env AKIVILI_DB_BUSY_TIMEOUT_MS 可覆盖）**
- [x] S1.2 `routes/auth.py` 3 处旁路 `aiosqlite.connect` 改走 `get_connection()`（login/logout/me；删 2 处冗余 row_factory；改 `async with`→`try/finally: await db.close()`；import 去 aiosqlite/get_db_path 换 get_connection）
- [x] S1.3 `auth.py` 2 处旁路改走 `get_connection()`（seed_admin/_user_from_token；删 1 处冗余 row_factory）
- [x] S1.4 `skills.py` 2 处旁路改走 `get_connection()`（rescan/count_skills；row[0] 下标取值 Row 兼容）
- [x] S1.5 `agents.py` 2 处旁路改走 `get_connection()`（rescan/count_templates；row[0] 下标取值 Row 兼容）
- [x] S1.6 `database.py` 内 2 处直接 `connect`（init 建库除外）审查：init 路径保留**并加 `PRAGMA journal_mode=WAL`**（新建库即 WAL 模式，消除时序空窗）、`get_connection()` 工厂为收口出口非旁路
- [x] S1.7 连接生命周期审查：确认 WAL 下连接复用/关闭策略一致，无连接泄漏。**全库 116 个 `= await get_connection()` 调用点均后跟 `try:`，连接均 close（collab.py 4 处用别名 db0/db2/db3/dbf close，为既有代码非本次改动）；无「开了不关」路径**
- [x] **S1.V 验收**：全库源码 `aiosqlite.connect` 仅剩 `database.py` 2 处（init 建库 + 工厂本体），9 处旁路清零（checklist B1 9→0）；重启后 `get_connection` 实测 journal_mode=wal（`-wal`/`-shm` 仅活跃连接期存在，连接干净关闭后 checkpoint 清理——属正常）；并发写压测 **12 写者×40轮=480 写全成功、零 database-is-locked**（1.78s）；服务自检 divisions/projects/settings/前端根路径 200 + login 401（收口无破坏）；**回归 235/235 全绿**（主套件 31/31 + 22 probe 204/204，每项 N/N 与基线逐一吻合，行为零变更）→ **提交，回滚锚点 A**

## S2. 迁移框架 Alembic（Schema 版本化，结构不变）

> 详细施工书见同目录 `s2-plan.md`。关键决策见 `design.md` 决策 2：依赖边界（装 alembic 连带 sqlalchemy Core，S2 只跑迁移不写 ORM）/ 迁移用同步 sqlite3 driver / 001 照 baseline_schema.sql 固化 / 存量库先备份再 stamp。

- [x] S2.1 引入 `alembic` 依赖（连带 `sqlalchemy` Core 传递装入，S2 不写 ORM）；`alembic init backend/migrations`，env.py 用**同步 sqlite3** driver + 从 `config.py` 读 DB URL + 迁移连接置 WAL。**实测装 alembic==1.18.5 + SQLAlchemy==2.0.51(传递依赖)；env.py 加 ALEMBIC_DB_PATH 隔离逃生口(供测试/S2.5/S2.7 隔离，config 无 env_prefix 不认 AKIVILI_DB_PATH)；WAL 用独立 AUTOCOMMIT 连接置（避免扰乱版本 stamp 事务）**
- [x] S2.2 关闭 `database.py` 里 `PRAGMA table_info` 兜底加列逻辑的「隐式演进」——改为「schema 只由迁移定义」（本任务只标记与规划，实际切换在 S2.4 后）。**SCHEMA 常量 + _migrate() 加 🔒 冻结标记注释，逻辑未改（存量库迁移前仍靠它）**
- [x] S2.3 编写 `001_baseline` 迁移：把 S0.1 的 `baseline_schema.sql` **原样**转成 Alembic 迁移（表/列/索引逐一对齐，不改任何结构）。**op.execute 逐条嵌入18表原始CREATE(仅去IF NOT EXISTS，含补列最终形态+列注释)，不用op.create_table避免重排；空库重建与baseline逐字节diff一致；SQLite方言原样保留(方言收敛留S3)**
- [x] S2.4 启动流程接入 `alembic upgrade head`（`main.py` startup 内，早于 `reclaim_orphan_runs`）。**新增 db_migrate.py 编程式接入，三状态健壮处理：空库upgrade/已纳管noop/存量库未纳管自动stamp；main.py startup 用 asyncio.to_thread 调用，早于 init_db；临时库验证三状态全对**
- [x] S2.5 空库可重放验证：全新空库启动 → 自动 apply 001 → 结构与 S0.1 快照逐字段一致。**空库 upgrade→18表逐字节==baseline、version=001、WAL生效、foreign_key_check零违规、二次upgrade幂等；固化为 run_migration_probe.py 常驻护栏**
- [x] S2.6 存量库幂等验证：现有 `jianagency.db` 启动 → 标记为 001 已应用（`alembic stamp`）→ 不重复建表、不丢数据。**先备份 jianagency.db.bak_pre_s2_20260727(38MB,gitignore忽略)；杀净8100进程树(无在跑Agent)+端口空闲后重启→日志 stamp_revision->001；真实库 version=001/18表/projects4·tasks221·task_runs635数据零丢/WAL生效；服务接口200·login401；二次run_migrations=noop幂等**
- [x] S2.7 回滚验证：`alembic downgrade` 能回退 001（验证 down 迁移可用，不实际用于生产数据）。**临时库 upgrade→downgrade base(干净DROP全18表+version清空)→再upgrade(重建18表)往返逐字节无损；固化进 run_migration_probe.py**
- [x] **S2.V 验收**：空库重建结构 == 基线快照；存量库启动幂等无损；up/down 双向可用；S0.2 回归全绿 → **提交，回滚锚点 B**。**空库重建逐字节==baseline✓；真实库stamp幂等无损(数据零丢)✓；up/down双向✓；回归258/258全绿(主套件31+隔离227，S2新增run_migration_probe 15/15)✓**

## S3. ORM（SQLAlchemy 2.0 async）+ SQL 收敛 + 方言隔离
- [ ] S3.1 引入 `sqlalchemy[asyncio]` 依赖；建 `backend/models/` 定义全部表的 ORM 模型（对齐 001 基线，逐表）
- [ ] S3.2 建统一 async session 工厂，桥接 S1 的连接调优（WAL/busy_timeout 在 engine 层配置）
- [ ] S3.3 方言 helper 收敛：`datetime('now')`（41 处）统一为 ORM 时间表达式 / 单一 helper；`AUTOINCREMENT`（17 处）改由 ORM 主键策略表达；占位符统一
- [x] S3.4 按文件分批迁移手写 SQL 到 ORM（每文件一提交 + 一隔离 probe，独立回归）。**数据访问层全部迁离手写 SQL；审计：业务/路由层零 `get_connection` 引用、零裸 SELECT/INSERT/UPDATE/DELETE 字面量**：
  - [x] S3.4 批1 `agent_memory_sync.py`（probe 9/9）
  - [x] S3.4 批2 `auth.py` / `projects.py` / `activity.py`（probe 23/23）
  - [x] S3.4 批3 `skills.py` / `routes/skills.py` / `routes/auth.py`（probe 18/18）
  - [x] S3.4 批4 `agents.py` / `routes/agent_config.py`（probe 15/15）
  - [x] S3.4 批5 `routes/agent_cli.py` / `routes/project_agents.py`（probe 24/24）
  - [x] S3.4 批6 `routes/agents.py` / `reflect.py`（probe 28/28）
  - [x] S3.4 批7 `progress.py`（probe 17/17，commit e319812）
  - [x] S3.4 批8 `routes/tasks.py`（probe 27/27，commit 5c05ce7）
  - [x] S3.4 批9 `routes/runs.py`（probe 27/27，commit 57c74de；新方言点 datetime('now',?) 窗口 + julianday SUM）
  - [x] S3.4 批10 `executor/runner.py`（probe 20/20，commit 25f85c0）
  - [x] S3.4 批11 `collab.py`（probe 32/32，commit fdbb744；_claim_one 优先级+FIFO+退避、两层 reclaim、julianday idle sweep）
  - [~] `database.py`（41）**改判为基础设施，非数据访问**：全部为 `init_db`(DDL/PRAGMA) / `_migrate`(ALTER/PRAGMA table_info) / `get_connection`(PRAGMA 工厂)，无一条数据查询，无法「迁成 ORM select」。归 S3.6 下线，不属 S3.4 迁移面。`models/engine.py` 3 处为 ORM 自身 PRAGMA 监听器，永不迁移。
- [x] S3.5 每批迁移后校验：每文件一隔离 probe，逐一对照读结果/写副作用与迁移前等价（含金样对照与 TestClient 真实接口）；每批跑全量回归（schema parity 604 + engine 8 + dialect 7 + batch1-11 + S1 8 + S2 15）+ E2E QA 31/31，全绿方提交
- [x] S3.6 下线 `init_db()` 建表职责（commit 1e9dd6c）：`database.py` 移除 SCHEMA 常量 + `init_db()` + `_migrate()`（~300 行，自 001 起即冗余），仅保留 `get_connection()` 连接工厂。原 `_migrate` 两条数据规整（planning→backlog / archived→done）落成 Alembic **002** 数据迁移（幂等、downgrade no-op）。`main._startup` 删 init_db 调用，`run_migrations()` 成唯一建表路径（env.py 已用 AUTOCOMMIT 连接置 WAL，S1 时序保证不回退）。QA bootstrap + 5 个直调 init_db 的 probe → 统一 `run_migrations`；`run_orm_engine`/`run_wal_concurrency` 建库入口改判 + wal「database.py 恰 1 个 connect 点」；`run_migration_probe` head 断言 001→002 + 新增 002 数据规整 3 条覆盖。见记忆 [[init-db-schema-stale-vs-alembic]]。方案见 s3.6-plan.md
- [x] **S3.V 验收**（回滚锚点 C）：
  - **手写 SQL 归零**：业务/路由/执行层零 `get_connection` 调用、零裸 SELECT/INSERT/UPDATE/DELETE 字面量、零 `session.execute(text(SQL))` 混入。残留仅 `database.py` 的 PRAGMA(连接工厂调优) + `migrations/`(建表 DDL) + `models/engine.py:101` ping 的 `SELECT 1`(健康检查)——均属基础设施，非数据查询。
  - **方言集中**：时间「现在」统一走 `now_expr()` helper（29 处调用，零裸 `datetime('now')` 运行时字面量）；相对时间窗口/julianday 时长聚合是 SQLite 固有方言点，集中在 `collab.py`(2) + `routes/runs.py`(3)，逐点标注「S4 迁 PG 改 EXTRACT EPOCH」，边界清晰。
  - **init_db 建表职责已下线**（S3.6）：建表唯一走 Alembic。
  - **回归全绿**：35 个隔离 probe 合计 **1059 [PASS] 零 [FAIL]**（schema parity 604 + engine 8 + dialect 7 + batch1-11 + migration 18 + wal 8 + 18 个遗留 bootstrap probe）+ E2E QA **31/31**。
  - → 提交回滚锚点 C，S3（ORM + SQL 收敛 + 方言隔离）完成。

## S4. 引擎抽象 + SQLite⇄PostgreSQL 双跑 ✅（回滚锚点 D，2026-07-28）
- [x] S4.1 引入 `asyncpg`（运行期异步）+ `psycopg[binary]`（迁移期同步，Alembic 用）；`config.py` 加 `db_url`（`AKIVILI_DB_URL`）+ `migration_db_url()` 单一构造；`engine.py` 按 URL 分支（PG 跳过 sqlite PRAGMA）。空 URL=默认 sqlite，逐字节不变
- [x] S4.2 Docker `postgres:16`（容器 `akivili-pg` / `localhost:5432` / 库·用户 `akivili` / 卷 `akivili-pg-data`）
- [x] S4.3 001 `upgrade()` 按 `dialect.name` 分支：sqlite 逐条原始 DDL（不变）；pg 走 `metadata.create_all`（IDENTITY + 归一化 now()）。`_NOW`→方言感知 `now_default_ddl()`。002 纯 UPDATE 不动
- [x] S4.4 5 处方言查询收敛到 `dialect.py` 方言感知元素（`now_expr`/`now_offset`/`elapsed_seconds`）；时间统一归一化为秒级 UTC text（两引擎逐字节同格式）；调用点 collab.py(2)+routes/runs.py(3) 改用新元素。PG 端到端 CRUD 冒烟通过
- [x] S4.5 `migrate_sqlite_to_pg.py`（只读源、依赖序、保留 id、序列重置）；处理 NUL 剔除 + 8 行悬空外键跳过 + id 不连续保留
- [x] S4.6 `run_pg_sqlite_consistency_probe.py` 逐表行数 + **逐行逐列全量比对**（37/37）；`run_pg_e2e_probe.py` 端到端全链路 + 4 方言查询（22/22）
- [x] S4.7 双跑回归：sqlite 侧 40 门禁 40/40（默认路径零回归）+ PG 侧端到端 22/22 + 一致性 37/37。注：40 门禁 27 个深度绑 sqlite-only seed（业务运行期已零 `get_connection`），PG 侧以端到端场景验证替代单元探针硬改（见 README S4.6）
- [x] **S4.V 验收**：大检阅 8 项全通——依赖就绪 / 001→002 双引擎建库 / 迁移 31772 行 / 一致性 37/37 / PG 端到端 22/22 / 探针后一致性重验 37/37 / sqlite 40 门禁 1125 断言 / 方言 helper 双引擎编译核对 → **回滚锚点 D**

## S5. 切 PostgreSQL 为**唯一**引擎（方向调整）
> 计划调整（用户 2026-07-24 拍板）：原 S5「PG 默认 + SQLite 显式可选/降级」升级为
> **PG 单引擎、无 SQLite、无降级、无双引擎兼容**（"挂了就挂了"）。连测试也彻底去 sqlite。
> 落地拆为 S5a（运行期/迁移侧去 sqlite）+ S5b（测试基座去 sqlite）+ 收尾三项。

### S5a. 运行期 + 迁移侧单引擎化
- [x] S5a.1 `config.py` 硬默认 PG（`_default_pg_url` 按 `AKIVILI_PG_*` 拼），删 sqlite 分支
- [x] S5a.2 `models/engine.py` 删 sqlite 分支与 PRAGMA 监听（PG 内建 WAL/MVCC/外键）
- [x] S5a.3 迁移侧（`db_migrate`/`migrations/env.py`）删 sqlite 分支，`migration_db_url` 恒 psycopg
- [x] S5a.4 `database.py` 改为**保留 aiosqlite 式 API 的 PG 适配器**（测试 seed 零改跑 PG）

### S5b. 测试基座去 sqlite
- [x] S5b.1 探针隔离改 PG namespace（`isolated_pg_db_url` 建 `qa_iso_*` 库 + atexit drop）
- [x] S5b.2 `get_connection` PG 适配器：`?`→`$N`、INSERT 自动 `RETURNING id`、`datetime('now')`→PG、`INSERT OR IGNORE/REPLACE`→`ON CONFLICT DO NOTHING`
- [x] S5b.3 修 seed 内联 sqlite SQL（`INSERT OR *`、`datetime` 修饰符）
- [x] S5b.4 sqlite 语义探针改造：退役 WAL/orm_engine 探针；新建 `run_pg_concurrency_probe`；migration/parity/dialect 探针改 PG
- [x] S5b.5 全量门禁 PG 上全绿（39 项 · 580 断言）

### S5c. 收尾
- [x] S5c.1 CI（GitHub Actions windows-latest）在 PostgreSQL 上跑门禁 + QA Suite，绿
- [x] S5c.2 根治 CI 时序/并发敏感断言（验不变式不验精确值）+ 协同链路 flaky（退避清零+驱动到静止）
- [x] S5c.3 `run_queue` 部分唯一索引 `uq_run_queue_active`（迁移 003）兜底 enqueue TOCTOU
- [x] S5c.4 `config.py` 清理 sqlite 时代死配置（`db_busy_timeout_ms`）+ `db_path` 注释收口
- [x] S5c.5 `start.ps1` 加 PostgreSQL 就绪检查（`wait_for_pg.py`，PG 不通则中止启动）
- [x] S5c.6 更新 README：PG 前置依赖、启动步骤、**无 SQLite 降级**声明
- [x] S5c.7 更新 OpenSpec：能力固化进 `specs/foundation-data-layer/`（「双引擎/PG默认化含降级」两条 Requirement 改写为「PostgreSQL 单引擎」+「数据搬迁一致性」，反映实际落地）
- [x] S5c.8 三个被阻塞 change（concurrency-scaling / graceful-restart / session-resume）design.md 顶部加「⚠️ 数据底座已切 PG 单引擎、SQLite/WAL 表述废弃、建表走 Alembic+ORM」废弃标注（不改正文/不碰 review 历史；graceful-restart spec 一致性探针改注前后均 12 文件 0 违规）
- [x] **S5.V 验收**：真实数据 cutover **已随 S5a 切引擎事实完成**——2026-07-29 只读核查确认后端自 S5a 起即运行在 PG（`localhost:5432/akivili`，启动自动 Alembic upgrade，19 表 31800 行），PG 为**当前唯一真实运行库**：`activities` 最新 2026-07-29 vs sqlite 快照停在 2026-07-28、`tasks.max(id)` PG 950 vs sqlite 277、projects PG 9 vs sqlite 4。故**不执行** `migrate_sqlite_to_pg.py --truncate`（会用过期 sqlite 覆盖 PG 真实数据，方向相反）；旧 `backend/jianagency.db`（7-28 快照）留存、非权威源（`*.db` 已被 gitignore、另有 3 个历史 .bak 快照）。当前后端进程（PID 53584，7-28 16:05 启动）仍持有该 sqlite 的 `-wal/-shm` 句柄（启动早期残留、之后再无 sqlite 写入——sqlite 数据停在 7-28 03:35 早于进程启动），故暂无法改名归档；代码侧已核查**零 sqlite 运行期 open**（无 `sqlite3.connect`/`aiosqlite`/`journal_mode`，`database.py` 是 PG 适配器），句柄纯属旧进程残留，**下次重启后端即随之释放、wal/shm 消失**，非当前代码缺陷。关键路径人工验收（建项目/跑 Agent/看流式/kill/孤儿兜底）由日常运行覆盖，无需专门 cutover 重启。change 可归档到 `changes/archive/`。

## 通用验收门（每个 S*.V 都必须满足）
- [x] 该阶段 S0.2 回归基准全绿（行为不回退）——CI 门禁 39 项 · 580 断言全绿（本地 + GitHub Actions PG 上）
- [x] 该阶段改动可通过回退到上一 `V` 锚点提交完整回滚——每步 spec 分支 + `--no-ff` 合入，锚点可回退
- [x] 无新增 `fmt.Println` 类裸输出 / 无 TODO / 无 mock 占位
- [x] 涉及重启的验证严格遵守 `backend-restart-single-instance`（杀净 8100 监听、确认端口空闲），改代码前不擅自重启后端——本轮 cutover 为只读核查，未擅自重启
