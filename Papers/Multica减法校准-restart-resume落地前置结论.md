# Multica 减法校准：restart / resume 落地前置结论

> **定位**：在动 restart/resume 代码前，拿 Multica 生产实现对我们经 21 轮 Review 的设计做「减法校准」，避免过度设计拖慢交付。本文是**决策依据文档**，不改代码、不改 spec；改 spec/tasks 前以本文结论为准。
>
> **证据基础**：三路源码级并行取证（Multica @ `C:/Code/Multica`，生产系统；我们 @ `openspec/changes/{platform-graceful-restart,agent-session-resume,platform-concurrency-scaling}`）。所有结论可追溯到 `文件:行号`。
>
> **日期**：2026-07-24（foundation-db S5 已完成、PG 单引擎线上运行之后）

---

## 0. 一句话结论

我们的 restart/resume 设计在**三处过度**（recovery 三表、generation 接管、process_cleanup 二态），在**两处不能砍**（attempt 级 fencing、message_seq 行锁水位），在**三处反而缺 Multica 已生产验证的护栏**（retired_session、Codex rollout 校验、resume-unsafe 完整清单）。落地顺序应调成 **Worker 剥离先行**——进程隔离（containment）是「砍 fencing / 简化 recovery」的前提，而非后置收尾。

---

## 1. 减法总表

| # | 维度 | 我们的设计 | Multica 生产 | 结论 | 依据 |
|---|---|---|---|---|---|
| A1 | recovery 血缘 | execution_edges 统一边表 + 三类 successor + 并集唯一 | 三列指针 `parent_task_id`/`retry_of_task_id`/`rerun_of_task_id` | **可降简版**（初版只有 retry 时三列够用） | Multica `migrations/055`、`184` |
| A2 | 恢复预算 | recovery_budgets / recovery_operations / recovery_requests 三表 | 单列 `max_attempts` 整数 + 内存可重试白名单 | **可砍**（用计数+白名单替代） | Multica `service/task.go:3388` |
| A3 | 在途恢复 | 逐 execution 判死 + 复杂状态机 | 整批一条 SQL `RecoverOrphanedTasksForRuntime` | **可简化为整批判死** | Multica `agent.sql:895-910` |
| B1 | generation 接管 | 代际交棒 ack + 硬崩溃接管 | 无（daemon 重启扫 runtime_id 整批失败） | **简化为审计记录** | Multica `daemon.go:3385` |
| B2 | claim lease | 心跳续期推进 | 固定超时 stale reclaim | **改固定超时回收** | Multica `scheduler/db_ops.go` |
| B3 | process_cleanup_state | unconfirmed/confirmed 二态 | 无（containment 保证清理必成） | **可砍**（保留 checkpoint 时间戳即可） | Multica containment 模型 |
| C1 | attempt 级 fencing | current_attempt_id + status 五重校验 | 无 attempt 层（单层状态机） | **🔴 不能砍**（双层设计命根，防同世代旧 attempt 迟到写） | 我们 spec「attempt 级 fencing」Requirement |
| C2 | message_seq 水位 | per-conversation 行锁分配 + 增量回灌 | 无（靠 CLI 原生 session 记忆） | **🔴 不能砍**（多 Agent 跨 task 模型 token 必爆；late-commit 坑必须行锁） | 我们 spec 第21轮 P0-1 |
| D1 | retired_session | **无** | `retired_session_id` 列 + 查询剔除（迁移 234，解 GH#6066） | **🟠 要补** | Multica `migrations/234`、`agent.sql:704-816` |
| D2 | Codex rollout 校验 | **无** | `gateCodexResumeToRolloutPresence` + `session_rollout_missing` 标记 | **🟠 Codex resume 必补** | Multica `daemon.go:4324-4366` |
| D3 | resume-unsafe 清单 | spec 只写「至少包含」 | 6+ 类生产验证 + 三层防御 | **🟠 补全清单** | Multica `taskfailure/failure.go:36-41`、`poisoned.go:66-166` |

---

## 2. 核心架构洞察：为什么进程隔离能换取简化

Multica 用「**独立 daemon 进程物理隔离**」这一个架构选择，换掉了一大堆 DB fencing 的精细协调：

- **死进程不会再写**：daemon 是独立长驻进程，CLI 子进程受进程树 containment（Job Object/cgroup）束缚。daemon 死 → OS 连带清理子进程 → 无需 fencing token 保证「死进程不写库」。证据：Multica agent 队列**无 lease_token**（只有 cron 调度器 `sys_cron_executions` 才有 fencing），靠状态机 + containment。（`daemon.go:354-3412`、`scheduler/db_ops.go:56-150`）
- **重启恢复 = 整批判死**：daemon 启动扫 `runtime_id` 对应的 in-flight 行，一条 SQL 全标 `runtime_recovery` failed，靠进程隔离保证这些行背后的进程确已死。（`agent.sql:895-910`）
- **优雅停止极简**：pollerCancel 止领 + taskWG.Wait 等 30s drain，**无显式 claim barrier / 交棒 ack**。（`daemon.go:3385-3401`）

**对我们的含义**：先做 Worker 剥离（独立进程 + containment）后，B1/B2/B3 的简化才有依据——containment 保证无孤儿，generation 接管、process_cleanup 二态、心跳续期都可退化为「审计 + 固定超时」。

**但一个关键区分**：Multica **没有 attempt 层**（单层状态机），所以它天然没有「同 generation 内旧 attempt 迟到写」的问题。我们是 execution/attempt 双层，**C1 attempt 级 fencing 是双层设计的必需防线，进程隔离救不了它**——attempt#N failed 回队后，`current_attempt_id` 置 NULL，旧 attempt 的迟到写必须被 status+pointer 校验挡住。这条不能砍。

---

## 3. 落地路线（修订 DAG + 动作清单）

### 3.1 DAG 顺序修订

现 OpenSpec DAG：`阶段1(DB协议地基,含全套 fencing/edges) → 阶段2(Worker 剥离)`。
**建议调整**：`阶段2 Worker 剥离先行(独立进程+containment) → 回看阶段1/5 的 fencing/recovery 能砍多少 → 再落最小 DB 协议`。理由：进程隔离是简化前提，先写全套 fencing 会返工。

> ⚠️ 此调整触及三份 change 共享的「统一实施 DAG」（第21轮 P1-5 拍板），改 DAG 需重走 Review + 过 `spec_consistency_probe` 6/6 门禁，非小事。本文仅出建议，是否调整由决策者定。

### 3.2 动作清单（按优先级，非承诺工期）

**先做（解锁简化）**
1. Worker 剥离最小版：独立 `worker.py` 进程 + 进程树 containment（Job Object/cgroup）+ 整批 orphan 判死（对标 `RecoverOrphanedTasksForRuntime`）。

**补护栏（Multica 已验证，低成本高价值）**
2. `retired_session_id` 列 + resume 查询剔除（对标迁移 234）。成本：一列 + 一个查询条件。
3. resume-unsafe 完整清单固化进 ASR spec（对标 `taskfailure/failure.go` + `poisoned.go` 三层防御：failure_reason 白名单 + 文本 ILIKE/regex + provider-agnostic）。
4. Codex rollout 在位校验 + `session_rollout_missing` 标记（Codex resume 阶段必补，对标 `gateCodexResumeToRolloutPresence`）。

**做减法（Worker 剥离落地后回看）**
5. recovery 三表 → 评估降为 `max_attempts` 计数 + 可重试白名单（对标 6 值 `retryableReasons`）；execution_edges 边表仅在确需多源 successor（NULL migration / continuation 并发汇入）时保留，restart/resume 初版只有 retry 则推迟。
6. generation 接管 → 审计记录；claim lease → 固定超时回收；process_cleanup 二态 → 砍。

**不动（必需防线）**
7. attempt 级 fencing（current_attempt_id + status）、message_seq 行锁分配水位——保留，probe 已守护其正确性。

### 3.3 与「补护栏」的关系

D1/D2/D3 三项护栏是**真缺口、非减法**——它们独立于 DAG 调整，可以先补进 ASR spec（不阻塞、纯增量、对标生产验证做法）。减法（A/B 类）则依赖 Worker 剥离先落地才有校准依据。

---

## 4. 给决策者的三句话

1. **不是全盘过度**：我们在 recovery 血缘 / generation 接管上确实过度（Multica 用 1 表我们用 3 表），但 attempt 级 fencing 和 message_seq 增量回灌是双层 + 多 Agent 模型的必需，不是过度——不能照 Multica 砍。
2. **先剥离，再减法**：Worker 剥离（进程隔离）是「砍 fencing / 简化 recovery」的前提，应先行；现 DAG「先写全套 fencing 再剥离」会返工。
3. **顺手补三个护栏**：retired_session / Codex rollout 校验 / resume-unsafe 清单是 Multica 生产验证、我们缺的真缺口，独立于减法可先补进 ASR spec。

---

*附：证据可追溯到 Multica 与本仓库的具体文件行号（见第 1、2 节）。需要某一块的完整代码引用可展开。*

