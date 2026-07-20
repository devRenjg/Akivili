# Tasks

> 规划态。未打勾 = 未实现。规模目标 5×3×2 ≈ 30 并发任务。分阶段推进，每阶段落地前独立探针验证。

## 阶段 0 — 过渡桥（低成本先做，不改架构）
- [ ] 0.1 SQLite 开 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`（连接建立时设）——消除迁移落地前的写锁真空期
- [ ] 0.2 `MAX_CONCURRENCY` 改为可配置（Paladin/环境变量），按机器资源调
- [ ] 0.3 **同 slug 串行**（一致性优先）：`_claim_one` 跳过「该 agent_slug 已在 `_running`」的 run，同一 Agent 全局同一时刻只跑一个
- [ ] 0.4 并发上限与机器资源（内存/句柄）挂钩并文档化（每 CLI 进程约 X MB → 上限 Z）

## 阶段 1 — 数据层迁移 + 公平背压（近期）
- [ ] 1.1 迁移 SQLite → **Postgres**：驱动切换、SQL 方言校正（`datetime('now')` 等）、迁移脚本、连接池
- [ ] 1.2 全量回归在 Postgres 上跑通（QA/reflect/orphan/concurrency/timeout/memory-hygiene）
- [ ] 1.3 项目公平调度：`_claim_one` 改 round-robin 或「每项目并发上限 + 全局上限」双层配额，防单项目饿死其他
- [ ] 1.4 资源准入背压：起新 CLI 进程前检查可用内存/负载，不足则排队
- [ ] 1.5 记忆写并发保护：文件锁（filelock）或 append-only + 后台 compact（配合 0.3 双保险）

## 阶段 2 — 解耦水平扩展（到单机瓶颈才做）
- [ ] 2.1 调度状态外置：`_running`/`_RUN_PIDS` 落库或 Redis，worker 无状态化
- [ ] 2.2 支持多 worker 进程并行消费队列。**依赖 [platform-graceful-restart] 的原子 claim CAS 协议——现状 `_claim_one` 尚未达多 Worker 原子安全（Review 第五轮 P1-4，撤销「原子领取已具备基础」表述）**，SHALL 在 PGR 阶段 1 落地原子 claim（单语句 CAS + generation/owner/lease 校验）后才启用多 Worker;三份 change 共享同一并发不变量与迁移顺序（见下）
- [ ] 2.2a **多实例 owner 模型 = cluster epoch + worker_instances + agent_leases（Review 第七轮 P1-7：主规格已定义，tasks 补实施路径，避免又复用单行 worker_state）**：迁移新增——① `cluster_epoch`（整个 Worker 集群的部署世代 + draining 状态，取代 PGR 单行 `worker_state.current_generation` 在多 Worker 下的语义）;② `worker_instances(instance_id PK, heartbeat_at, lease_expires_at, capacity, state)` 每 Worker 一行（**SHALL NOT 用 PGR 单行 `worker_state.owner_instance_id` 承载多实例**——单行只容一个 instance id，多实例无法同时通过 claim 校验）;③ `attempt owner = task_runs.worker_instance_id` 指向实际归属实例;④ 同 slug 全局串行用 `agent_leases(agent_slug UNIQUE, owner_attempt_id, lease_until)` 或等价原子容量机制（**SHALL NOT 只靠单进程 `_running`**）
- [ ] 2.2b **多 Worker claim / lease reclaim / capacity / draining / slug 串行**：claim 在多实例下用原子容量（PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` 或 SQLite 单语句 CAS + `agent_leases` 抢占）而非「先 COUNT 再 claim」;实例心跳失效由集群回收其 `agent_leases` 与在跑 attempt（复用 PGR fencing）;每实例 capacity 上限 + 集群总上限双层配额;实例 draining 时停领、等在跑收尾或交棒;同 agent slug 全局至多一个 active（跨实例）由 `agent_leases` 唯一约束保证
- [ ] 2.2c **多 Worker readiness fail-closed**：阶段 2 未落地 cluster epoch/worker_instances/agent_leases 前 readiness SHALL fail-closed，不以 PGR 单行 `worker_state` 冒充多 Worker;探针 `multi_worker_owner_model_probe`（两 Worker 同 cluster epoch、不同 instance id，各持不同 attempt 且通过 claim 校验;旧单行 owner 模型不得冒充多 Worker 支持）、`agent_lease_serial_probe`（多实例并发领同 slug→`agent_leases` 唯一约束保证全局至多一个 active）、`worker_instance_reclaim_probe`（实例心跳失效→集群回收其 lease 与在跑 attempt、fencing 旧实例写）
- [ ] 2.3 事件驱动唤醒替代 1s 轮询（enqueue 即唤醒）

## 阶段 3 — 分布式（远期，规模确实到了才做）
- [ ] 3.1 分布式执行器 + worker 节点池，CLI 进程分散到多节点
