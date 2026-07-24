# Tasks

> 执行原则：阶段刚性顺序 S1→S5，**每阶段的验收门（`V` 任务）全绿才进下一阶段**。每阶段结束是一个可回滚锚点（独立提交 / 独立分支合入）。任务粒度 = 一次可独立提交、可单独回归的改动。
> 追溯：每条任务勾选时在提交信息引用任务号（如 `S1.2`）。回滚：回退到上一阶段最后一个 `V` 任务对应的提交即可。

## S0. 准备与基线锚点

> 详细施工书见同目录 `s0-plan.md`（命令、产出、验收逐条钉死）。S0 全程零代码 / 零 schema / 零重启。

- [ ] S0.1 记录当前基线：`jianagency.db` 复制为 `jianagency.db.bak_baseline-<YYYYMMDD>`（已核对匹配 gitignore `*.db.bak*`，不入仓）；用 `py -3.12` 遍历 `sqlite_master` 导出全表结构快照到 `openspec/changes/2026-07-24-foundation-db-alignment/baseline_schema.sql`（纳入追踪）作为 S2 固化的比对基准
- [ ] S0.2 建立回归基准：入口已确认 = `TestReport/run_qa_suite.py`（主套件 31/31）+ 25 个隔离 probe（见 `TestReport/README.md`，全部临时 DB 隔离、不碰真实库）。跑一遍记录每脚本实测 `N/N` 到 change 目录 `baseline_regression.md`，作为后续每阶段「行为不回退」对照；`*` 标记的需真实 CLI 供应商（collab/codex smoke）不纳入基线
- [ ] S0.3 统计并登记 SQL 访问面清单（15 文件 / 398 调用点 / 11 旁路连接 / 41 datetime / 17 AUTOINCREMENT）到 change 目录 `sql_surface_checklist.md`，每项带 `file:line`（执行时 Grep 复核刷新行号），作为 S1/S3 收敛的 checklist

## S1. 连接收口 + PRAGMA 调优（零行为变更）
- [ ] S1.1 `get_connection()` 统一开启 `PRAGMA journal_mode=WAL`、`PRAGMA busy_timeout=<配置>`、`PRAGMA foreign_keys=ON`（busy_timeout 值走 `config.py`，不硬编码）
- [ ] S1.2 `routes/auth.py` 3 处旁路 `aiosqlite.connect` 改走 `get_connection()`
- [ ] S1.3 `auth.py` 2 处旁路改走 `get_connection()`
- [ ] S1.4 `skills.py` 2 处旁路改走 `get_connection()`
- [ ] S1.5 `agents.py` 2 处旁路改走 `get_connection()`
- [ ] S1.6 `database.py` 内 2 处直接 `connect`（init 建库除外）审查：init 路径保留、运行期路径收口
- [ ] S1.7 连接生命周期审查：确认 WAL 下连接复用/关闭策略一致，无连接泄漏
- [ ] **S1.V 验收**：全库 `grep aiosqlite.connect` 仅剩 init 建库处；WAL 文件（`-wal`/`-shm`）正常生成；并发写压测不再 `database is locked`；S0.2 回归全绿（行为零变更）→ **提交，回滚锚点 A**

## S2. 迁移框架 Alembic（Schema 版本化，结构不变）
- [ ] S2.1 引入 `alembic` 依赖；`alembic init backend/migrations`，配置 async engine + 从 `config.py` 读 DB URL
- [ ] S2.2 关闭 `database.py` 里 `PRAGMA table_info` 兜底加列逻辑的「隐式演进」——改为「schema 只由迁移定义」（本任务只标记与规划，实际切换在 S2.4 后）
- [ ] S2.3 编写 `001_baseline` 迁移：把 S0.1 的 `baseline_schema.sql` **原样**转成 Alembic 迁移（表/列/索引逐一对齐，不改任何结构）
- [ ] S2.4 启动流程接入 `alembic upgrade head`（`main.py` startup 内，早于 `reclaim_orphan_runs`）
- [ ] S2.5 空库可重放验证：全新空库启动 → 自动 apply 001 → 结构与 S0.1 快照逐字段一致
- [ ] S2.6 存量库幂等验证：现有 `jianagency.db` 启动 → 标记为 001 已应用（`alembic stamp`）→ 不重复建表、不丢数据
- [ ] S2.7 回滚验证：`alembic downgrade` 能回退 001（验证 down 迁移可用，不实际用于生产数据）
- [ ] **S2.V 验收**：空库重建结构 == 基线快照；存量库启动幂等无损；up/down 双向可用；S0.2 回归全绿 → **提交，回滚锚点 B**

## S3. ORM（SQLAlchemy 2.0 async）+ SQL 收敛 + 方言隔离
- [ ] S3.1 引入 `sqlalchemy[asyncio]` 依赖；建 `backend/models/` 定义全部表的 ORM 模型（对齐 001 基线，逐表）
- [ ] S3.2 建统一 async session 工厂，桥接 S1 的连接调优（WAL/busy_timeout 在 engine 层配置）
- [ ] S3.3 方言 helper 收敛：`datetime('now')`（41 处）统一为 ORM 时间表达式 / 单一 helper；`AUTOINCREMENT`（17 处）改由 ORM 主键策略表达；占位符统一
- [ ] S3.4 按表分批迁移手写 SQL 到 ORM（每表一提交，独立回归）：
  - [ ] S3.4a `database.py`（37）
  - [ ] S3.4b `collab.py`（36）
  - [ ] S3.4c `routes/runs.py`（28）
  - [ ] S3.4d `progress.py`（21）
  - [ ] S3.4e `routes/tasks.py`（20）
  - [ ] S3.4f `executor/runner.py`（17）
  - [ ] S3.4g `routes/agents.py`（16）
  - [ ] S3.4h `routes/project_agents.py`（14）
  - [ ] S3.4i `routes/agent_cli.py`（12）
  - [ ] S3.4j 其余文件（`agent_config.py` 8 / `reflect.py` 8 / `activity.py` 7 / `skills.py` 6 / `projects.py` 6 / `skills(root).py` 4 等）
- [ ] S3.5 每批迁移后校验：该表相关接口行为与迁移前逐一对照（读结果一致、写副作用一致）
- [ ] **S3.V 验收**：`grep` 手写 SQL 归零（或仅剩迁移文件）；方言用法集中在 helper/ORM 一层；S0.2 回归全绿；关键接口逐一自测通过 → **提交，回滚锚点 C**

## S4. 引擎抽象 + SQLite⇄PostgreSQL 双跑
- [ ] S4.1 引入 `asyncpg` 依赖；`config.py` 支持 `DB_URL` 切换（sqlite / postgresql），engine 按 URL 构造
- [ ] S4.2 本地起 PostgreSQL 实例（Docker / 本机），记录版本与连接参数到 README 草稿
- [ ] S4.3 S2 的 Alembic 迁移在 PG 上重放建库：修正 SQLite-only 语法（若 001 里有），确保 001 在两引擎都可 apply
- [ ] S4.4 ORM 层在 PG 上跑通：逐表 CRUD 冒烟，修正 SQLite/PG 行为差异（大小写、布尔、时间时区、自增策略）
- [ ] S4.5 编写数据搬迁脚本：`jianagency.db` → PG 全表导入（保序、保外键、保时间）
- [ ] S4.6 编写一致性校验脚本：搬迁后逐表行数 + 关键字段抽样比对 SQLite 与 PG 一致
- [ ] S4.7 双跑回归：同一套代码分别连 SQLite 与 PG 各跑一遍 S0.2 回归，两边都全绿
- [ ] **S4.V 验收**：001 迁移两引擎都可建库；ORM 两引擎都跑通；搬迁数据一致性校验通过；双跑回归两边全绿 → **提交，回滚锚点 D**

## S5. 切 PostgreSQL 为默认
- [ ] S5.1 默认 `DB_URL` 指向 PostgreSQL；SQLite 保留为显式可选（本地轻量开发）
- [ ] S5.2 `start.ps1` 增加 PG 就绪检查 / 启动前置（不破坏现有单进程流程）
- [ ] S5.3 PG 上跑完整回归 + 关键路径人工验收（建项目 / 跑 Agent / 看流式 / kill / 孤儿兜底）
- [ ] S5.4 更新 README：PG 前置依赖、启动步骤、SQLite 降级说明
- [ ] S5.5 更新 OpenSpec：本 change 涉及能力固化进 `specs/foundation-data-layer/`；三个被阻塞 change 的 schema 章节改注「基于 Alembic 迁移 + ORM 实现」
- [ ] **S5.V 验收**：PG 默认启动全回归绿 + 人工验收通过；README/OpenSpec 更新完成；底座对齐 Multica 完成 → **提交，change 待归档**

## 通用验收门（每个 S*.V 都必须满足）
- [ ] 该阶段 S0.2 回归基准全绿（行为不回退）
- [ ] 该阶段改动可通过回退到上一 `V` 锚点提交完整回滚
- [ ] 无新增 `fmt.Println` 类裸输出 / 无 TODO / 无 mock 占位
- [ ] 涉及重启的验证严格遵守 `backend-restart-single-instance`（杀净 8100 监听、确认端口空闲），改代码前不擅自重启后端
