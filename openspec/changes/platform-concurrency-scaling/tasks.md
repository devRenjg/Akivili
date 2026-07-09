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
- [ ] 2.2 支持多 worker 进程并行消费队列（原子领取已具备基础）
- [ ] 2.3 事件驱动唤醒替代 1s 轮询（enqueue 即唤醒）

## 阶段 3 — 分布式（远期，规模确实到了才做）
- [ ] 3.1 分布式执行器 + worker 节点池，CLI 进程分散到多节点
