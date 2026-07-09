## Why

多项目、多 Agent 并行任务规模化后，当前「单机单进程 + SQLite(无 WAL) + 全局 3 槽池」架构会先炸。基于对代码的实地核查（详见 `Papers/多项目多Agent并发-技术保障与架构演进预案.md`），识别出 7 个真实风险，其中最致命的是 SQLite 写锁竞争、并发池无公平、以及**同一 Agent 跨项目并发写坏共享记忆文件**——后者会直接污染 knowhow 血缘、破坏 [agent-self-improvement-metrics] 度量层的地基。

**规模目标（用户设定）**：5 个项目 × 每项目 3 个 Agent × 每 Agent 2 个并发任务 = **约 30 个并发任务**。

**方向决策（用户拍板）**：
1. **一致性优先**：同一 Agent（slug）全局同一时刻只跑一个 run，跨项目也串行排队——记忆是 Agent 核心资产、也是度量层地基，一致性 > 跨项目并行度。
2. **数据层早迁 Postgres**：不等 SQLite 撑到极限，把 Postgres 从远期提到近期；WAL 作为迁移落地前的过渡桥，避免真空期。

## What Changes

> 规划态，分阶段落实；每阶段落地前独立走探针验证。

- **阶段 0（过渡桥，低成本先做）**：SQLite 开 `WAL + busy_timeout`（消除迁移落地前的写锁风险）；并发池大小可配置；**同 slug 串行**（一致性优先）；并发上限与机器资源挂钩并文档化。
- **阶段 1（近期，含数据层迁移）**：迁移到 **Postgres**（MVCC 天然并发写，根治写锁）；项目公平调度（round-robin / 双层配额，防单项目饿死其他）；资源准入背压；记忆写并发保护（文件锁或 append-only + compact）。
- **阶段 2（规模再上台阶才做）**：调度状态外置（run_queue 已在 DB，将 `_running`/PID 外置），worker 无状态化 → 支持多 worker 进程。
- **阶段 3（远期）**：分布式执行器 + worker 节点池。

## Capabilities

### New Capabilities
- `platform-concurrency-scaling`: 平台在多项目多 Agent 并发下的技术保障与伸缩能力——数据层并发写正确性、调度公平与并发治理、同 Agent 记忆一致性、资源背压、水平扩展路径。

## Impact

- 规划态，**暂不改代码**。落实时预计涉及：`database.py`（WAL/busy_timeout→Postgres 驱动与方言）、`collab.py`（池配置/同 slug 串行/公平调度）、`memory.py`（写并发保护）、`executor/`（资源准入）。
- 强耦合：[agent-self-improvement-metrics]（R3/R6 不治理则 knowhow 血缘失真、度量失效）、[agent-collaboration]（调度）、[agent-memory]（一致性）、[agent-execution]（资源）。
- 关联文档：`Papers/多项目多Agent并发-技术保障与架构演进预案.md`（7 风险逐项分析与预案）。
- 关联记忆：Windows 进程/句柄坑（资源治理 R4 须吸取）。
