# platform-concurrency-scaling (delta)

## ADDED Requirements

### Requirement: 数据层并发写正确性

系统 SHALL 保证多 Agent 并发落库不产生 `database is locked` 类失败。阶段 0 SHALL 对 SQLite 启用 `journal_mode=WAL` 与 `busy_timeout`（连接建立时设）作为过渡；阶段 1 SHALL 迁移到 Postgres（MVCC 并发写）根治，迁移 SHALL 在全量回归探针通过后方可切换。

#### Scenario: 并发写不因锁失败
- **WHEN** 多个 Agent 在约 30 并发任务下同时写库（messages/activities/task_runs/run_queue）
- **THEN** 写操作要么成功、要么在 busy_timeout 内重试成功，不得直接抛锁错误导致 run 失败

#### Scenario: 迁移前全量回归
- **WHEN** 从 SQLite 迁移到 Postgres
- **THEN** QA/reflect/orphan/concurrency/timeout/memory-hygiene 等探针在 Postgres 上全部通过后才切换生产

### Requirement: 同 Agent 记忆一致性（一致性优先）

系统 SHALL 保证同一 Agent（agent_slug）在任一时刻全局至多有一个 run 在执行，跨项目亦然。这样同一 Agent 的共享记忆文件（`memory/<slug>.md`）不会被并发读改写破坏。跨项目的同名 Agent run SHALL 排队串行，而非拒绝或丢弃。

#### Scenario: 同 slug 跨项目串行
- **WHEN** 同一 Agent 在两个不同项目各有一个待执行 run
- **THEN** 调度器同一时刻只领取其中一个执行，另一个排队等待，不并发执行、不并发写同一记忆文件

#### Scenario: 记忆写不串档
- **WHEN** 某 Agent 的任一 run 触发记忆写入（reflect 沉淀 / 近期动态）
- **THEN** 不存在另一并发进程同时写同一 `memory/<slug>.md` 造成后写覆盖先写

### Requirement: 并发调度公平与可配置

并发池大小 SHALL 可配置（不写死 3）。调度 SHALL 保证多项目公平，单个项目的大批任务 MUST NOT 饿死其他项目的任务。

#### Scenario: 池大小可配置
- **WHEN** 部署环境机器资源不同
- **THEN** 可经配置（Paladin/环境变量）调整最大并发数，无需改代码

#### Scenario: 多项目公平不饿死
- **WHEN** 项目 A 一次性提交远多于池容量的任务，随后项目 B 提交任务
- **THEN** 调度按项目轮转或双层配额执行，项目 B 的任务不必等项目 A 全部跑完才获得执行机会

### Requirement: 资源准入背压

系统 SHALL 使并发上限与机器实际资源（内存/句柄）挂钩，防止 CLI 子进程数打爆机器。阶段 1 SHOULD 在起新 CLI 进程前做资源准入检查，资源不足时排队而非硬起。

#### Scenario: 资源不足时背压
- **WHEN** 当前机器可用内存不足以再安全启动一个 CLI 进程
- **THEN** 新 run 排队等待，直到资源可用，而不是强行启动导致 OOM/句柄耗尽

### Requirement: 水平扩展路径（远期）

系统 SHALL 保留水平扩展的演进路径：当规模超出单机时，调度状态 SHALL 可外置（run_queue 已在 DB，`_running`/PID 外置到 DB/Redis），使执行 worker 无状态化、可多进程/多机水平扩展。此为规模超单机时的演进方向，非近期必做。

**本 change 与 [platform-graceful-restart]、[agent-session-resume] SHALL 共享同一并发不变量与迁移顺序（Review 第五轮 P1-4）**，SHALL NOT 各自声称「原子领取已具备」而与 PGR 的现状判断（`_claim_one` 尚未达多 Worker 原子安全）冲突。统一迁移顺序：① 先落 execution/attempt/`worker_state` 基础表与状态词汇（PGR 阶段 1 最小地基）;② 再落 task/conversation/agent 粒度的 active 唯一约束（与 PGR、ASR 的 active partial unique index 同源）;③ 再启用多 Worker 的原子容量与 claim（PGR 原子 claim CAS 落地后）;④ 分别写清 **SQLite 当前落地路径**（单语句条件 UPDATE + busy_timeout）与**未来 PostgreSQL 的 `SELECT ... FOR UPDATE SKIP LOCKED`/约束差异**，SHALL NOT 把 PostgreSQL 方案倒推成 SQLite 已具备能力。本 change 的「同 slug 全局串行」约束 SHALL 并入 PGR 的顺序与唯一索引迁移设计，不另起一套。

#### Scenario: 多 worker 无状态消费
- **WHEN** 单机资源成为瓶颈、需多 worker 分担、且 PGR 原子 claim CAS 已落地
- **THEN** 多个无状态 worker 依 PGR 原子 claim 协议（单语句 CAS + generation/owner/lease 校验）从共享队列领取并执行 run，不重复领取、不丢任务

#### Scenario: 不把 PostgreSQL 能力倒推为 SQLite 现状
- **WHEN** 文档描述当前多 Worker 领取能力
- **THEN** 明确区分 SQLite 当前落地路径与未来 PostgreSQL `SKIP LOCKED`，不声称 SQLite 阶段「原子领取已具备」
