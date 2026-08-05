# Multica 对标分析：平滑重启 / Session 恢复 / 数据库层

**日期**：2026-07-29　**Multica 版本**：origin/main @ `9e3b661d4`（已更新到最新）

本报告回答三个问题：
1. 数据库层：我们切 PG 后是否已基本对标 Multica？
2. 平滑重启 / session 恢复这类底层演进，方案与 Multica 的差异点？
3. 要跟上 Multica，有哪些前置工作？

> 方法：并行 4 路源码级探查（Multica 的 resume/restart 机制、Multica 的 DB 层、我们项目的现状实现、我们三个 OpenSpec change 的规划设计）。结论均基于真实源码/文档，非臆测。

---

## 0. 一句话结论

- **数据库层：已基本对标，且我们在某些点上更严谨。** 都是 PG 单引擎、参数化查询、部分唯一索引去重、`FOR UPDATE SKIP LOCKED` / CAS 领取。差异是工程选型（sqlc vs SQLAlchemy ORM、自研迁移器 vs Alembic），非能力代差。
- **平滑重启 / session 恢复：Multica 已生产落地，我们是规划态（零代码）。** 这是**能力代差最大**的地方。但我们的 OpenSpec 设计（尤其 graceful-restart 经 21 轮 Review）在若干点上比 Multica 现状更超前（two-phase dispatch + SSE 续传、attempt 级 fencing、per-execution 事件游标）。
- **最关键的架构分歧**：Multica 是**独立 daemon 进程（执行面）+ 控制面**两层物理隔离；我们现在是**单进程内 asyncio 协程池**。这是所有 restart/resume 能力的地基差异——我们的 graceful-restart「阶段 2 Worker 剥离」正是要补这一层。

---

## 1. 数据库层对标（问题 3）

### 1.1 对照表

| 维度 | Multica | JianAgency（切 PG 后） | 对标状态 |
|---|---|---|---|
| 引擎 | PostgreSQL 单引擎，无 SQLite | PostgreSQL 单引擎，无 SQLite/无降级 | ✅ 一致 |
| 驱动 | pgx/v5 + pgxpool | asyncpg（运行期）+ psycopg（迁移期） | ✅ 等价 |
| 连接池 | 显式 MaxConns=25/MinConns=5（踩过 pgx 默认=4 致尾延迟坑） | SQLAlchemy async engine 池（默认值） | ⚠️ 我们未显式调优池大小 |
| 查询层 | sqlc 代码生成 + 手写 `.sql`，零 ORM、零运行时拼接 | SQLAlchemy 2.0 ORM + 方言 helper | ◐ 不同路线，均参数化、均无拼接 |
| 迁移工具 | 自研极简迁移器（275 个 up/down SQL，整数版本号） | Alembic（001/002/003，标准工具） | ◐ 不同路线，我们用成熟工具更省心 |
| baseline | 无 baseline，001 即第一条真实迁移 | 有 001_baseline（对齐真实库快照） | ✅ 我们有 baseline |
| 迁移并发安全 | `pg_advisory_lock` 串行化多副本迁移 | 启动 `alembic upgrade head`（单实例假设） | ⚠️ 多副本部署时我们缺 advisory lock 保护 |
| 队列领取并发 | `FOR UPDATE SKIP LOCKED` + status CAS | 应用层 SELECT-then-UPDATE（**无 CAS/无 SKIP LOCKED**） | 🔴 **差距**（见 2.3） |
| 入队去重 | 部分唯一索引 `(issue_id,agent_id) WHERE status IN(queued,dispatched)` | 部分唯一索引 `uq_run_queue_active (task_id,agent_slug) WHERE status IN(queued,running)` | ✅ 一致（我们迁移 003 刚补） |
| 乐观锁 | 未用 version 列 | 未用（graceful-restart 规划 row_version） | ✅ 一致 |

### 1.2 结论

**数据库「静态层」（引擎/schema/迁移/查询/去重约束）已基本对标，无能力代差。** 我们切 PG + Alembic + ORM + 部分唯一索引后，与 Multica 在数据正确性保证上处于同一水位。

**但数据库「并发动态层」有一处真实差距**：Multica 的任务领取用 `FOR UPDATE SKIP LOCKED` + CAS 做到了**多消费者原子安全**；我们的 `_claim_one` 是 `SELECT ... LIMIT 1` 然后 `UPDATE ... WHERE id=?`（**UPDATE 不带 `AND status='queued'` CAS 条件、无 SKIP LOCKED**），正确性依赖「单进程单事件循环串行 claim」这一假设。单实例下没问题，一旦多 worker/多实例就会双领双执行。这正是我们 graceful-restart 阶段 1「原子 claim CAS」要补的（已在设计里，见 2.3）。

**两个可立即补的小差距**（低成本、不必等大演进）：
1. **连接池显式调优**：Multica 踩过 pgx 默认池=4 导致 claim 尾延迟 3s+ 的坑，显式设 MaxConns=25。我们应显式设 asyncpg 池大小，别用默认。
2. **迁移 advisory lock**：多副本部署前，`alembic upgrade` 外面包一层 `pg_advisory_lock`，防两副本同时迁移。（单实例暂不急）

---

## 2. 平滑重启 / Session 恢复对标（问题 1、2）

### 2.1 三方现状总览

| 能力 | Multica（已生产） | 我们·现状（代码里有的） | 我们·规划（OpenSpec 设计） |
|---|---|---|---|
| 执行模型 | 独立 daemon 进程 + 控制面两层 | 单进程内 asyncio 协程池（默认 3 槽） | graceful-restart 阶段2「Worker 剥离」 |
| 任务领取原子性 | `FOR UPDATE SKIP LOCKED` + CAS | SELECT+UPDATE 非 CAS（依赖单实例） | 阶段1「原子 claim 单语句 CAS」 |
| 重启后在途任务 | 判 `runtime_recovery` failed → 自动重试新 attempt → 尽量 resume 会话 | orphan 回收判死（不续跑、不重入队，只人工重派） | 阶段5「交棒 + resume 续跑」 |
| CLI session 续接 | 存 `session_id`/`work_dir`，下次 `--resume`；poison 会话 retire | **无**，每次全量回灌历史 | agent-session-resume 整个 change |
| 失败分类 | 22 值 taxonomy + 可重试白名单 | transient/permanent 二分 + 退避重试 | （现状已够，规划里进一步细化） |
| graceful shutdown | 有：LB drain hold + HTTP drain + daemon 30s 等在途 drain | **无** | 阶段2/5 |
| 进程交棒 handoff | 有：自更新原地重启 + claim barrier + 拉起继任者 | **无** | 阶段5/6 |
| 流式跨重启 | WebSocket + Redis Stream 中继（跨节点/跨重启续流） | SSE 无 Last-Event-ID，重启即断 | 阶段9「execution_events + SSE 续传」 |

### 2.2 方案差异点（我们的设计 vs Multica 现状）

**共识（方向一致）**：
- 执行面与控制面分离（Multica daemon = 我们的「Worker 剥离」）。
- 任务队列在 PG，靠 DB 约束保证并发安全。
- session 恢复 = 存 provider 的 session_id，下次 `--resume`；识别 poison 会话主动丢弃换 fresh。
- 失败分类驱动「可重试 vs 不可重试」，重试起新 attempt。
- 重启后「在途任务恢复」= 判失败 + 新 attempt 尽量续会话，**不是原地续跑同一子进程**（Multica 明确如此，我们的设计也是这个口径）。

**差异 1 — 领取原子性的实现载体**：
- Multica：`FOR UPDATE SKIP LOCKED`（PG 原生行锁跳过），无 fencing token。
- 我们规划：单语句 `UPDATE ... WHERE id=(子查询) AND status='queued' AND conversation_id IS NOT NULL RETURNING *` 的 CAS，**外加 generation + attempt 级 fencing**（比 Multica 多一层 fencing）。
- **评价**：我们规划更严。Multica 的 agent 任务队列**没有 fencing token**（只有 cron 调度器 `sys_cron_executions` 才有 lease_token fencing）；我们把 fencing 做到 attempt 级（不变量 5），是因为我们要防「残留 CLI 进程还在改文件」——这点 Multica 靠 daemon 进程 containment + runtime 归属解决，我们靠 fencing + 进程树确认双条件。**两条路，都成立。**

**差异 2 — 流式续传**：
- Multica：WebSocket + Redis Stream 中继（`ws:scope:*:stream`，maxlen 10000），跨节点/跨重启续流。**未用 SSE。**
- 我们现状：SSE 无 id、无 Last-Event-ID，重启即断。
- 我们规划：`execution_events` 表 + per-execution `event_seq`（run_queue 行锁分配）+ `Last-Event-ID: <execution_id>:<event_seq>` 续传。
- **评价**：我们规划走「DB 事件表 + SSE 续传」，Multica 走「Redis Stream 中继」。我们的方案不依赖 Redis（少一个组件），但 Multica 的 Redis Stream 天然支持多节点广播。**取决于我们要不要多节点——单机多 worker 场景，DB 事件表足够且更简单。**

**差异 3 — session 水位/事件游标的正确性**：
- Multica：靠 `session_id` 指针 + rollout 文件在位校验（`gateCodexResumeToRolloutPresence`），未见「消息水位游标」这种细粒度增量回灌。它是「resume 整个会话」而非「增量喂 N 条新消息」。
- 我们规划：per-conversation `message_seq` 水位（committed + planned 两段）做**增量回灌**——只喂 `committed < seq <= planned` 的新消息，比全量省 token。这是我们比 Multica **更细**的设计（Multica 靠 CLI 原生 session 记忆，不自己管消息水位）。
- **⚠️ 关键坑（21 轮 Review 抓出）**：`message_seq` / `event_seq` **必须用 per-conversation/per-execution 的行锁分配序号，绝不能用全局自增 id 当水位**——PG 的 identity 在 INSERT 时分配、不等提交，late-commit 的小 id 会被 reader 永久越过，导致消息漏灌 / SSE 漏投。Multica 因为不做消息级水位所以没这个坑；我们做了增量所以必须踩准。

**差异 4 — 恢复的血缘模型**：
- Multica：`parent_task_id`/`retry_of_task_id`/`rerun_of_task_id` 三条血缘线，`force_fresh_session` 标记 resume-unsafe。够用、直接。
- 我们规划：execution/attempt 一对多（模型 A）+ `execution_edges` 统一边表（recovery/migration/continuation 三类 successor，`UNIQUE(parent_execution_id)` 保证一父一子）+ recovery_chain 有界。**明显更复杂。**
- **评价**：这里我们**可能过度设计了**。Multica 生产跑的血缘模型比我们规划的简单得多。我们 21 轮 Review 堆出的 execution_edges/recovery_budgets/null_migration_requests 一整套，是把很多边界情况（NULL conversation 迁移、跨键并集唯一、late-commit）都在设计阶段解决了——严谨，但**实现成本和 Multica 的实际做法不成比例**。建议落地时对照 Multica，砍掉暂时用不到的分支（见第 3 节前置工作）。

### 2.3 我们独有的一个已落地优势

我们的 `enqueue_run` 已经用「应用层预检 + `INSERT ON CONFLICT DO NOTHING` + 部分唯一索引 uq_run_queue_active」做到了**入队去重的 DB 硬保证**（迁移 003，本周期刚做）。这和 Multica 的 037 号迁移（`(issue_id,agent_id)` 部分唯一索引）是**完全对标**的。这一层我们不欠账。

---

## 3. 前置工作清单（问题 3）

按依赖顺序排列。前置的前置是「数据底座」——**已完成**（S1~S5，PG 单引擎 + Alembic + ORM 全部落地并线上运行），这是三个演进 change 共同标注的硬前置，现在**地基已就绪**。

### 3.1 立即可做的 DB 层小补齐（不必等大演进，低成本高价值）

| # | 事项 | 理由 | 成本 |
|---|---|---|---|
| P0 | asyncpg 连接池显式设大小 | Multica 踩过默认池太小致 claim 尾延迟 3s+ 的坑，别重蹈 | 小（config 加参数） |
| P1 | `alembic upgrade` 包 `pg_advisory_lock` | 多副本部署前防并发迁移；对标 Multica migrate 的 advisory lock | 小 |

### 3.2 演进 change 的落地顺序（三个 change 的 DAG）

文档已定义唯一实施 DAG（三个 change 共享）：

```
[已完成] foundation-db（PG 单引擎地基）
    ↓
graceful-restart 阶段1（DB 协议地基：execution/attempt 状态机 + 原子 claim CAS + 新表/列）
    ↓
graceful-restart 阶段2（Worker 剥离：独立 worker.py + 进程 containment + generation/lease）
    ↓
agent-session-resume（阶段3 Claude resume / 阶段4 Codex）——同时是独立的省 token 优化
    ↓
graceful-restart 阶段5（M2.5：交棒 + resume 续跑 + 有界恢复）
    ↓
graceful-restart 阶段6（Nginx 蓝绿）
    ↓
concurrency-scaling 阶段2（多 worker，复用上面的原子 claim + worker 模型）
```

**关键依赖关系**（务必遵守）：
1. **原子 claim CAS 是全局地基**——在 graceful-restart 阶段 1。concurrency-scaling 的多 worker、graceful-restart 的 Worker 剥离都依赖它先落地。这也直接补上第 1 节说的「DB 并发动态层差距」。
2. **agent-session-resume 是 graceful-restart 阶段5（交棒续跑）的前置**——重启中断后要 resume，得先有 session_id 存储 + `--resume` 能力。但 ASR 本身是独立可交付的省 token 优化，可以先做、单独收益。
3. **concurrency-scaling 阶段2 依赖 graceful-restart 阶段1**——不能自己再造一套 claim。

### 3.3 落地前建议做的「对标校准」（避免过度设计）

我们的 graceful-restart 设计经 21 轮 Review，比 Multica 生产实现**更复杂**。落地前建议用 Multica 现状做一次「减法校准」：

| 我们的设计 | Multica 对应 | 建议 |
|---|---|---|
| execution/attempt 一对多 + `final_attempt_id` | `attempt`/`max_attempts` 计数 + `retry_of_task_id` 血缘 | 保留（我们更清晰），但别过度 |
| `execution_edges` 统一边表（3 类 successor + 并集唯一） | `parent_task_id`/`retry_of_task_id`/`rerun_of_task_id` 三列 | **考虑简化**——Multica 三列够用，边表是为极端并发场景，评估我们真需要吗 |
| `recovery_budgets`/`recovery_operations`/`recovery_requests` 三表 | 无（重试计数 + 白名单即可） | **考虑砍**——先上重试计数 + 可重试白名单（对标 Multica `retryableReasons`），有界恢复用简单 attempt ceiling |
| NULL conversation 三层硬门 + 三态迁移状态机 + quarantine 表 | `force_fresh_session` + session 门禁 | 我们的 NULL 处理是因为「可执行 task 必须有 conversation」这条自我约束衍生的一大堆边界——**评估能否用更简单的不变量收敛** |
| per-execution event_seq + execution_events 表 + SSE 续传 | WebSocket + Redis Stream | 二选一：不引 Redis 就走我们的 DB 事件表方案（更简单）；要多节点就学 Redis Stream |
| attempt 级 fencing（generation+instance+status+attempt 五重） | daemon 进程 containment + runtime 归属 + prepare_lease | 保留 fencing 思路，但校准复杂度——Multica 靠进程隔离省了很多 fencing |

**核心判断**：Multica 用「独立 daemon 进程物理隔离」这一架构选择，换来了很多**不需要用 DB fencing 精细协调**的简化——进程死了，它的 task 靠 `runtime_recovery` 整批判死重投即可，因为进程隔离保证了「死进程不会再写」。我们如果也走「独立 worker 进程 + 进程 containment」（阶段2 Worker 剥离），很多 attempt 级 fencing 的复杂度其实可以**跟着简化**。建议：**先把 Worker 剥离（阶段2）做扎实，再回看阶段5 的 recovery 设计能砍多少。**

### 3.4 session 恢复的具体前置（对标 Multica 的做法）

Multica 的 session resume 很成熟，可直接参照它的关键决策：
1. **存 session_id + work_dir 到任务行**（我们对应 `agent_sessions` 表）。
2. **claim 时 pin session**（daemon 起来马上 `PinTaskSession`）——防中途崩溃后新 attempt 仍能 resume。
3. **resume-unsafe 判定清单**（Multica `resumeUnsafeFailureReason`：iteration_limit / api 400 / context_overflow / codex 静默 / 空历史）→ 这些失败强制 `force_fresh_session`。我们的降级链设计已覆盖，可对照补全清单。
4. **retired_session 机制**（Multica 234 迁移）——被 poison 的 session 显式标记、从所有 resume 查询剔除，防坏 transcript 复活。我们设计里的 `session_version`/owner CAS 是等价保护。
5. **Codex 特有**：resume 前校验 rollout 文件在位（`gateCodexResumeToRolloutPresence`），不在位就丢 resume 避免「假装在续聊但其实从头」。**我们做 Codex resume 时必须抄这个校验。**
6. **runtime/provider 门禁**：只在同 runtime + 同 provider 才 resume（跨 runtime session 无效）。我们设计的 provider/backend/workdir 变更降级已覆盖。

---

## 4. 给决策者的三句话

1. **DB 层不欠账**：切 PG 后静态层已对标 Multica，只差「原子 claim CAS」这一动态层——而它已在 graceful-restart 阶段1 的设计里。外加两个立即可做的小补齐（连接池调优、迁移 advisory lock）。
2. **restart/resume 是最大代差**：Multica 生产跑、我们零代码。但地基（PG）已就绪，且我们的设计在 fencing/续传/增量回灌上更超前。
3. **落地策略 = 先 Worker 剥离，再校准减法**：先把「独立 worker 进程 + 进程 containment」做扎实（阶段2），它能像 Multica 一样用进程隔离换取 recovery 逻辑的简化；然后拿 Multica 现状对我们 21 轮 Review 堆出的 execution_edges / recovery_budgets / NULL 三态机做减法，避免过度设计拖慢交付。

---

*附：本报告基于 4 路源码级并行探查，证据可追溯到具体文件行号。需要某一块的详细证据引用可展开。*


