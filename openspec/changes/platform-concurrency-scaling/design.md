# Design — 多项目多 Agent 并发规模化

> 完整逐项分析见 `Papers/多项目多Agent并发-技术保障与架构演进预案.md`。本文记录风险清单、方向决策与取舍理由。

## 规模目标

5 项目 × 3 Agent/项目 × 2 并发任务/Agent ≈ **30 并发任务**。当前全局池仅 3 槽，差一个量级。

## 实地核查确认的 7 个风险（按会先炸排序）

| # | 风险 | 现状根因（已核查） | 严重度 |
|---|------|------|--------|
| R1 | SQLite 写锁 `database is locked` | `database.py` 无 WAL、无 busy_timeout、每调用新开连接；默认 rollback 模式写时全库排他锁 | 🔴 |
| R2 | 全局 3 槽池、无项目公平 | `MAX_CONCURRENCY=3` 单一全局池；`_claim_one` 按 id 全局最旧领取 | 🔴 |
| R3 | 同 Agent 跨项目并发写坏共享记忆 | 记忆按 slug 跨项目共享；`_claim_one` 不按 slug 去重 → 同名 Agent 两项目可同时跑 | 🟠 |
| R4 | 每 Agent 一 CLI 进程、无资源闸 | 每 run 起独立 claude/codex 进程，无机器级资源准入 | 🟠 |
| R5 | 单进程单机、无水平扩展 | 调度全靠内存态 `_running`/`_RUN_PIDS`，绑单进程 | 🟠 |
| R6 | 记忆文件 RMW 无锁 | `upsert_managed_section` 纯读改写，无锁 | 🟡 |
| R7 | 1s 轮询队列 | `_loop` `sleep(1.0)` 轮询 | 🟡 |

## 方向决策与理由

### 决策 1：一致性优先（R3）
**选同 slug 串行**：同一 Agent（slug）全局同一时刻只跑一个 run，跨项目也排队。
- 理由：记忆按 slug 跨项目共享，是 Agent 的核心资产，也是度量层（knowhow 复用率）的地基。若允许同名 Agent 跨项目并发，两进程会并发 read-modify-write 同一 `memory/<slug>.md` → 后写覆盖先写、knowhow 串档丢失。
- 代价：同名 Agent 跨项目不能真并行。可接受——30 并发目标下，同一 Agent 同时被多项目要求执行的概率不高，且串行只是排队非拒绝。
- 与 R6 关系：0.3 的同 slug 串行是主保险；1.5 的记忆写锁是双保险（防其他并发写路径）。

### 决策 2：数据层早迁 Postgres（R1）
**把 Postgres 从远期提到阶段 1 近期**，WAL 作为过渡桥。
- 理由：用户选一致性/正确性优先。30 并发下 WAL 其实够用，但早迁 Postgres 一步到位根治写锁（MVCC 天然并发写），避免将来再迁一次。
- 过渡桥：阶段 0 先开 WAL+busy_timeout，覆盖「决定迁移」到「迁移落地」之间的真空期，不留窗口。
- 迁移注意：SQL 方言（`datetime('now')`→`now()`、`AUTOINCREMENT`→`SERIAL/IDENTITY`、`INTEGER PRIMARY KEY`）、`PRAGMA foreign_keys` 无需（PG 默认强制）、aiosqlite→asyncpg/psycopg、连接池、迁移脚本 + 全量回归在 PG 上跑通。

## 演进路线（分阶段，不过度设计）

- **阶段 0**：WAL+busy_timeout（过渡桥）、池可配、同 slug 串行、资源挂钩文档化。低成本、不动架构。
- **阶段 1**：Postgres 迁移 + 全量回归；项目公平调度；资源背压；记忆写保护。
- **阶段 2**（到单机瓶颈才做）：调度状态外置 → 多 worker；事件驱动唤醒。
- **阶段 3**（远期）：分布式执行器 + 节点池。

## 与度量层的耦合

R3/R6 若不治理，同 Agent 跨项目并发写坏记忆 → knowhow 血缘与条目失真 → [agent-self-improvement-metrics] 的复用率指标不可信。故本能力的一致性保障是度量层能落地的前提。

## 落地纪律

- 每阶段独立走 OpenSpec change + 探针验证；Postgres 迁移必须全量回归通过才切。
- 资源治理吸取 Windows 进程/句柄继承教训（见记忆）。
- 本提案未做任何代码变更。
