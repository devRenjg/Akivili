# platform-graceful-restart (delta)

## ADDED Requirements

### Requirement: durable execution 状态机与并发不变量

系统 SHALL 以持久化执行状态机承载每次执行：一条 `run_queue` 行 = 一个**稳定 execution**，状态 `queued → claimed → running → {done | failed | killed | superseded | recovery_blocked}`（**execution 成功态 SHALL 命名 `done`、SHALL NOT 命名 `succeeded`**——`succeeded` 是 attempt 层终态，两层不共用一张状态图，见下方「双层状态词汇表」Requirement;**无独立 accepted 态**：POST 同事务写用户消息 + 建 queued execution、提交后才返回 execution_id;`recovery_blocked` = 无法安全恢复而进 dead-letter 待人工的终态，见异常重启 Requirement）。**execution : attempt SHALL 为一对多（模型 A）**：每次 claim（含 lease 回收、瞬时失败重试）SHALL 在该 execution 下创建一个新的 attempt（`task_runs` 行，`attempt_no` = 该 execution 现有最大 +1），约束为 `UNIQUE(run_queue_id, attempt_no)`，**SHALL NOT 用 `run_queue_id` 单列唯一**（否则 lease 回收/重试的第二次 claim 会撞唯一键）。claimed 时 SHALL 同事务创建该新 attempt 并在 `run_queue` 写 `claim_lease_until`（Review 第六轮 P0-1：claimed 阶段领取租约命名 `claim_lease_until`，区别于 `worker_state.lease_expires_at` 世代心跳租约;`task_runs` 无 lease 字段）;`claimed` 超 `claim_lease_until` 未转 running SHALL 可被 CAS 回收——execution 回 `queued`、该 attempt 落 `abandoned`，下次 claim 建 `attempt_no+1`;`claimed→running` SHALL 用 CAS 校验 claim_owner/claim_generation。execution 终态 SHALL 由**定局 attempt**（`final_attempt_id`，第六轮 P1-3：final 而非 winning——失败/被杀 execution 亦有 final attempt）决定，非定局 attempt 落 `abandoned/superseded` 等终态（失败 attempt 一律 `failed` + `failure_stage`，见双层状态词汇表 P1-2 方案 B）。**终态 `final_attempt_id` 非 NULL 有唯一例外（第十六轮 P1-B + 第十七轮 P1-C）**：`status=recovery_blocked AND blocked_reason=null_conversation_migration AND terminal_source_status=queued`（从未 claim、无 attempt）的 execution `final_attempt_id=NULL` 合法——例外条件 SHALL 引用进入终态时固化的不可变列 `terminal_source_status`（第十七轮 P1-C），SHALL NOT 引用运行期推导的 `source_status`;除此之外所有终态 SHALL `final_attempt_id` 非 NULL（见「execution/attempt 指针协议」Requirement 的例外定义与 CHECK 等价校验）。recovery child SHALL 是**新的 execution**（新 run_queue 行 + `superseded_from`），与「同一 execution 内多 attempt」分属两个层级。SSE SHALL 绑稳定 execution_id，同一 execution 的多 attempt 事件进同一事件流。引入 `claimed`/`superseded` 后，所有状态消费者（progress 聚合、Runtime 总览、任务自动流转、孤儿巡检、失败归因、前端状态色）SHALL 同步识别新状态，SHALL NOT 把 `claimed` 误判空闲或把 `superseded` 落入成功显示;完整状态矩阵 + 允许转换表 SHALL 在阶段 1 引入状态时同步落地。系统 SHALL 满足以下不变量：① 一个 queue item 同一时刻只被一个 Worker generation 持有;② 同一 `(conversation, agent)` 最多一个 active（queued/claimed/running）且最多一个持久化 pending intent（Review 第六轮 P1-1：粒度与 session owner 键统一为 conversation;`conversation_id` 为空的历史/系统 run 走 `(task_id, agent_slug)` NULL 兜底索引，见迁移 Requirement）;③ execution 的 `running→done` 与 `running→superseded` 通过 CAS 竞争只能一个成功;④ `superseded + recovery child 入队` 同一事务提交;⑤ 旧 generation **及同世代的旧 attempt** 不能 finalize、不能写平台（fencing SHALL 到 attempt 级：generation+instance+attempt/execution/current pointer 全匹配才放行，见「`jian` 平台写的 attempt 级 fencing」Requirement，第七轮 P0-4）;⑥ recovery chain 有次数上限 + 退避 + dead-letter;⑦ task_run/run_queue/session 水位/消息投递之间有明确事务边界。状态转换 CAS 的 source status **SHALL 按转换类型分别指定、SHALL NOT 一刀切为 `WHERE status='running'`（Review 第七轮 P1-5）**：`running→done/failed/superseded` 用 `WHERE status='running' AND worker_generation=?`;但 `claimed→queued`（claim lease 回收）、`claimed→killed`（claimed 阶段被 kill）、`claimed→failed`（prestart failure 在 CLI 启动前终局失败）等转换的 source status 是 `claimed` 而非 `running`。完整 source-status transition table（每条转换的合法 source status + owner/generation/instance 条件）SHALL 在阶段 1 引入状态时与状态机一并落地，见下方「状态转换表」Requirement。

#### Scenario: 自然完成与交棒互斥
- **WHEN** 一个 running run 同时被「自然完成」与「交棒 supersede」触发
- **THEN** 两者通过 CAS 竞争，只有一个成功落终态；结果只能是「execution=done（定局 attempt=succeeded）且无 recovery child」或「execution=superseded 且恰好一个 recovery child」，不出现互相覆盖或半提交

#### Scenario: supersede 与 recovery child 同事务
- **WHEN** 一个 running run 被交棒中断需要续跑
- **THEN** 「旧 run 落 superseded」与「recovery child 入队」在同一事务提交，不出现「已 superseded 但无 child」的半提交状态

#### Scenario: 旧 generation 被 fencing
- **WHEN** 一个属于旧 Worker generation 的进程（含崩溃后残留的孤儿 CLI）尝试 finalize run 或调用 `jian` 写平台
- **THEN** 系统按 generation 校验拒绝该写入（当前活跃 generation 不匹配），防止旧执行与恢复执行双写

### Requirement: 双层状态词汇表（execution vs attempt 单一真相源）

系统 SHALL 维护**唯一一张**双层状态词汇表，`proposal.md`/`design.md`/`spec.md`/`tasks.md`/后端状态枚举/前端状态文案 SHALL 全部引用该表，SHALL NOT 在任一处使用未在表内定义的状态词，SHALL NOT 让 execution 层与 attempt 层共用一张模糊状态图（Review 第四轮 P0-1）。两层各有独立命名空间，成功态在两层 SHALL NOT 同名：

**execution 层（`run_queue.status`，稳定执行）**：

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `queued` | 已入可领取队列，attempt 尚未创建 | 否 |
| `claimed` | 被某 Worker generation 原子领取、已建当前 attempt、尚未起 CLI | 否 |
| `running` | CLI 已起、attempt 正在执行 | 否 |
| `done` | 成功终态（由定局 attempt=`succeeded` 决定）；**SHALL NOT 命名 `succeeded`** | 是 |
| `failed` | 失败终态（定局 attempt=`failed`，恢复次数已耗尽或不可重试） | 是 |
| `killed` | 用户主动终止，不续跑 | 是 |
| `superseded` | 交棒/reclaim 中断，恰好一个 recovery child 入队（child 为**新 execution**） | 是 |
| `recovery_blocked` | 无法安全恢复，进 dead-letter 待人工，不自动再生成 child | 是 |

**attempt 层（`task_runs.status`，单次尝试）**：

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `claimed`/`preparing` | 该 attempt 已建、CLI 未起 | 否 |
| `running` | 该 attempt 的 CLI 正在执行 | 否 |
| `succeeded` | 该 attempt 成功收尾（= 该 execution 的定局 attempt，驱动 execution 落 `done`）；**SHALL NOT 命名 `done`** | 是 |
| `failed` | 该 attempt 执行失败（**发生阶段/归因/可重试性由正交字段 `failure_stage`+`failure_class`+`retryable` 表达**，见下） | 是 |
| `killed` | 该 attempt 被用户 kill | 是 |
| `abandoned` | **无残留进程的非成功中止（Review 第十六轮 P1-A 扩义 + 第十七轮 P0 收窄）**——正交字段 `abandon_stage(prelaunch\|running)` + `abandon_reason(lease_reclaim\|null_conversation_migration\|protocol_incompatible)` 区分：`prelaunch/lease_reclaim`=claimed lease 回收、CLI 未起（**非定局**、execution 回 `queued`）;`prelaunch/null_conversation_migration`=**claimed NULL、CLI 未起无残留进程**（**可作 final**、execution `recovery_blocked(null_conversation_migration)`、不回队;**第十七轮 P0：running NULL 不再落 abandoned——running NULL 隔离恒 `orphaned`，进程确认与否由正交 `process_cleanup_state` 表达**）;`protocol_incompatible`=协议不兼容中止（可作 final）。**定局性 SHALL 由 `final_attempt_id` 是否引用它决定、SHALL NOT 仅凭 `status=abandoned` 判非定局或回队性** | 是 |
| `orphaned` | **进程树未确认退出的孤儿（Review 第八轮 P1-B）**——reclaim 发现残留 `running` attempt 但无法证明其 CLI 已停;与 `abandoned` 语义分开，因 `orphaned`=进程**未确认退出**（可能存活）、`abandoned`=**无残留进程**（未起 CLI 或已确认退出）。**其 execution `blocked_reason` 按来源子类分（第十七轮 P0/P1-B）**：unsafe orphan/protocol mismatch → `process_not_confirmed_dead`;**activate 前 running NULL 隔离 → `null_conversation_migration`，attempt 恒 `orphaned` 首次即永久终态、进程确认与否走正交 `process_cleanup_state(unconfirmed\|confirmed)`，两阶段均 SHALL NOT 把它改写为 `abandoned`** | 是 |
| `superseded` | 该 attempt 随 execution 交棒中断 | 是 |

**失败 attempt 用「状态负责结果 + 正交字段负责阶段/归因/可重试」（Review 第六轮 P1-2 拍板方案 B）**——**取消 `prestart_failed` 作为独立 attempt 终态状态**；「起进程前准备失败」不再是单独状态名，而是 `status=failed` 上的一个阶段标记。三个正交维度：

```text
status        = failed                          -- attempt 失败结果（唯一失败终态名）
failure_stage = prestart | running             -- 发生阶段（prestart = 起 CLI 进程前的准备）
failure_class = infrastructure | configuration | business   -- 失败率/告警归因
retryable     = true | false                    -- 是否重排
```

由 `retryable + recovery_count` 决定是否重排（`retryable=true` 且未达恢复上限 → **同 execution 回 queued、下次 claim 建 attempt#N+1**，第七轮 P0-1：普通瞬时重试走同 execution 新 attempt、SHALL NOT 走 recovery child;否则 execution 落 `failed`/`recovery_blocked`）;由 `failure_class` 决定计基础设施失败率还是业务失败率（`infrastructure`/`configuration` 不计业务失败率，`business` 计）;`failure_stage` 只记发生阶段、不隐含结果或可重试性。**这样「决定 execution failed 的定局 attempt」永远是 `status=failed`**，与「定局 attempt=`failed` ⇒ execution=`failed`」的层间映射自洽，消费者只认一个失败终态名。

**层间映射 SHALL 恒定**：execution 终态由**定局 attempt** 决定——定局 attempt=`succeeded` ⇒ execution=`done`;定局 attempt=`failed`（且恢复耗尽/不可重试，含 `failure_stage=prestart` 的不可重试准备失败）⇒ execution=`failed`;`orphaned`（进程树未确认退出的孤儿 attempt，第八轮 P1-B）驱动 execution=`recovery_blocked` 且 `final_attempt_id` 指向它——**其 `blocked_reason` 按 orphaned 来源子类分（第十七轮 P1-B）**：unsafe orphan / protocol mismatch 等 CLI 已起残留 → `process_not_confirmed_dead`;**activate 前 running NULL 隔离（第十七轮 P0）→ `null_conversation_migration`（attempt 恒 `orphaned`、进程确认与否由正交 `process_cleanup_state(unconfirmed|confirmed)` 表达、SHALL NOT 用 attempt 状态从 orphaned 改写为 abandoned）**。**`abandoned` 的定局性按正交字段分（第十六轮 P1-A）**：`abandon_reason=lease_reclaim`（prelaunch）= **非定局**，execution 回 `queued`、不被 `final_attempt_id` 引用;`abandon_reason=null_conversation_migration`（**claimed·CLI 未起无残留进程**，第十七轮 P0 收窄——running NULL 不再落 abandoned）= **定局**，驱动 execution=`recovery_blocked(null_conversation_migration)` 且被 `final_attempt_id` 引用;`abandon_reason=protocol_incompatible`（第十七轮 P1-B）= **定局**，驱动 execution=`recovery_blocked(protocol_incompatible)` 且被 `final_attempt_id` 引用。`superseded` 恒为非定局（随 execution 交棒中断）。**定局性 SHALL 由「是否被 `final_attempt_id` 引用」决定，SHALL NOT 仅凭 `status` 一刀切判 `abandoned` 非定局**。所有 execution 自然完成 CAS SHALL 明确写成 `SET status='done' WHERE status='running' AND worker_generation=? AND worker_instance_id=?`，SHALL NOT 写 `SET status='succeeded'`。

**attempt 状态消费者矩阵 SHALL 与 execution 消费者矩阵并列落地**（Review 第四轮 P1-2）——除 execution 层消费者外，attempt 层各终态 SHALL 明确以下消费口径：

| attempt 终态 | 是否计入失败率 | Runtime/RunRow 展示 | 是否触发任务失败/自动流转 |
|---|---|---|---|
| `succeeded` | 否 | 成功（且驱动 execution=`done`） | 按 execution=`done` 走正常流转 |
| `failed`（含 `failure_stage=prestart`/`running`） | 由 `failure_class` 决定：infrastructure/configuration 不计业务失败率、business 计;retryable 时计入恢复计数 | 失败（可按 `failure_stage` 标「准备失败」/「执行失败」） | 仅当为**定局 attempt** 且 execution 落 `failed` 才触发任务失败流转;非定局 failed attempt 不单独触发;`retryable=true` 未达上限则**同 execution 回 queued、下次 claim 建 attempt#N+1**（Review 第七轮 P0-1：普通瞬时重试走同 execution 新 attempt，SHALL NOT 走 recovery child）、不触发任务失败 |
| `killed` | 否（用户主动） | 已终止 | 不触发续跑/流转 |
| `abandoned`·`lease_reclaim`（prelaunch） | 否（非定局、claimed lease 回收、未起 CLI） | 折叠隐藏或标「已放弃」，SHALL NOT 显示为失败 | 不触发;execution 回 `queued` 重领 |
| `abandoned`·`null_conversation_migration`（**claimed·CLI 未起无残留进程**，第十六轮 P1-A + 第十七轮 P0 收窄） | 否（基础设施/迁移中止、非业务失败） | 标「迁移待处理」（关联 execution=`recovery_blocked(null_conversation_migration)`）;`final_attempt_id` 指向它 | 不触发自动流转;execution `recovery_blocked` 不回队、等人工迁移出口 |
| `abandoned`·`protocol_incompatible`（第十七轮 P1-B） | 否（协议不兼容中止、非业务失败） | 标「协议不兼容·待处理」（关联 execution=`recovery_blocked(protocol_incompatible)`）;`final_attempt_id` 指向它 | 不触发自动流转;等人工恢复出口 |
| `orphaned`（第八轮 P1-B）——含 unsafe orphan/protocol mismatch（`process_not_confirmed_dead`）与 **running NULL 隔离（`null_conversation_migration`，第十七轮 P0）** | 否（基础设施中断、非业务失败，不计业务失败率） | 标「孤儿·待确认清理」;`process_not_confirmed_dead` 关联 execution=`recovery_blocked(process_not_confirmed_dead)`;running NULL 关联 execution=`recovery_blocked(null_conversation_migration)` + `process_cleanup_state`（`unconfirmed`=待确认清理、`confirmed`=派生展示「已清理·待迁移」）;`final_attempt_id` 指向该 orphaned attempt（**两子类 attempt 均恒 orphaned 不改写**） | 不触发自动流转;`process_not_confirmed_dead` 人工确认清理后恢复 recovery child;running NULL 经 `confirm_null_process_cleanup()` 翻 `confirmed` 后走 migration 出口建普通 successor |
| `superseded` | 否 | 折叠（execution 已 superseded、由 recovery child 承接） | 不触发（避免误判 done/reviewing） |

此外 SHALL 明确：① **retryable running failure** 走 attempt=`failed`（`failure_stage=running`）+ execution 层恢复计数决定是否重试;② **claimed 阶段被 kill** 的合法转换为 attempt→`killed`、execution→`killed`;③ **prestart failure** 走 attempt=`failed`（`failure_stage=prestart`），其**可重试性由 `retryable` 字段而非状态名决定**——`retryable=true` 且未达恢复上限则**同 execution 回 queued、下次 claim 建 attempt#N+1**（Review 第七轮 P0-1：普通瞬时重试走同 execution 新 attempt，非 recovery child;recovery child 只用于 supersede/交棒）、否则依恢复上限使 execution 落 `failed` 或 `recovery_blocked`（Review 第六轮 P1-2 方案 B：不再有独立 `prestart_failed` 状态名）。**每次 retryable attempt 终态回队 SHALL 在同一 `finish_execution()` 事务内退休该 attempt 的 owner epoch**（保留 session_id 供下次 attempt 重新 acquire，见 P0-1）。完整 attempt 允许转换表 SHALL 在阶段 1 引入 attempt 状态时与 execution 转换表一并落地。

#### Scenario: 非定局 attempt 不污染失败率与流转
- **WHEN** 某 execution 的 attempt#1 落 `abandoned`（lease 回收）、attempt#2 落 `succeeded`
- **THEN** 失败率不计入 attempt#1，Runtime 不把 attempt#1 显示为失败，任务按 execution=`done` 正常流转，SHALL NOT 因 attempt#1 触发任务失败

#### Scenario: 两层成功态不同名
- **WHEN** 一个 execution 正常执行成功
- **THEN** 定局 `task_runs.status='succeeded'`、其 `run_queue.status='done'`；progress 聚合、Runtime 总览、`terminal{status}` SSE payload、partial index、前端状态色均按各自所在层的正确值消费，不把 execution 写成 `succeeded`、不把 attempt 写成 `done`

#### Scenario: 词汇表为单一真相源
- **WHEN** 任一文档段落、后端枚举或前端文案需要引用执行/尝试状态
- **THEN** 其取值必须来自本词汇表；出现表外状态词或两层混用同名成功态时视为规格缺陷，须先修表再实现

### Requirement: 状态转换表（每条转换的 source status + owner/generation 条件）

**顶层不变量 SHALL NOT 把所有终态转换写成单一 `WHERE status='running'`（Review 第七轮 P1-5）**——多条合法转换的 source status 并非 `running`。系统 SHALL 提供完整 execution 与 attempt 转换表，每条转换 SHALL 明确 source status、目标 status、generation/instance/current_attempt/owner token/lease-budget 条件、是否写 `final_attempt_id`、事件类型，并在阶段 1 引入状态时与状态机一并落地。至少覆盖：

| 层 | 转换 | source | generation/instance/current_attempt 条件 | lease/budget/owner 条件 | 写 final_attempt_id | 事件 |
|----|------|--------|------------------------------------------|------------------------|--------------------|------|
| execution | `queued→claimed` | `queued` | `AND status='queued'`（原子 claim）+ worker_state generation/instance/lease/state='running' | claim 容量未超 | 否 | `run_claimed`（仅领取，CLI 未起，第九轮：不复用 `run_started`） |
| execution | `claimed→running` | `claimed` | 校验 `claim_owner`/`claim_generation` 未变 | — | 否 | `run_started`（CLI 真正启动，唯一发 run_started 的转换） |
| execution | `claimed→queued`（claim lease 回收） | `claimed` | — | `AND claim_lease_until < db_now` | 否（清 current） | `retry_scheduled` |
| execution | `claimed→killed` | `claimed` | kill `target_generation`==当前活跃 generation | — | 是（指该 attempt） | `terminal(killed)` |
| execution | `claimed→failed`（prestart 终局） | `claimed` | generation/instance 匹配 | **`retryable=false` OR (`retryable=true` AND retry/recovery 预算耗尽)** | 是 | `terminal(failed)` |
| execution | `running→done` | `running` | `AND worker_generation=? AND worker_instance_id=?` | owner 匹配 | 是（succeeded attempt） | `terminal(done)` |
| execution | `running→failed` | `running` | `AND worker_generation=? AND worker_instance_id=?` | `retryable=false` OR 预算耗尽 | 是 | `terminal(failed)` |
| execution | `running→killed` | `running` | `worker_generation` + `target_generation` 匹配 | — | 是 | `terminal(killed)` |
| execution | `running→superseded` | `running` | `AND worker_generation=? AND worker_instance_id=? AND current_attempt_id=?`（**补 instance/current_attempt，防多 Worker 错误实例定局别人 attempt**，第八轮 P1-A） | — | 是（触发交棒的 attempt） | `superseded` |
| execution | `running/claimed→queued`（瞬时 retry 回队） | `running` 或 `claimed` | generation/instance 匹配 | 未达恢复上限（同 execution 新 attempt，见 P0-1）;retire owner | 否（清 current+final） | `retry_scheduled` |
| execution | `→recovery_blocked` | `running`/`claimed` | generation/instance 匹配 | 无法安全恢复/预算耗尽/协议不兼容，带 `blocked_reason`（**source 不含 `superseded`——终态不可逆，见下**） | 是 | `terminal(recovery_blocked)` |
| attempt | `claimed/preparing→running` | `claimed`/`preparing` | owner CAS | — | — | `run_started` |
| attempt | `running→succeeded/failed/killed` | `running` | owner/generation/instance CAS | — | — | `terminal`/`retry_scheduled` |
| attempt | `running→orphaned`（unsafe orphan，第八轮 P1-B） | `running` | 进程未确认死亡 | — | 是（指该 orphaned attempt） | `terminal(recovery_blocked)` |
| attempt | `claimed→abandoned`（lease 回收放弃/未起 CLI/gate 未释放 protocol mismatch/NULL migration claimed） | `claimed` | claim lease 过期 或 protocol mismatch 且 launch gate 未释放（CLI 未起） 或 NULL migration（claimed·CLI 未起） | `abandon_stage=prelaunch` | lease_reclaim 否;NULL migration 是（driven recovery_blocked） | —（NULL migration 时 `terminal(recovery_blocked)`） |
| execution | `process_cleanup_state: unconfirmed→confirmed`（NULL migration 进程清理确认，第十七轮 P0 `confirm_null_process_cleanup()`） | `recovery_blocked`（**execution 状态不变**、只翻正交字段） | `AND status='recovery_blocked' AND blocked_reason='null_conversation_migration' AND process_cleanup_state='unconfirmed' AND cleanup_version=?`（CAS） | fencing 生效 **AND** 完整进程树确认退出（`pid+create_time` 指纹一致） | 否（**attempt 恒 orphaned 不改写、final 不变**） | 无第二 terminal（写 `cleanup_confirmed_at/by/evidence_hash`、`cleanup_version+1`;**SHALL NOT 向已封闭父 SSE 发第二个 terminal** ） |
| attempt | `running→orphaned`（protocol mismatch 且 gate 已释放/CLI 已起，第九轮 P0-C + 第十轮 source 对齐;或 NULL migration·两阶段首次隔离 `quarantine_null_execution()`，第十七轮 P0，attempt 恒 orphaned 永久终态、进程确认与否只翻 `process_cleanup_state`） | `running`（**gate 释放在 CAS 转 running 之后，故 source 必为 running 非 claimed**，见启动闸门 Requirement 步骤 2→3→4） | protocol mismatch AND launch gate 已释放（此时 attempt 已 running、进程未确认退出）;或 NULL migration（首次隔离即落，`process_cleanup_state=unconfirmed`、`cleanup_version=0`） | — | 是（指该 orphaned attempt） | `terminal(recovery_blocked)` |
| execution | `recovery_blocked` 人工恢复（第十一轮 P0-A：父终态**不变**，仅建 child） | `recovery_blocked` | 人工确认前置（据 `blocked_reason` 分原因，见恢复矩阵）+ `UNIQUE(superseded_from)` 保证至多一个 child | **父 `recovery_blocked` 永久不变**、`final_attempt_id` 永久指向定局 attempt（如 orphaned） | 否（父不改，不重复 retire） | 恰好 1 个 recovery child execution（`queued`，`superseded_from=父`）+ child 流 `recovery_resumed{source:manual}` + child `queued` 事件;**父 SHALL NOT 发第二个 terminal/superseded 事件** |
| attempt | `running→superseded`（交棒，第九轮拆 wildcard） | `running` | `AND worker_generation=? AND worker_instance_id=? AND current_attempt_id=?` | 交棒且前置 AND 条件满足 | — | `superseded` |
| attempt | `claimed→superseded`（交棒发生在 CLI 起前，第九轮拆 wildcard） | `claimed` | `AND worker_generation=? AND worker_instance_id=?` | 交棒 | — | `superseded` |

SHALL NOT 用一条 `WHERE status='running'` 覆盖 source 为 `claimed`/`preparing` 的转换。**终态不可逆约束（第八轮 P1-A）**：`done/failed/killed/superseded/recovery_blocked` 是终态，`*→recovery_blocked` 的 source **SHALL NOT 含 `superseded`**——父 execution 已 `superseded` 后，预算耗尽/恢复阻塞 SHALL 落在**当前 recovery child/chain** 上，SHALL NOT 把已终态的父 execution 改写为 `recovery_blocked`。

**🔴 终态无出边，人工恢复只建 child 不改父（第十一轮 P0-A，修第十轮引入的 `recovery_blocked→superseded` 自相矛盾）**：`recovery_blocked` 是 **execution** 终态（`run_queue.status`），**不是 attempt 状态**——attempt（`task_runs.status`）合法终态只有 `succeeded/failed/killed/abandoned/orphaned/superseded`，**SHALL NOT 在 attempt 层出现 `recovery_blocked`**。任何终态（含 `recovery_blocked`）**SHALL NOT 有出边转换**：人工恢复/可靠 reclaim 一律经「**父终态不变 + 原子创建一个 `superseded_from=父` 的 queued recovery child**」实现，**SHALL NOT 把父 `recovery_blocked` 改写为 `superseded`**（否则父会先发 `terminal(recovery_blocked)` 再发 `superseded`，破坏 `late_event_after_terminal` 不变量、使 SSE/progress/失败归因/`final_attempt_id` 层间映射失真）。**`superseded_from` 语义 = 「统一 recovery predecessor（恢复前驱）」，SHALL NOT 推导「父状态一定等于 `superseded`」**——父可以是 `superseded`（交棒/reclaim 中断路径）**或** `recovery_blocked`（人工恢复路径），两种父都能挂唯一 recovery child;单个父的两条路径互斥（要么交棒成功转 superseded、要么无法安全恢复落 recovery_blocked），故 `UNIQUE(superseded_from)` 对每个父至多写一次、不冲突。这样仍只有一套父子恢复链，不需要 `recovered_from` 第二套键。

**事件边界唯一性（第九轮缺陷3）**：`run_claimed` 与 `run_started` SHALL 是两个不同事件——`queued→claimed`（领取、CLI 未起）发 `run_claimed`，只有 `claimed→running`（CLI 真正启动）发 `run_started`。SHALL NOT 让两个转换都发 `run_started`（否则前端收到两个 `run_started` 无从区分「已领取待起」与「已在跑」，也无法解释 attempt 编号未变时的重复）。

**protocol mismatch 的 claim CAS SHALL 带 launch gate/进程树状态条件（第九轮 P0-C）**：`claimed` 阶段发现 protocol mismatch 时，回队与否 SHALL 由 launch gate 是否释放决定——gate 未释放（CLI 未起）→attempt=`abandoned`、execution 回 `queued`（安全）;gate 已释放（CLI 已起、进程未确认退出）→attempt=`orphaned`、execution=`recovery_blocked(process_not_confirmed_dead)`。**claim CAS 谓词 SHALL 排除 `recovery_blocked` execution**（`WHERE status IN ('queued',...) AND status != 'recovery_blocked'` 或正向白名单），使 orphaned execution 不被任何 Worker 重新 claim，直到人工/可靠 reclaim 确认进程树清理。SHALL NOT 让「CLI 已起的 protocol mismatch」回 `queued`。

#### Scenario: 各转换只接受合法 source status
- **WHEN** 表驱动遍历每个合法/非法 source status 触发转换（claimed kill、prestart failure、claim lease 回收 `claimed→queued` 等）
- **THEN** 合法转换唯一命中;每条 CAS 只在其合法 source + owner/generation/instance/current_attempt 条件满足时命中，错误 source/owner/generation/instance 全部 CAS 失败;SHALL NOT 因 CAS 写死 `status='running'` 而漏改或误改

#### Scenario: prestart 终局失败条件含预算耗尽
- **WHEN** `claimed→failed` 的 prestart 终局判定
- **THEN** 条件为 `retryable=false` **OR** (`retryable=true` AND retry/recovery 预算耗尽)，SHALL NOT 写成「`retryable=false` 且达恢复上限」（后者漏掉 retryable=true 耗尽的情形）

#### Scenario: 已 superseded 父不被改写为 recovery_blocked
- **WHEN** 某已 `superseded` 的父 execution，其 recovery chain 预算耗尽或恢复阻塞
- **THEN** `recovery_blocked` 落在**当前 recovery child/chain** 上，父 execution 保持 `superseded` 终态不变，SHALL NOT 把终态父改写为 `recovery_blocked`（终态不可逆）

#### Scenario: running→superseded 校验 instance 与 current_attempt
- **WHEN** 多 Worker 同 cluster epoch 下，某实例试图把一个 `running` execution 转 `superseded`
- **THEN** CAS 除 `worker_generation` 外还 SHALL 校验 `worker_instance_id` 与 `current_attempt_id` 匹配，SHALL NOT 让错误实例定局/交棒别人的 attempt

#### Scenario: claim 只发 run_claimed，CLI 起才发 run_started（第九轮缺陷3）
- **WHEN** 某 execution 从 `queued→claimed`（领取但 CLI 未起），随后 `claimed→running`（CLI 启动）
- **THEN** `queued→claimed` 发 `run_claimed`、`claimed→running` 发 `run_started`，前端据此区分「已领取待起」与「已在跑」;SHALL NOT 两个转换都发 `run_started`、SHALL NOT 出现两个 `run_started` 无从区分

#### Scenario: recovery_blocked execution 不被重新 claim（第九轮 P0-C）
- **WHEN** 调度器扫描可领取集合，其中含一个 `recovery_blocked(process_not_confirmed_dead)` execution（源于 CLI 已起的 protocol mismatch 或 unsafe orphan）
- **THEN** claim CAS 谓词排除 `recovery_blocked`，该 execution 不被任何 Worker 领取;只有人工「确认已清理后重试」或可靠 reclaim（fencing 生效 AND 进程树确认退出）后，才转为可生成 recovery child 或重新排队

#### Scenario: NULL migration 进程清理确认走 cleanup_version CAS、attempt 不改写（第十七轮 P0）
- **WHEN** 一个 `recovery_blocked(null_conversation_migration)` 且 `process_cleanup_state=unconfirmed` 的 running-NULL execution，其进程树经确认完整退出（`pid+create_time` 指纹一致），触发 `confirm_null_process_cleanup()`
- **THEN** CAS（`status='recovery_blocked' AND blocked_reason='null_conversation_migration' AND process_cleanup_state='unconfirmed' AND cleanup_version=expected`）→ `process_cleanup_state=confirmed` + `cleanup_version+1` + 写 `cleanup_confirmed_at/by/evidence_hash`;**execution status 保持 `recovery_blocked` 不变、attempt 保持 `orphaned` 不改写为 `abandoned`、`final_attempt_id` 不变**;SHALL NOT 向已封闭父 SSE 发第二个 `terminal` 事件;展示「已清理」用派生态（orphaned + cleanup_state=confirmed）而非 UPDATE `task_runs.status`

#### Scenario: 两阶段清理并发确认与崩溃恢复（第十七轮 P0）
- **WHEN** 后台巡检与人工并发确认同一 execution 清理;或 `quarantine_null_execution()` 提交后 kill 未执行即崩溃;或 kill 完成、`confirm_null_process_cleanup()` 前崩溃;或 `pid` 复用但 `create_time` 不同
- **THEN** `cleanup_version` CAS 保证并发确认只一个胜出（attempt/final pointer 不变）;事务 A 后崩溃→重启巡检发现 `unconfirmed` 继续清理、不建 successor;确认前崩溃→重启按 `pid+create_time` 指纹重确认后幂等 CAS `confirmed`;`create_time` 不匹配的旧证据 SHALL NOT 确认清理;migration gate 仅在 `confirmed`（或从未 claim 的 queued/无残留进程的 claimed）时放行

### Requirement: 原子 claim（CAS 单语句领取）

系统 SHALL 用单语句条件更新原子领取 queued run：`UPDATE run_queue SET status='claimed', claim_owner=?, claim_generation=?, claimed_at=... WHERE id=(子查询选一条 queued) AND status='queued' AND conversation_id IS NOT NULL RETURNING *`。`AND status='queued'` 的 CAS 条件 SHALL 保证多个 Worker 竞争同一行时至多一个成功。**CAS 谓词 SHALL 含 `AND conversation_id IS NOT NULL`（Review 第十二轮 P0-A，NULL task 不可执行的第三层硬门）**——即便脏数据绕过 dispatch/scheduler 前两层入了队，`conversation_id IS NULL` 的 run 也无法被 claim 成 running;调度器扫描候选与 dispatch 入队时也 SHALL 各自排除 NULL conversation（见两段式 dispatch Requirement 与 [agent-session-resume] 的「可执行 task 必须拥有非 NULL conversation」Requirement），三层任一单独即可挡住。**claim 与 draining/generation 的互斥 SHALL 在同一条 CAS 内校验、SHALL NOT 只靠本地进程标志（Review P1-1）**：claim SQL 的 WHERE 条件 SHALL 同时校验领取者的 `worker_state.state='running'`（非 draining/done）、`claim_generation == 当前活跃 generation`、`owner_instance_id` 匹配、且 lease 未过期，任一不满足则不命中——关闭「旧世代/正在 draining 的 Worker 仍领到新活」的竞态。SHALL NOT 使用「先 SELECT 再无条件 UPDATE」的两步领取。并发上限判断在多 Worker 下 SHALL 使用原子容量机制而非「先 COUNT 再 claim」（单 Worker 阶段可用进程内 semaphore）。

#### Scenario: 并发竞争同一行只有一个成功
- **WHEN** 多个 Worker 同时尝试领取同一个 queued run
- **THEN** CAS 条件使至多一个 UPDATE 命中，该 run 只被一个 Worker 领取，其余落空去领下一条，不重复不遗漏

#### Scenario: claim 即建 attempt 关联
- **WHEN** 一个 execution 被原子领取（进入 claimed）
- **THEN** 同一事务内创建一个新 attempt（`task_runs` 行，`attempt_no` = 该 execution 现有最大 +1）并写 `run_queue_id + attempt_no`（约束 `UNIQUE(run_queue_id, attempt_no)`），建立关联，不再等执行结束才回填

#### Scenario: lease 回收后重领不撞唯一键
- **WHEN** 某 execution 第一次 claim 建了 attempt#1，Worker 在转 running 前崩溃，lease 到期后 execution 回 queued 被再次 claim
- **THEN** attempt#1 落 `abandoned`，第二次 claim 创建 attempt#2（`UNIQUE(run_queue_id, attempt_no)` 不冲突），execution 正常重新执行，最终终态由定局 attempt 决定

#### Scenario: 瞬时失败重试保留各 attempt
- **WHEN** 同一 execution 连续两次瞬时失败后重试
- **THEN** attempt#1/#2/#3 各保留为独立 `task_runs` 行（不互相覆盖），execution 终态由最终定局 attempt 决定，SSE 事件全部归入同一 execution 事件流

#### Scenario: NULL conversation 脏数据不被 claim 成 running（Review 第十二轮 P0-A）
- **WHEN** 构造一条 `conversation_id IS NULL` 的 queued 行（绕过 dispatch/scheduler 前两层硬门的脏数据），调度器尝试领取
- **THEN** claim CAS 谓词 `AND conversation_id IS NOT NULL` 使其永不命中，该 NULL run 无法被任何 Worker claim 成 running;三层硬门（dispatch 拒绝 / scheduler 排除 / claim CAS 排除）任一单独即可挡住，NULL task 零执行

### Requirement: 两段式 dispatch（提交与订阅分离）

系统 SHALL 把所有触发（人工 @、auto-dispatch、mention、leader 协同）统一经持久化队列，采用两段式协议：① `POST /tasks/{id}/dispatch` 接收客户端 idempotency key，**同一事务**内幂等持久化用户消息 + 创建 `queued` execution、提交后返回稳定 `execution_id`，SHALL NOT 在请求内直接执行 CLI;**创建 `queued` execution 前 SHALL 校验目标 task `conversation_id IS NOT NULL`（Review 第十二轮 P0-A，NULL task 不可执行的第一层硬门），为空 SHALL 拒绝入队并返回结构化人工迁移原因，SHALL NOT 入队一个 NULL conversation 的可执行 run**;调度器/auto-dispatch 扫描候选 task 时 SHALL 在查询谓词内 `AND conversation_id IS NOT NULL`（第二层硬门）;② `GET /executions/{execution_id}/events` 独立 SSE 订阅，按 execution_id 尾随，API 重启后可重新订阅。idempotency 作用域 SHALL 为 `UNIQUE(task_id, actor_id, idempotency_key)`;相同 key 重试 SHALL 只产生一条用户消息与一个 execution;同 key 不同 payload SHALL 返回 409。

**🔴 conversation 校验与 message/execution 写入 SHALL 用同一原子快照（Review 第十三轮 P1-B，防先查后插 TOCTOU）**：「校验 `conversation_id IS NOT NULL`」与「写 user message + 插 run_queue」若分两步（先 SELECT 校验、再 INSERT），中间 task 可能被迁移/隔离把 `conversation_id` 改为 NULL 或换成新 conversation，导致 task 当前 conversation、message conversation、execution conversation 三者不一致，而 claim 只查 `run_queue.conversation_id IS NOT NULL` 仍会执行这个陈旧快照。故 SHALL：
1. **条件插入而非先查后插**——用单事务条件 INSERT 从 `tasks` 现值取 conversation 快照，为空则零行插入（等价拒绝）：
```sql
INSERT INTO run_queue (..., conversation_id, ...)
SELECT ..., tasks.conversation_id, ...
FROM tasks
WHERE tasks.id = :task_id
  AND tasks.conversation_id IS NOT NULL;
```
2. **user message 与 run_queue SHALL 用同一 `conversation_id` 快照并同事务提交**——两者写入同一个从 `tasks` 读到的 conversation 值，SHALL NOT 各自读一次。
3. **🔴 execution 的 conversation 快照创建后永久不可变（用户拍板，第十三轮 P1-B）**——`run_queue.conversation_id` 一经写入即冻结，后续 task conversation 变化 SHALL NOT 改写已存在 execution 的快照。
4. **task 存在 active execution（queued/claimed/running）时 SHALL 禁止迁移/修改其 `conversation_id`**——迁移 SHALL 用 CAS 断言「该 task 无 active execution」（`WHERE NOT EXISTS(active execution)`），否则拒绝迁移或要求先排空;这与「execution 快照不可变」互补，防止迁移与在跑 execution 打架。
5. **claim SHALL 只信 execution 自身冻结快照**——claim CAS 校验 `run_queue.conversation_id IS NOT NULL`（第三层硬门）即可，因快照不可变、且 active execution 期间 task 不可迁移，无需再回查 task 当前 conversation;SHALL NOT 出现「execution 快照与 task 当前 conversation 不一致仍执行」（该不一致已被步骤 3/4 从源头杜绝）。
6. 人工迁移、dispatch、auto-dispatch 三方并发 SHALL 有专门故障注入测试（见测试矩阵）。

#### Scenario: 提交不在请求内执行
- **WHEN** 用户人工 @ 一位成员触发执行
- **THEN** API 同事务写消息+queued execution、提交后返回 execution_id，不在 POST 请求内同步跑 CLI；执行由 Worker 领取，API 重启不影响该执行

#### Scenario: POST 幂等
- **WHEN** 相同 idempotency key 的 dispatch 请求被重试（网络抖动/前端重发）
- **THEN** 系统只产生一条用户消息与一个 execution，不重复入队

#### Scenario: dispatch 校验后并发置 NULL 只能一方成功（第十三轮 P1-B）
- **WHEN** dispatch 读到 `tasks.conversation_id=100` 校验通过后暂停，事务 B 并发把该 task 的 `conversation_id` 改为 NULL/新 conversation，dispatch 再继续写 message/run_queue
- **THEN** 因用条件 `INSERT...SELECT ... WHERE tasks.conversation_id IS NOT NULL` 单事务取快照，两方 SHALL 只有一方成功——要么 dispatch 在迁移前以 conversation=100 完成、要么迁移先发生使 INSERT 零行（等价拒绝）;SHALL NOT 出现 message 与 run_queue 指向不同 conversation 的陈旧快照

#### Scenario: message 与 run_queue 用同一 conversation 快照
- **WHEN** dispatch 与「迁移到新 conversation」并发
- **THEN** 同一次 dispatch 写入的 user message 与 run_queue 必须指向**同一个** conversation 快照值、同事务提交，SHALL NOT 各自读一次而分裂

#### Scenario: task 有 active execution 时禁止迁移 conversation
- **WHEN** 某 task 存在 active execution（queued/claimed/running），此时尝试迁移/修改其 `conversation_id`
- **THEN** 迁移 CAS 断言「无 active execution」不成立 → 迁移被拒或要求先排空;已存在 execution 的 `run_queue.conversation_id` 冻结不可变，SHALL NOT 被迁移改写

#### Scenario: 迁移竞态下同 idempotency key 不产生跨 conversation 重复
- **WHEN** 同一 idempotency key 在迁移竞态中重试
- **THEN** `UNIQUE(task_id, actor_id, idempotency_key)` 仍保证只一条 message 与一个 execution，SHALL NOT 因 conversation 快照变化产生跨 conversation 的重复 message/execution

### Requirement: 子进程 containment（Worker 死则 CLI 死）

系统 SHALL 保证 CLI 子进程不因 Worker 崩溃而成为继续运行的孤儿，**且 SHALL 区分「containment（父死清理）」与「CAS 前启动闸门（挡 CAS 前用户代码执行）」两个独立问题**（Review 第六轮 P1-4）：
- **Windows**：SHALL 用 Job Object + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 做 containment，且 SHALL 按 `CREATE_SUSPENDED` → `AssignProcessToJobObject` → （generation CAS）→ `ResumeThread` 顺序创建，兼做 containment 与 CAS 前启动闸门，杜绝「已跑起但未进 Job」及「CAS 前已执行用户代码」的逃逸窗口。
- **POSIX**：containment SHALL 用真实机制——`PR_SET_PDEATHSIG` + cgroup（或 systemd scope `KillMode=control-group`）做进程树全清理，**SHALL NOT 仅用 `start_new_session` 独立进程组冒充完整 containment**;CAS 前启动闸门 SHALL 用 launcher 阻塞于 pipe/eventfd gate、CAS 成功后才 `exec` 真实 CLI（见「恢复 CLI 前的 generation 最终启动围栏」Requirement 第 2 步）。`start_new_session`/父死 containment **SHALL NOT** 被当作 suspended launch 的等价物。未落地该 gate 的平台 readiness SHALL fail-closed 不放行。

系统 SHALL 持久化 `pid + pid_create_time + worker_generation`（不只存 pid、不只在内存），使重启后可比对进程身份。恢复中断的 run 前系统 SHALL 先确认旧进程树已清理；无法证明旧执行已停止时 SHALL NOT 创建 recovery child（宁可不续，不可双执行），并以 generation fencing 作为兜底。

#### Scenario: Worker 崩溃后无孤儿 CLI
- **WHEN** Worker 进程崩溃退出
- **THEN** 其启动的 CLI 子进程树被 OS（Job Object / 进程组机制）连带清理，不留下继续运行的孤儿进程

#### Scenario: 无法证明旧进程已停则不续跑
- **WHEN** 恢复某中断 run 前，无法确认其旧 CLI 进程树已被清理
- **THEN** 系统不创建 recovery child，避免旧 CLI 与恢复 CLI 双执行；generation fencing 兜底拒绝旧进程写平台

### Requirement: Worker generation 与交棒 ack

系统 SHALL 用单调递增的 Worker generation 标识执行世代，持久化于 `worker_state` 单行表（current_generation/owner_instance_id/state/heartbeat_at/lease_expires_at/protocol_version）、每个 `task_runs.worker_generation` **与 `task_runs.worker_instance_id`**（Review P1-2：fencing 要比对 owner instance，故 attempt 行须同时持久化 generation 与 instance id，并注入 `jian` 执行环境供写平台时校验）。系统 SHALL 支持两类接管，共用同一 generation 机制：① **优雅交棒（有 ack）** 走 3 态 `running(g) → draining(g)`（旧 Worker 停领、杀在跑 CLI、supersede + 入队 recovery child、置 done）`→` 新 Worker 确认旧 generation 为 done 后 `g+1` 接管;② **硬崩溃接管（无 ack）** 旧 Worker 未置 done 即死，新 Worker SHALL 以 `lease_expires_at < now` 为据、用 CAS `WHERE current_generation=g AND lease_expires_at<now SET current_generation=g+1, owner_instance_id=<new>` 抢占接管（并发拉起时 CAS 保证唯一接管者），SHALL NOT 无限干等 done。**lease 时间语义 SHALL 用数据库时间为准**（Review P1-6，避免 Worker 与 DB 系统时钟漂移误判），并明确定义 `heartbeat_interval`、`lease_duration`（= 若干倍 heartbeat_interval）、允许的短暂暂停窗口（DB 卡顿/GC），使短暂停顿不触发误接管;心跳 SHALL 周期推进 `lease_expires_at`;`protocol_version` 与 DB schema 不匹配时 SHALL fail-closed 不接管。fencing 校验 SHALL 同时比对 `worker_generation` 与 `owner_instance_id`，防 pid/generation 复用误判。claim 与 draining 检查 SHALL 在同一受保护决策点完成（draining 后不再 claim），关闭「停领与新任务刚进」的竞态。

#### Scenario: 新 Worker 确认旧世代 done 才接管（优雅交棒）
- **WHEN** 旧 Worker 进入 draining 并完成 kill/supersede/入队 recovery child、置 generation 为 done
- **THEN** 新 Worker 读到 done 后才升 generation 接管领取，不与旧 Worker 并发持有同一 run

#### Scenario: 硬崩溃 lease 过期抢占接管
- **WHEN** 旧 Worker 未置 done 即崩溃（心跳停摆，`lease_expires_at` 已过期），一个或多个新 Worker 被拉起
- **THEN** 新 Worker 以 lease 过期为据 CAS 抢占接管，只有一个成功升 generation，其余失败退让；接管后按「先 fencing 再判死再续跑」处理残留 running run

#### Scenario: draining 后不再领新活
- **WHEN** Worker 已进入 draining 状态
- **THEN** 该 Worker 不再 claim 新 run，避免「正在停机却又领了新任务」的竞态

### Requirement: `jian` 平台写的 attempt 级 fencing（不止 generation）

系统 SHALL 对 `jian` 平台写采用 attempt 级 fencing，SHALL NOT 仅凭 `generation == 当前活跃 generation` 放行（Review 第七轮 P0-4：只看 generation 挡不住同世代旧 attempt）。以下场景仍处于同一 generation，只看 generation 会放行：① attempt#1 retryable failure 后 execution 回 `queued`，attempt#1 的残留线程迟到写;② attempt 已 `killed`/terminal/`superseded`，但 Worker 尚未升 generation;③ 温和交棒先杀 CLI、再 supersede/入 child、最后才置 generation done，若 kill 不完整，残留 CLI 在 generation 递增前仍携带当前 generation。这些旧 attempt 都可能通过 generation 检查写任务/评论/消息/状态。

事件流已要求 owner/generation/status 三重校验，`jian` 平台写接口 SHALL 采用同等级 fencing——**两层都必须满足**才放行：
```text
worker_generation            == current_generation          -- 属于当前世代
worker_instance_id           == current owner_instance_id    -- 属于当前 Worker 实例
task_runs.status             == 'running'                    -- attempt 仍在跑
run_queue.status             == 'running'                    -- execution 仍在跑
run_queue.current_attempt_id == task_run_id                  -- 是当前合法 attempt
session/attempt owner token 仍匹配（需要 session 写时）
```
generation + instance 证明「属于当前 Worker」，attempt/execution/current pointer 证明「仍是当前合法执行」。任一不满足 SHALL 拒绝该平台写并隔离审计。

**🔴 交棒/reclaim child 的启动前置 = 「fencing 生效 AND 完整进程树确认退出」双条件（Review 第八轮 P0-B，收紧上一轮的「或先完成 generation fencing」）**：generation/attempt fencing **只能阻止经过 `jian` 的平台写**（comment/subtask/status/message、session pointer/committed 等），**挡不住残留 CLI 改项目文件、执行 shell、调用不经 JianAgency 的内外部 API、继续占用锁/端口/子进程/本地资源**。因此 SHALL 用 **AND** 条件而非 OR：
```text
旧 attempt/generation fencing 已生效
AND
完整进程树已确认退出 / containment 已确认清理（所有后代进程，非只根 PID）
THEN 父 execution 才允许 superseded + recovery child 入队
```
任一未满足 SHALL：`execution=recovery_blocked`、`blocked_reason=process_not_confirmed_dead`、**不建 child**、保留 `pid / pid_create_time / worker_generation / worker_instance_id / containment 句柄信息`，由人工确认清理后再创建 recovery child。SHALL NOT 仅凭 generation fencing 就 supersede + 建 child。

#### Scenario: 同世代旧 attempt 迟到写被拒
- **WHEN** attempt#1 已 terminal（retryable failed 回队 / killed / superseded）但 Worker 尚未升 generation，attempt#1 残留线程调用 `jian` 写平台
- **THEN** 尽管 `worker_generation` 仍匹配当前 generation，因 `task_runs.status`/`run_queue.status` 非 running 或 `run_queue.current_attempt_id != task_run_id`，平台写被拒绝并隔离审计，不污染任务/评论/消息/状态

#### Scenario: retry 残留线程不写平台
- **WHEN** attempt#1 retryable failure 后 execution 已回 `queued`，attempt#1 残留线程迟到调用 `jian`
- **THEN** `run_queue.status='queued'`（非 running）且 `current_attempt_id` 已 NULL，attempt 级 fencing 拒绝该写入

#### Scenario: 交棒 kill 未确认进程树退出不建 child（AND 双条件）
- **WHEN** 温和交棒杀 CLI 时进程树终止失败/超时，无法确认残留 CLI 已退出（即便 generation fencing 已生效）
- **THEN** 系统 SHALL NOT supersede + 建 recovery child（fencing 生效不足以放行），SHALL 落 `recovery_blocked`(`blocked_reason=process_not_confirmed_dead`)、保留 pid/create_time/generation/instance/containment 信息，由人工确认清理后再建 child——因 fencing 挡不住残留 CLI 改文件/执行 shell/调外部 API/占资源

#### Scenario: 残留进程绕过 jian 写文件证明 fencing 不足
- **WHEN** 交棒后残留 CLI 未被清理，持续写 sentinel 文件/调用外部 API（不经 jian）
- **THEN** generation fencing 无法阻止这些外部副作用;只有完整进程树确认退出后才允许恢复 child，验证 child 启动前置必须是「fencing AND 进程树退出」而非仅 fencing

#### Scenario: 人工确认清理后恰好创建一个 child
- **WHEN** 处于 `recovery_blocked`(`process_not_confirmed_dead`) 的 run，人工确认旧进程树已清理并触发恢复
- **THEN** 系统才创建恰好一个 recovery child（`superseded_from` 幂等），确认前 SHALL NOT 自动恢复

#### Scenario: containment 验证所有后代退出
- **WHEN** Windows Job Object / POSIX cgroup·scope 下 CLI 派生了孙进程，交棒杀树
- **THEN** 确认退出 SHALL 覆盖所有后代进程（非只根 CLI PID），孙进程仍存活即视为「进程树未确认退出」→ `recovery_blocked` 不建 child

#### Scenario: 需 session 写时校验 owner token
- **WHEN** 平台写涉及 session pointer/committed 等 session 状态
- **THEN** 除 generation+instance+attempt/execution/current pointer 外，还 SHALL 校验 session/attempt owner token 仍匹配，退休 owner 的迟到 session 写被拒

### Requirement: API 层与执行层解耦

系统 SHALL 将 Agent 执行(队列消费、CLI 子进程、并发池)从 API 进程剥离到独立的 Worker 进程。API 进程 SHALL NOT 直接执行 Agent,只负责 HTTP/API、往队列塞任务、尾随日志转发流式、写 kill/交棒标记。Worker 进程 SHALL 负责领取队列、起 CLI、跑 Agent、落终态、孤儿回收。API 进程 SHALL 无执行态(状态全在 DB 与 Worker),从而可独立重启。

#### Scenario: 重启 API 不中断 Agent
- **WHEN** 一个 Agent 正在 Worker 进程中执行,此时 API 进程被停止并重启(模拟改代码热更新)
- **THEN** 该 Agent 的 CLI 子进程继续运行、run 不被判死、执行结果正常落库,不需要重跑

#### Scenario: API 只塞队列不直接执行
- **WHEN** 用户/Agent 触发一个新的执行请求
- **THEN** API 进程将其入队(run_queue),由 Worker 进程领取执行;API 进程内不存在跑 Agent 的协程或 CLI 子进程

### Requirement: 调度状态外置以支持多进程

系统的并发调度状态 SHALL 外置到数据库,不依赖单进程内存。并发计数 SHALL 由 DB 查询得出(`run_queue`/`task_runs` 的 claimed+running 计数)。run 领取 SHALL 用「原子 claim（CAS 单语句领取）」Requirement 定义的方式（**非**现状「SELECT + 无条件 UPDATE」两步），使多个 Worker 进程可安全竞争同一队列而不重复领取、不丢任务。执行 run 的 `pid + pid_create_time + worker_generation` SHALL 落库,跨进程可见且可校验进程身份。

#### Scenario: 多进程不重复领取
- **WHEN** 多个 Worker 进程同时从共享队列领取待执行 run
- **THEN** 每个 queued run 至多被一个 Worker 领取执行,不重复、不遗漏

#### Scenario: 并发计数不依赖内存
- **WHEN** 判断是否达到并发上限
- **THEN** 计数来自 DB 的 running 状态查询,而非某个进程的内存集合,重启进程不丢计数

### Requirement: 跨进程终止执行（kill）

当 API 进程需要终止一个由 Worker 进程启动的 Agent 执行时,系统 SHALL 通过持久化的终止请求(kill 标记)协调,由持有该 CLI 子进程的 Worker 执行实际终止。kill 请求 SHALL 持久化生命周期字段 `request_id / target_task_run_id / target_generation / state(requested→acked→done) / acked_at / outcome`。Worker 执行 kill 前 SHALL 校验 `target_generation == 当前活跃 generation`——世代不符（针对已被交棒/恢复取代的旧 run）的 kill 请求 SHALL 作废不执行，避免旧 kill 标记误伤后续恢复 run。终止 SHALL 杀整棵子进程树(Windows `taskkill /F /T`),并 SHALL 沿用 pid + 进程创建时间指纹校验,防止 pid 复用导致误杀无辜进程。kill 是用户主动终止,SHALL NOT 触发 resume 续跑(区别于交棒式重启)。

#### Scenario: API 请求 kill 由 Worker 执行
- **WHEN** 管理员在 API 侧请求终止某个正在 Worker 执行的 run
- **THEN** API 写入持久化 kill 标记,Worker 读到后杀该 run 的进程树并落终态,run 状态正确变为终止态,不生成续跑 run

#### Scenario: pid 复用不误杀
- **WHEN** 执行终止前,目标 pid 已被操作系统回收并复用给另一无关进程
- **THEN** 创建时间指纹比对不一致 → 拒绝终止该 pid,不误杀无辜进程

#### Scenario: 过期世代的 kill 请求不误伤恢复 run
- **WHEN** 一个针对已被交棒/恢复取代的旧 run 的 kill 请求，其 `target_generation` 不等于当前活跃 generation
- **THEN** Worker 校验世代不符，作废该 kill 请求不执行，不误杀已接管的恢复 run

### Requirement: 流式输出尾随 execution_events 且可续传

API 的 SSE 端点 SHALL 把输出近实时推送给前端而非直连 CLI stdout。**续传游标 SHALL 用统一事件表 `execution_events(id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id, event_type, payload_json, created_at)` 的全局自增 `id`，SHALL NOT 用 `run_logs.id`，SHALL NOT 用 `SELECT MAX(seq)+1` 自算 per-execution seq**（并发写会竞争/撞唯一键）——控制事件（queued/**run_claimed**/run_started/**retry_scheduled**/superseded/**recovery_resumed**/terminal）不写在 run_logs，用 log id 做游标会导致断线期间的控制事件（如 superseded 跳转）重连后漏投或错序;全局自增 id 由引擎原子分配，execution 内有间隙不影响单调性。**`recovery_resumed` 是统一恢复控制事件（Review 第十三轮 P1-C，替代 `manual_recovery` 第二事件名——SHALL NOT 再保留 `manual_recovery` 作为独立 event_type）**：人工恢复与自动 reclaim 恢复**共用同一个** `recovery_resumed{source: manual|reclaim, recovery_reason, actor?, recovery_parent_id, child_execution_id, blocked_reason?, request_token?}` 事件，`source` 区分来源、`recovery_reason(handover|orphan_reclaim|manual_blocked_resume)` 区分承接语义（第十七轮 P2-B）;**reclaim 交棒承接 `superseded` 父时无 `blocked_reason`（该字段缺省/null），仅承接 `recovery_blocked` 父时 `blocked_reason` 必填**;它是 **child 事件流的控制事件、进入统一 event_type 真相源**（前端消费者、回放协议、事件事务探针一并覆盖），SHALL NOT「写 child SSE 流 或 写独立审计表」两种实现并存——统一走 child SSE 控制事件（若另需持久审计，SHALL 作为该控制事件的落库派生、非第二套 event_type）。`run_claimed`（领取、CLI 未起）与 `run_started`（CLI 启动）SHALL 是两个不同事件（第九轮缺陷3），SHALL NOT 让 `queued→claimed` 与 `claimed→running` 都发 `run_started`。**`retry_scheduled` 为 attempt 边界控制事件（Review 第七轮 P1-6）**：同一 execution 多 attempt 共用一条事件流，attempt#N failed 回队时 SHALL 与 attempt 状态转换**同事务**写 `retry_scheduled{attempt_id, attempt_no, failure_stage, failure_class, retryable, next_retry_at}`——否则前端只会先后看到两个 `run_started` 而无法解释中间的失败、退避与 attempt 编号变化;execution terminal event 只在 execution 真正落终态时出现。所有 SSE 事件（log 与全部控制事件）SHALL 先落 `execution_events` 再推，`id: <全局id>` 为唯一游标;`run_logs` SHALL 加 `meta_json` 列承载 log 事件的结构化附加信息（channel/tool/tool_input/tool_output），log 事件可从 run_logs + meta 完整重建。**状态/数据写入与其对应事件写入 SHALL 同事务提交**（POST 消息+queued execution+queued event；queued→claimed+run_claimed event；claimed→running+run_started event；run_log+log event；终态+terminal event；superseded+recovery child+superseded event+child `recovery_resumed{source=reclaim}` event+child queued event），SHALL NOT 分离提交而出现「有日志无 event」「已终态无 terminal event」「有 recovery child 无 superseded 跳转」「有 recovery child 无 recovery_resumed」。CLI 输出写 `run_logs` 的**调用入口与日志语义 SHALL 保持不变**(Worker 线程 → `_log()`)，但因 run_log 行与其 log event SHALL 同事务提交，`_log()` 内部落库 SHALL 改为「同事务写 run_logs + execution_events」，SHALL NOT 表述为「整条路径零改动」（Review 第四轮 P1-4）。SSE 断连重连 SHALL 携带 `Last-Event-ID: <id>` 从该 id 之后回放。**切换到 successor execution 时游标 SHALL 沿用同一条全局 `id` 序列、SHALL NOT 声称「successor 有独立 id 序列」**（Review 第四轮 P0-5）——`execution_events.id` 是全局单调自增，不存在 per-execution 独立序列。supersede 时事件插入顺序 SHALL 固定为「父 CAS 转 `superseded` 并清 active → 建 recovery child execution → 写父 execution 的 `superseded{successor_execution_id}` event → 写 child 的 `recovery_resumed{source: reclaim}` event → 写 child 的 `queued` event → 提交」（**第十五轮 P1-1：自动 supersede/reclaim 建的 recovery child SHALL 同人工恢复一样发统一 `recovery_resumed`，只是 `source=reclaim` 不带人工 actor;此写序为全 change 唯一真相源，与 `finish_execution()` 收尾写序、design 双写同事务组合、tasks 1.6/1.6a/1.6c 逐字一致**），使 child 的 `recovery_resumed`/`queued` event 全局 id **必然大于**父 superseded event 的 id;客户端收到父 superseded 后订阅 child 时 SHALL 继续携带当前全局 `Last-Event-ID`，服务端按 `WHERE execution_id=:child AND id > :last_global_id ORDER BY id` 即可无损获得 child 的 recovery_resumed/queued 及后续事件，SHALL NOT 因「切 execution」把游标重置或漏取 child 首事件。**`recovery_resumed` event 的 payload 字段 SHALL 用 `source`（manual|reclaim），SHALL NOT 用 `recovery_source`——`recovery_source` 仅为 HTTP 响应体字段名，SHALL NOT 混进 SSE event payload（第十五轮 P1-1）**。（若产品选择 child 订阅不带 Last-Event-ID、完整回放 child，则 SHALL 明确幂等去重规则，两种口径不并存。）**terminal/superseded 后 SHALL 封闭旧 execution/attempt 的事件流（Review 第五轮 P1-8）**：终态或 superseded 事件写入后，对旧 attempt 的任何 log/control event append SHALL 再做 `owner/generation/status` CAS 校验——CAS 失败（该 attempt 已非当前 owner 或 execution 已终态）即丢弃该 event 或写入隔离审计表，SHALL NOT 进入用户事件流。否则迟到的父日志可能在 child queued 之后取得更大的全局 id，虽不漏数据但前端会看到终态后的父输出、甚至被错误消费者当成有效结果。

**🔴 `recovery_blocked` 父的人工恢复 SHALL NOT 向已封闭的父事件流追加任何事件（含 `recovery_resumed`），发现 child 走「POST 响应 + 查询」而非父流迟到事件（Review 第十二轮 P0-B，修第十一轮引入的封流冲突）**：`superseded` 是父的**最后一个**事件、且它带 `successor_execution_id`，故交棒/reclaim 路径的客户端能在父流内收到 superseded 后按全局游标切 child（见下方 supersede scenario）;但 **`recovery_blocked` 是终态、父流在 `terminal(recovery_blocked)` 后已永久封闭**，人工恢复发生在父终态**之后**，此时 SHALL NOT 再向父流写恢复事件（会破坏「终态是父流最后一个事件」的封流不变量、且父终态后关闭订阅的客户端也收不到）。因此：① **统一的 `recovery_resumed{source: manual|reclaim, recovery_reason, actor?, recovery_parent_id, child_execution_id, blocked_reason?, request_token?}` SHALL 作为 recovery child 事件流的控制事件**（进入统一 event_type 真相源、绑 child 的 execution_id，见 SSE Requirement 的写序;`recovery_reason` 必填、`blocked_reason` 仅承接 recovery_blocked 父时必填，第十七轮 P2-B），SHALL NOT append 到父 execution 的事件流、SHALL NOT 保留 `manual_recovery` 第二事件名（Review 第十三轮 P1-C）;② 触发恢复的调用端 SHALL 从 **`POST /executions/{parent_id}/resume` 响应体**直接得到 `child_execution_id`，据此订阅 child 流（`GET /executions/{child_id}/events`），不依赖父流迟到事件;③ 未持有 POST 响应的其他客户端 SHALL 经 **`GET /executions/{parent_id}` 查询** 得到 `recovery_child_id`/`recovery_resolution` 发现 child。父流封闭不变量对所有终态（含 `recovery_blocked`）一致成立。

#### Scenario: 尾随近实时呈现
- **WHEN** CLI 持续产出输出、Worker 写入 run_logs 并同事务落 log 类 execution_events
- **THEN** 前端经 SSE 近实时(亚秒级延迟)逐段看到新输出,体验与直连 stdout 基本一致

#### Scenario: SSE 断点无损续传（含控制事件）
- **WHEN** API 进程重启或网络抖动导致 SSE 断开,前端携带 `Last-Event-ID: <id>` 重连
- **THEN** 服务端 `WHERE execution_id=? AND id > ? ORDER BY id` 回放,log 与控制事件统一有序、不重复已收、不遗漏（含断线期间发生的 superseded/terminal）

#### Scenario: 事件与状态同事务不半提交
- **WHEN** run 落终态后、写 terminal event 前进程崩溃
- **THEN** 因终态转换与 terminal event 在同一事务，崩溃时整体回滚，不出现「run 已终态但 SSE 永远收不到 terminal」的半提交状态

#### Scenario: 并发写事件游标不冲突
- **WHEN** API、Worker、日志线程、终态线程同时向 execution_events 写入
- **THEN** 全局 AUTOINCREMENT id 原子分配，各写入拿到不同单调 id，不冲突、不乱序、不撞唯一键

#### Scenario: supersede 切 successor 用全局游标无损续订
- **WHEN** 父 execution 断线期间发生 supersede，同事务按「建 child → 写父 superseded event → 写 child queued event → 提交」落库，客户端重连后收到父 `superseded{successor_execution_id}` 并携带当前全局 `Last-Event-ID` 订阅 child
- **THEN** 因 child queued event 的全局 id 必然大于父 superseded event id，服务端 `WHERE execution_id=child AND id > last_global_id` 恰好取到 child 的 queued 及后续事件，全局 id 单调、child queued/log/terminal 各恰好一次，不因切 execution 重置游标或漏取 child queued

#### Scenario: 中途进入回放已结束 run
- **WHEN** 用户在某 run 已终态后进入其详情
- **THEN** 一次性回放该 run 的全量输出与收尾态,不需要实时尾随

#### Scenario: 终态后旧 attempt 迟到日志不进用户流
- **WHEN** 某 attempt 已 terminal/superseded，其 CLI 残留线程仍尝试 append log event
- **THEN** 该 append 的 owner/generation/status CAS 失败，event 被丢弃或写隔离审计，用户事件流中不出现终态后的旧 attempt 日志

### Requirement: 恢复 CLI 前的 generation 最终启动围栏

claim 校验当前 generation、`claimed→running` 用 attempt/run 行的 owner/generation CAS 之外，从 claim 到实际创建 CLI 之间仍存在时间窗——**旧 Worker claim 成功 → 心跳暂停/lease 过期 → 新 Worker 接管 generation → 旧 Worker 恢复并启动 CLI**（Review 第五轮 P0-3）。若 `claimed→running` 只校验旧 Worker 自己此前写入的 generation、不在启动临界区重读当前 `worker_state`，旧 Worker 仍可能把 CLI 跑起来;平台写 fencing 能挡脏写，但**挡不住 CLI 自身的文件/工具/外部系统副作用**。系统 SHALL 把恢复/启动 CLI 固定为一个不可省略的顺序，任一步失败即中止本次启动：

1. **启动前重校验**：起进程前 SHALL 重新读取数据库当前 `worker_state` 的 generation、owner instance、worker lease 与该 attempt 的 attempt lease;任一不匹配 SHALL 立即 self-fence（不启动、不领取、本 generation 停止一切副作用动作）。
2. **CAS 前启动闸门（挂起/gate）+ containment**：SHALL 在 CAS 转 running **之前**让子进程停在「尚未执行真实 CLI 用户代码」的挂起点，并完成进程树 containment，SHALL NOT 先跑起来再补 containment。**两平台的挂起机制不同、SHALL 分别落地（Review 第六轮 P1-4：containment 与 launch barrier 是两个问题，父死 containment 不等价于 suspended 启动）**：
   - **Windows**：以 `CREATE_SUSPENDED` 创建子进程、加入 Windows Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 全树清理），CAS 成功后 `ResumeThread`。
   - **POSIX**：launcher 子进程 `fork` 后先阻塞在**匿名 pipe/eventfd gate** 上、**尚不 `exec` 真实 CLI**;父进程完成 cgroup/`PR_SET_PDEATHSIG`/systemd scope containment 并持久化 pid、做 generation CAS;CAS 成功后父进程释放 gate、launcher 才 `exec` CLI，CAS 失败则关闭/销毁整个 scope。`start_new_session`/父死 containment **SHALL NOT** 冒充 suspended launch——它们只解决父死后清理，挡不住 `exec` 后、CAS 前的用户代码执行。
   - 若某平台（如当前 POSIX）尚未实现该 gate，SHALL 在 readiness 显式 fail-closed 不放行该平台的 Worker，SHALL NOT 用父死 containment 顶替后放行;不阻塞已具备 gate 的 Windows 阶段 2。
3. **CAS 转 running**：SHALL 用 CAS 持久化 `pid + pid_create_time` 并把 attempt/execution 转 `running`，该 CAS **同时校验当前 `worker_state` 的 generation/instance/lease** 仍属本 Worker;校验不过则不转。
4. **仅 CAS 成功才放行闸门**：只有第 3 步 CAS 成功才 SHALL 放行启动闸门（Windows `ResumeThread` / POSIX 释放 pipe·eventfd gate 让 launcher `exec`）;CAS 失败 SHALL 立即销毁该挂起进程树/整个 scope，绝不让其执行用户代码。
5. **续租失败分级 self-fence（Review 第六轮 P1-8：区分单次心跳失败与租约失效，避免 DB 短抖动大面积误杀）**：SHALL 分三级处置，SHALL NOT 一次心跳 UPDATE 失败就杀掉本 generation 全部 CLI：① **单次/短时心跳失败**——立即停止新 claim / 新 launch（不再扩大副作用面），但**不杀在跑 CLI**;② **在本地单调时钟的安全截止点前重试续租**——续租成功则恢复 claim/launch;③ **已无法证明数据库租约仍有效（重试至安全截止点仍失败）**——才终止本 generation 所持全部进程并退出，SHALL NOT 继续依赖本地缓存的 generation 运行。

**本地安全截止点的换算公式 SHALL 明确、并保守扣除续租 RTT（Review 第七轮 P1-4 + 第八轮 P1-E）**：DB lease 用 DB time、self-fence 用本地 monotonic，两种时钟不能直接比较。每次成功续租 SHALL 在请求前后各采一次本地单调时钟，连同 DB 返回值记录四元组：
```text
local_mono_before_request   -- 发送续租请求前的本地单调时钟
local_mono_after_response   -- 收到 DB 响应后的本地单调时钟
db_now                      -- DB 侧当前时间（随响应返回）
db_lease_expires_at         -- DB 侧租约到期时间
```
保守映射 SHALL 以**请求前采样点**为基准、并至少扣除完整 RTT 与 safety_margin（把网络/排队耗时算作已消耗，不高估本地可运行窗口）：
```text
rtt              = local_mono_after_response - local_mono_before_request
local_safe_deadline = local_mono_before_request
                    + (db_lease_expires_at - db_now)   -- 剩余租约时长（DB 时钟测量）
                    - rtt                              -- 保守扣除完整往返耗时
                    - safety_margin                    -- 时钟偏移+调度抖动+进程终止预算
```
判定 SHALL 用本地单调时钟与 `local_safe_deadline` 比较，SHALL NOT 用可回拨的墙钟、SHALL NOT 直接拿墙钟与 `db_lease_expires_at` 比、SHALL NOT 在响应后采样却直接相加完整剩余租期（会高估窗口）。系统 SHALL 定义 `safety_margin`（≥ 最长 DB/调度抖动 + 进程终止预算）、`heartbeat_interval`、`lease_duration`。**验收语义 SHALL 表述为「不会晚于安全截止点 self-fence，也不会无理由大幅提前」**，SHALL NOT 写成「截止点严格早于 lease 但 DB lease 有效时绝不早杀」这种字面不可同时满足的要求。用 fake clock 测试 DB/本地时钟偏移 + 高 RTT + GC pause 下的行为。

#### Scenario: 启动临界区切世代旧 CLI 零执行
- **WHEN** 旧 Worker claim 成功后心跳暂停、新 Worker 接管 generation，随后旧 Worker 恢复并尝试启动 CLI
- **THEN** 旧 Worker 在 suspended 创建后的 CAS 转 running 阶段发现当前 `worker_state` generation 已变，CAS 失败并销毁 suspended 进程树，旧 generation 的 CLI 用户代码从未开始执行

#### Scenario: POSIX launcher 在 CAS 前阻塞于 gate 不 exec
- **WHEN** POSIX 部署下 launcher 子进程 fork 后，在父进程完成 generation CAS 前，其间发生 generation 切换（CAS 失败）
- **THEN** launcher 全程阻塞在 pipe/eventfd gate 上、从未 `exec` 真实 CLI，父进程 CAS 失败后关闭 gate 并销毁整个 cgroup/scope，真实 CLI 用户代码零执行;`start_new_session`/父死清理不被用作 suspended launch 的替代

#### Scenario: 未实现启动 gate 的平台 readiness fail-closed
- **WHEN** 某平台（如尚未落地 gate 的 POSIX）Worker 启动，但只具备父死 containment、无 CAS 前启动闸门
- **THEN** 该平台 Worker readiness fail-closed 不放行、不领取 run;已具备 gate 的 Windows Worker 不受影响

#### Scenario: 单次心跳失败停 claim 但不误杀
- **WHEN** 数据库短抖动导致一次心跳 UPDATE 失败，但 `worker_state.lease_expires_at` 仍未到期、随后续租在安全截止点前成功
- **THEN** 该 Worker 只暂停新 claim/launch、**不杀在跑 CLI**，续租成功后恢复领取;短抖动不造成在跑 run 大面积中断

#### Scenario: 续租至安全截止点仍失败则 self-fence
- **WHEN** 某 Worker 持续无法续租（DB 不可达）直到本地单调时钟越过安全截止点（早于 `lease_expires_at`）
- **THEN** 无法再证明数据库租约有效，该 Worker 终止本 generation 所持全部进程并退出、不 finalize、不继续依赖本地缓存 generation;截止点早于 lease 到期，接管者不会与其真实运行窗口重叠

#### Scenario: 安全截止点按 DB/本地时钟偏移 + RTT 保守换算
- **WHEN** DB 时钟与本地墙钟存在偏移、续租 RTT 较高、期间有 GC pause，用 fake clock 推进本地单调时钟
- **THEN** self-fence 按 `local_safe_deadline = local_mono_before_request + (db_lease_expires_at - db_now) - rtt - safety_margin` 判定（以请求前采样点为基准、扣完整 RTT，SHALL NOT 响应后采样却相加完整剩余租期）;验收 = **不晚于安全截止点 self-fence、也不无理由大幅提前**;SHALL NOT 用墙钟与 `db_lease_expires_at` 直接比较

### Requirement: Worker 温和重启 + resume 续跑（改执行层代码）

更新执行层代码需重启 Worker 时,系统 SHALL 优先「温和重启」：收到重启意图后停止领新活、等待在跑 run 自然收尾一个上限窗口（默认 5 分钟，参数化可调）,窗口内全部收尾则零中断重启。仅当超过等待上限仍有在跑 run,才 SHALL 转入「中断 + resume 续跑」硬路径：旧 Worker 收交棒标记后停领新活、杀在跑 CLI 子进程树、**在确认完整进程树退出后**把这些 run 落 `superseded` 终态（`superseded` SHALL NOT 触发子任务 done / 父任务 reviewing 等自动流转）,并对每个 run（**不论有无 session**，统一 recovery child 模型，第七轮 P0-2/第八轮 0.8）入队一条「续跑 run」(携带 `superseded_from`、`recovery_mode`：有 session=`session_resume`/无=`full_replay`、系统恢复标记),然后退出;进程树未确认退出时按 P0-B 落 `recovery_blocked` 不建 child;新 Worker 领取续跑 run 后 SHALL **重发原始任务 prompt** 并依 [agent-session-resume] 的 resume 从上次上下文续跑。在跑 Agent SHALL 允许秒级中断。**恢复承诺 SHALL 按三层口径表述，SHALL NOT 承诺「上下文必然不丢」或 exactly-once（Review 第五轮 P1-7）**：① **平台消息与执行意图 = at-least-once**——不漏、允许重放（committed 水位 + 续跑重取保证）;② **CLI 原生 session = best-effort resume**——平台 pin 住 `session_id` 只证明有 resume 指针，CLI 在被强杀前可能尚未把最新模型 turn/tool result/session metadata 刷入其原生存储，故只能尽力续、不保证所有原生上下文必然落盘;③ **外部副作用 = 不保证 exactly-once**——依赖幂等键/fencing/人工确认。目标是「一般无需人工从头重跑」，而非「上下文绝不丢失」。**所有交棒中断路径 SHALL 统一为「父 execution→superseded + 子 execution→queued(带 `superseded_from`+`recovery_mode`)」的 recovery child 状态机，SHALL NOT 让无 session 的 run 落 `failed` 再重排队（Review 第六轮 P0-3：`failed` 是 execution 终态，状态机无 `running→failed→queued`；且会引入两套并存恢复模型）**：有 `session_id` → child `recovery_mode=session_resume`（resume 续跑）;无 `session_id`（首次执行尚无 session、或 poisoned 已丢 session）→ child `recovery_mode=full_replay`（从平台消息重建上下文，恢复能力较弱但仍是 recovery child，SHALL NOT 计为业务 execution failed）。两条路径复用同一 `superseded_from` 唯一约束、统一 `finish_execution()` 收尾事务、recovery budget 与事件顺序。续跑 run SHALL 标记为系统恢复类,豁免 mention-chain 空转链计数与单任务运行数配额,不与 Agent 自发触发混淆。续跑 SHALL 靠 session 记忆 + prompt 约束防副作用重复,SHALL NOT 依赖服务端精确幂等键。

本能力**依赖** [agent-session-resume] 提供 per-agent `session_id`（claude+codex 均已接入）、流中途 pin 落库、续跑重发原 prompt 语义;本 change 只负责「重启时的 defer 等待、中断、落终态与续跑入队」,不实现 resume 本身。

#### Scenario: 温和重启等空闲窗口零中断
- **WHEN** 触发执行层重启,当前有在跑 run,且这些 run 在等待上限窗口内自然收尾
- **THEN** 系统等其全部收尾后再重启 Worker,在跑 Agent 零中断、无需 resume

#### Scenario: defer 超时转交棒 resume 续跑
- **WHEN** 等待上限窗口内仍有在跑 run,某 run 有可用 session_id
- **THEN** 旧 Worker 杀该 CLI、父 execution 落 `superseded`（不触发自动流转）、恰好一个子 execution 入队 `queued` 带 `superseded_from`+`recovery_mode=session_resume`+系统恢复标记;新 Worker 领取后重发原 prompt 以 resume 续跑,Agent 从上次上下文继续,不从头重跑

#### Scenario: 未 pin session 即强杀走 full_replay child
- **WHEN** 强杀一个尚未 pin session_id 的在跑 CLI
- **THEN** 父 execution 恰好落 `superseded`、恰好一个子 execution 入队 `queued` 带 `recovery_mode=full_replay`，无 `running→failed→queued`、无 terminal→queued

#### Scenario: 有无 session 恢复协议一致
- **WHEN** 分别对有 session 与无 session 的中断 run 触发恢复
- **THEN** 前者子 execution `recovery_mode=session_resume`、后者 `full_replay`，但父子因果链（`superseded_from`）、recovery budget、统一收尾事务与事件协议完全一致，不出现两套恢复模型

#### Scenario: 续跑不吃防死循环配额
- **WHEN** 系统因重启多次为某任务的 run 入队续跑
- **THEN** 这些续跑标记为系统恢复类,不计入 mention-chain 空转链、不占单任务运行数配额,不触发误熔断

#### Scenario: 续跑防副作用重复
- **WHEN** 被中断的 run 已产生副作用（如已建卡/已评论/已改状态）,续跑 resume 后可能重复
- **THEN** 系统靠 CLI session 记忆 + prompt 约束（聚焦本轮、只做一次、先检查再动手）抑制重复,不依赖服务端精确幂等

#### Scenario: 无 session 的 run 走 full_replay recovery child
- **WHEN** 交棒时某在跑 run 无可用 session_id（首次执行尚未产生 session,或 poisoned 已丢 session）
- **THEN** 父 execution 落 `superseded`（非 `failed`）、恰好一个子 execution 入队 `queued` 带 `superseded_from=父` + `recovery_mode=full_replay`（从平台消息重建上下文），复用 superseded_from 唯一约束/统一收尾事务/recovery budget，不因缺 session 而卡死或丢任务、不出现 `running→failed→queued`

#### Scenario: 终态 execution 不得回 queued
- **WHEN** 任一 execution 已处 `done/failed/killed/superseded/recovery_blocked` 终态
- **THEN** 系统 SHALL NOT 把它改回 `queued`；恢复一律经「新建子 execution + `superseded_from`」实现，终态不可逆

#### Scenario: 防双续幂等
- **WHEN** 交棒杀与 reclaim 兜底可能对同一被中断 run 各触发一次续跑入队
- **THEN** 以「该 run 是否已有 `superseded_from` 子 run」为幂等键,同一被中断 run 至多生成一条续跑 run

#### Scenario: 各强杀窗口恢复符合 best-effort/at-least-once 口径
- **WHEN** 分别在 session_id 首次出现前、出现后、首个工具调用前、工具调用后、assistant 完成前强杀在跑 CLI
- **THEN** 平台消息与执行意图不漏（at-least-once、允许重放），CLI 原生 session 尽力 resume（可能丢最后未刷盘的 turn），外部副作用可能重复（依赖幂等/fencing/人工），恢复行为符合三层口径而非「上下文必然不丢」

### Requirement: 异常重启的 resume 兜底

系统在启动 `reclaim_orphan_runs` 回收残留 running 记录时,**SHALL NOT 无条件假设「running 已死」**（硬崩溃/断电/`kill -9` 时 CLI 子进程可能成孤儿存活）。SHALL 按「先接管 → 再判死 → 才续跑」处理：① 先 CAS 升 generation 接管（决策 8），使旧世代被 fencing、孤儿 CLI 无法写平台（杜绝双写）;② 用持久化 `pid + pid_create_time` 探活，进程不存在或 create-time 不匹配才判定已停;③ **仅在「fencing 已生效 AND 完整进程树已确认退出/containment 已确认清理」双条件同时满足时**，才据 recovery mode 追加入队续跑 run（带 `superseded_from` 幂等标 + 系统恢复豁免配额）——此处与「交棒/reclaim child 启动前置」Requirement 的 AND 条件是**同一真相源**，SHALL NOT 写成「已确认停 **或** 已被 fencing」的 OR 口径（generation fencing 只挡平台写、挡不住残留 CLI 改文件/执行 shell/调外部 API，单凭 fencing 不足以建 child）;④ 既不能证明进程树已退出、又不能保证 containment 清理时,SHALL 置该 run 为 `recovery_blocked`（`blocked_reason=process_not_confirmed_dead`，进 dead-letter 待人工），SHALL NOT 创建 recovery child。**异常 reclaim 与温和交棒 SHALL 统一为同一套 recovery child 模型（Review 第七轮 P0-2，删除旧的「无 session 落 failed / 现状兜底不续」路径——它重新引入两套恢复模型且与本文件「所有交棒中断路径统一 recovery child」矛盾）**：
- **有可用 `session_id`** → 父 `superseded` + `recovery_mode=session_resume` child。
- **无/失效 `session_id`**（首次执行尚无 session、或存量 session 不可用）→ **清 session 后** 父 `superseded` + `recovery_mode=full_replay` child（从平台消息重建上下文），SHALL NOT 落 `failed` 或丢弃执行意图。
- **无法确认旧执行已停** → `recovery_blocked`，无 child。
- **恢复预算耗尽** → `recovery_blocked`，无 child。

「session 不可用」只决定 recovery mode，不应把基础设施中断记成业务 `failed`。SHALL 区分：① attempt 因业务/模型 poisoned failure 自身失败 → 按 failure/recovery policy（可禁自动重试）;② Worker 崩溃时发现已存 session 不可用 → 清 session、用 `full_replay` child。异常 reclaim 与温和交棒的父子因果链（`superseded_from`）、recovery budget、事件顺序与 `recovery_mode` SHALL 完全一致。**`recovery_blocked` SHALL 有产品闭环（Review P1-7）**：Runtime/任务详情 SHALL 展示阻塞原因、旧 pid、generation、处理建议，并提供人工「确认已清理后重试」入口（人工确认旧进程已清理 → 允许生成 recovery child 续跑），SHALL NOT 只落状态而无出口。**该人工入口 SHALL 走独立事务 `resume_blocked_execution()`（见下方 Requirement，第十轮 P1-3 + 第十一轮 P0-A/P1-A）**——**父 `recovery_blocked` 终态永久不变**（不改 superseded、不发第二终态事件），仅原子创建一个 `superseded_from=父` 的 recovery child + 在 **child 事件流**写统一的 `recovery_resumed{source:manual}`/child queued 事件（**不追加到已封闭的父流**，第十二轮 P0-B;事件名统一为 `recovery_resumed`、无 `manual_recovery` 第二名，第十三轮 P1-C），由 `UNIQUE(superseded_from)` 保证至多一个 child;SHALL NOT 复用 `finish_execution()`、SHALL NOT 二次 retire/terminal。

#### Scenario: 硬崩溃后先接管再判死才 resume
- **WHEN** Worker 非交棒地异常退出(崩溃),重启后 reclaim 发现残留 running run 且其有 session_id
- **THEN** 系统先 CAS 升 generation 接管（fencing 旧世代）、再用 pid+create_time 确认旧 CLI 已停或已清理,才追加入队续跑 run,新 Worker 领取后 `--resume` 续跑

#### Scenario: 硬崩溃时无 session 走 full_replay child 不落 failed
- **WHEN** Worker 崩溃前该 run 尚未出现 session_id（首次执行），重启后 reclaim 确认旧进程已停
- **THEN** 父 execution 落 `superseded`、恰好一个子 execution 入队 `recovery_mode=full_replay`（从平台消息重建上下文），SHALL NOT 落 `failed` 丢任务、SHALL NOT 出现 `running→failed→queued`

#### Scenario: 硬崩溃时存量 session 不可用则清 session 走 full_replay
- **WHEN** Worker 崩溃前 run 已有 session 但已标 poisoned/失效，重启后 reclaim 确认旧进程已停
- **THEN** 系统清 session 后父 `superseded` + `recovery_mode=full_replay` child，SHALL NOT 因 session 不可用把基础设施中断记为业务 `failed`;区分「attempt 自身业务 poisoned failure」（按 failure policy）与「崩溃发现存量 session 不可用」（清 session full replay）

#### Scenario: 异常 reclaim 与温和交棒恢复协议一致
- **WHEN** 分别经温和交棒与异常 reclaim 恢复同一类 run（有 session / 无 session）
- **THEN** 两条路径的父子因果链（`superseded_from`）、recovery budget、事件顺序与 `recovery_mode`（session_resume/full_replay）完全一致，不存在两套恢复模型

#### Scenario: 无法确认旧执行已停则不续跑进 dead-letter
- **WHEN** reclaim 发现残留 running run，但无法证明其旧 CLI 已停止、也无法保证 fencing/进程树清理已生效
- **THEN** 系统置该 run 为 `recovery_blocked` 进 dead-letter 待人工介入，不创建 recovery child，避免与孤儿 CLI 双执行

#### Scenario: recovery_blocked 有人工出口
- **WHEN** 用户在 Runtime/任务详情看到某 run 处于 `recovery_blocked`
- **THEN** 界面展示阻塞原因、旧 pid、generation 与处理建议，并提供「确认已清理后重试」入口，用户确认后系统才生成 recovery child 续跑

#### Scenario: reclaim 不重复续跑
- **WHEN** 交棒流程已为某中断 run 入队续跑,随后 reclaim 又扫到该 run
- **THEN** 幂等键命中,reclaim 不再重复入队续跑

### Requirement: 统一收尾事务 finish_execution()（跨 PGR/ASR 单一提交边界）

execution 终态与其事件同事务（本 change SSE Requirement）、session pointer/committed 水位/backlog·pending successor 同事务（[agent-session-resume] 水位与 backlog Requirement）两者各自正确，但 **SHALL 是同一个提交事务**，SHALL NOT 由两份规格分别提交（Review 第五轮 P0-4）。分别提交会产生半提交：committed 水位已推进但 execution 仍 active（恢复后该段消息被认为已消费却无成功 execution）、attempt 已成功且 session pointer 已更新但 terminal event 未提交（前端永久等待）、execution 已终态但 successor 创建失败（pending/backlog 永久丢失）、先建 successor 被 active partial unique index 拒绝（父 execution 尚未在同一事务转终态）、owner 未退休导致终态后迟到 pin/final 仍写 session、`superseded` event 已发但 recovery child 或其 queued event 不存在。

系统 SHALL 定义唯一的 `finish_execution()` 事务边界，在**一次提交**内完成所有适用项：① 定局 attempt 终态;② execution 终态;③ session final 或 poisoned/fallback 清理;④ committed 水位推进;⑤ session owner retire（见 [agent-session-resume]）;⑥ backlog、pending 或 recovery successor 创建及因果关联（`superseded_from`/`history_backlog_from_execution_id`）;⑦ terminal/superseded/queued 事件写入 + **supersede 路径的 child `recovery_resumed{source=reclaim}` 事件（第十五轮 P1-1）**，遵守全局事件 id 顺序（见 SSE Requirement）。任一 CAS 或唯一约束失败 SHALL 使整个事务回滚，调用方重读状态后决定退出或重试，**SHALL NOT 补偿式地继续提交剩余项**。该事务边界 SHALL 覆盖下方字段矩阵的**定局路径**（对 active execution 的一次性终态收尾）：normal success、pending success、retryable failure（未达上限）、普通终局 failure（无/有 external pending）、kill、supersede、poisoned failure、history_backlog success、unsafe orphan recovery_blocked、recovery budget exhausted、protocol_incompatible（pre-claim / claimed·gate 未释放 / running·CLI 已起）、full_replay child（Review 第七轮 P1-1 补齐 backlog 与各类 recovery_blocked;第八轮 P0-A 拆普通终局 failure + external pending、P1-C 补 protocol_incompatible;第九轮 P0-C 拆 claimed 阶段「gate 未释放 CLI 未起」与「gate 已释放 CLI 已起」两分支;第十轮 P1-1 gate 已释放 source 对齐 running）。**`manual recovery`（人工恢复终态 `recovery_blocked` execution）SHALL NOT 走 `finish_execution()`，而走独立事务 `resume_blocked_execution()`（第十一轮 P1-A，见下方 Requirement）**——因为定局动作（attempt/execution 终态、owner retire、terminal event、committed）在首次 `finish_execution()` 已完成，二次调用会重复 retire/terminal、错误重写 `final_attempt_id` 或误推 committed;矩阵中列出 manual recovery 行仅为对照字段处置，其事务边界是 `resume_blocked_execution()` 而非 `finish_execution()`。

**每条路径的字段写入矩阵 SHALL 明确定义（Review 第六轮 P1-5，防「成功路径顺手推进而失败/被杀路径误推 committed」）**。列含义：attempt=定局 attempt 终态;execution=execution 终态;committed=是否推进 `committed_msg_id` 水位;session=session pointer 处置;owner=owner retire;successor=后继 execution。

| 完成路径 | attempt 终态 | execution 终态 | committed 推进 | session pointer | owner | successor |
|---------|------------|--------------|--------------|----------------|-------|-----------|
| normal success | `succeeded` | `done` | **推进**到本次消费的最后原始消息 | final 覆盖为收尾 session | retire | pending 有则建 1 个;否则无 |
| pending success（带回队意图） | `succeeded` | `done` | **推进** | final 覆盖 | retire | 据 pending 建 1 个 successor（新 execution） |
| retryable failure，未达上限（同 execution 瞬时重试） | `failed`(+`failure_stage`/`retryable=true`) | 回 `queued` | **不推进** | **保留** `session_id`（非 poisoned），owner 退休——保留 session pointer ≠ 保留旧 attempt 写权限 | **retire / epoch+1** | 回队不建 successor（同 execution 下次 claim 新 attempt#N+1、重新 acquire 新 owner epoch）;**SHALL NOT 建 recovery child**（recovery child 只用于 supersede/交棒） |
| 普通终局 failure（`retryable=false` 或 `retryable=true` 且预算耗尽），**无** external pending | `failed` | `failed` | **不推进** | 非 poisoned 保留;poisoned 清理 | retire | 无 |
| 普通终局 failure，**有** external pending（Review 第八轮 P0-A，防丢用户新指令） | `failed` | `failed` | **不推进** | 安全 session 可保留;poisoned/失效 session 清理 | retire | **恰好 1 个普通 successor 承接 pending**——session 安全→resume/增量;poisoned/失效→`full_replay`。与父终态/owner retire/事件同 `finish_execution()` 事务 |
| kill | `killed` | `killed` | **不推进** | 不动或按需清理 | retire | 无（kill 是唯一取消 pending 的路径） |
| supersede（交棒） | `superseded` | `superseded` | **不推进**（由 recovery child 完成后推进） | 交由 recovery child 继承 | retire | 恰好 1 个 recovery child（`superseded_from`+`recovery_mode`;前置见「交棒 child 启动前置」P0-B） |
| poisoned failure（session 不可用、禁自动重试） | `failed`(+`failure_stage`) | `failed`（业务终局失败）或按预算 `recovery_blocked`(`blocked_reason=poisoned_session`) | **不推进** | poisoned/fallback 清理（清 session_id） | retire | 自动恢复不建;但**外部 pending intent 清 session 后建 1 个 `full_replay` 普通 successor**（第七轮 P0-3，不丢用户新指令） |
| history_backlog success（Review 第七轮 P1-1） | `succeeded` | `done` | **推进**到 `batch_scan_end` | final 覆盖 | retire | 仍有未扫描尾部→建 1 个 `history_backlog` successor;backlog 已完成且有 pending→建 1 个普通 successor;**两者不能同时建**;都无→不建 |
| unsafe orphan recovery_blocked（Review 第七轮 P1-1 + 第八轮 P1-B） | 旧 attempt 落 `orphaned`（running 中、进程树**未确认退出**，**非** `abandoned`——后者=无残留进程 / `orphaned`=可能存活未确认，第十六轮 P1-A 扩义后仍以「是否确认退出」区分） | `recovery_blocked`(`blocked_reason=process_not_confirmed_dead`) | **不推进** | 不动（未确认停，不清） | retire（清 owner 防迟到写） | 无（`final_attempt_id` 指向该 orphaned attempt，待人工确认清理后再生 child） |
| recovery budget exhausted（Review 第七轮 P1-1） | 定局 attempt 保持其终态 | `recovery_blocked`(`blocked_reason=recovery_budget_exhausted`) | **不推进** | 保留 `session_id`（供人工恢复） | retire | 自动不建;**external pending intent SHALL 持久保留、由人工出口恢复**（不自动建，人工据 pending 重启） |
| protocol_incompatible / pre-claim（Review 第八轮 P1-C） | 无（未创建 attempt） | **保持 `queued` 不变** | **不推进** | 不动 | 不涉及 | 无（Worker readiness fail-closed 不领取，execution 等兼容 Worker，SHALL NOT 污染为 recovery_blocked） |
| protocol_incompatible / claimed·gate 未释放（CLI 未起，Review 第九轮 P0-C） | attempt 落 `abandoned`（launch gate 尚未释放，CLI 从未启动，无残留进程） | 回 `queued`（等兼容 Worker 重领，安全） | **不推进** | 不动 | retire | 无（pending 保留待兼容 Worker 领取）;仅当反复不兼容且预算耗尽才落 `recovery_blocked`(`blocked_reason=protocol_incompatible`) |
| protocol_incompatible / running·CLI 已起（Review 第九轮 P0-C，防双执行;第十轮 source=running） | attempt 落 `orphaned`（launch gate 已释放意味着 CAS 已转 running、CLI 已启动、进程未确认退出——source 为 `running` 非 `claimed`，与 unsafe orphan 同口径） | `recovery_blocked`(`blocked_reason=process_not_confirmed_dead`) | **不推进** | 不动（未确认停，不清） | retire | 无（`final_attempt_id` 指向该 orphaned attempt;**SHALL NOT 回 `queued`**——否则新 Worker 重领同 execution，旧 CLI 若存活则并行双执行;须完整进程树清理并经人工/可靠 reclaim 确认后才建 recovery child） |
| manual recovery（人工恢复，第十轮 P1-3 + 第十一轮 P0-A/P1-A 收口——走独立 `resume_blocked_execution()`，不复用 finish_execution 的定局语义） | 父定局 attempt（如 `orphaned`）**永久不变** | **父 `recovery_blocked` 永久不变**（终态不可逆，SHALL NOT 改 superseded） | **不推进**（由 child 完成后推进） | 交由 recovery child 继承 | **不重复 retire**（落 recovery_blocked 时已 retire） | **恰好 1 个 recovery child**（`queued`，`superseded_from=父`，`UNIQUE(superseded_from)` 强约束;`manual_recovery_token` 仅 API 请求幂等）;父事件流已封闭、**不发第二个 terminal/superseded 事件、也不追加恢复事件**;统一的 `recovery_resumed{source:manual}`+child `queued` 写在 **child 事件流**（进统一 event_type、无 `manual_recovery` 第二名，第十三轮 P1-C），调用端从 `POST /resume` 响应得 child、其他客户端经 `GET` 查询发现（第十二轮 P0-B） |
| full_replay child（作为普通 execution 收尾） | 依其结果 `succeeded`/`failed` | 依其结果 `done`/`failed`/`queued`(retry) | 成功才**推进**（与 normal success 同规则） | 无 session 起步→成功后 final 落新 session;失败按对应失败路径 | retire | 同 normal/retry 规则 |

**只有 normal success / pending success / history_backlog success / full_replay child success 路径 SHALL 推进 `committed_msg_id`;retryable failure / kill / supersede / poisoned failure / recovery_blocked 各类路径 SHALL NOT 推进 committed**——失败/被杀/交棒/阻塞不代表这批原始消息已被成功消费，推进会使恢复后这批消息被误判已消费而丢失。

**`recovery_blocked` SHALL 带结构化 `blocked_reason`（Review 第七轮 P1-1）**，至少区分：`process_not_confirmed_dead`（无法确认旧进程已停）/ `recovery_budget_exhausted`（恢复预算耗尽）/ `poisoned_session`（session 不可用且已按 poisoned policy 处理）/ `protocol_incompatible`（协议版本不兼容 fail-closed）/ `null_conversation_migration`（第十四轮 P1-2：activate 前存量 `conversation_id IS NULL` 的在途行经隔离后落此终态，等待人工迁移到新 conversation，见 [agent-session-resume]「activate 前存量 NULL 在途行三态迁移状态机」）。人工出口据 `blocked_reason` 给不同处理建议。**`null_conversation_migration` 的恢复出口与前四种不同**：它 SHALL NOT 走 `resume_blocked_execution()` 建 `superseded_from=父` 的 recovery child（父 `conversation_id IS NULL`、child 为新 conversation 会违反下方 recovery chain scope 一致，且 NULL child 被三层硬门拒绝永不可执行），而走 [agent-session-resume] 迁移出口创建**普通 successor**（新 conversation、独立 migration lineage、不挂 `superseded_from`），NULL 父保持 `recovery_blocked` 终态不变、任务完成度由 task 级 active 聚合。**`null_conversation_migration` 的进程清理状态用正交字段 `process_cleanup_state(unconfirmed|confirmed)` + `cleanup_version` 表达（第十六轮 P1-C + 第十七轮 P0，SHALL NOT 对 running NULL 用 `process_not_confirmed_dead` 作 blocked_reason——否则通用恢复会建不可执行的 NULL recovery child、丢失迁移身份）**：**running NULL 的 attempt 恒 `orphaned`、两阶段均不改写为 `abandoned`（第十七轮 P0：终态不可逆）**——`unconfirmed`=进程树未确认退出（事务 A `quarantine_null_execution()` 首次落、出口只允许继续清理、activate fail-closed）;`confirmed`=经事务 B `confirm_null_process_cleanup()` CAS（`cleanup_version` 递增、写 `cleanup_confirmed_at/by/evidence_hash`）确认退出（attempt 仍 `orphaned`、展示「已清理」用派生态、出口进入 migration 事务）。claimed NULL（CLI 未起无残留进程）才落 `abandoned`（`abandon_reason=null_conversation_migration`），与 running NULL 区分。

**🔴 recovery_blocked 的人工恢复 SHALL 走独立事务 `resume_blocked_execution()`、父终态永久不变（Review 第十轮 P1-3 + 第十一轮 P0-A/P0-B/P1-A 收口）**：人工在 Runtime 点击「确认已清理后重试」（或可靠 reclaim 确认后自动触发）时，系统 SHALL 调用**独立事务 `resume_blocked_execution(parent_id, recovery_request)`**——它复用统一 DB transaction、事件插入与 successor helper，但**职责只限**：① 校验 `blocked_reason` 分原因前置（见下方恢复矩阵）;② 检查进程树/协议/预算前置;③ 原子插入**恰好一个** recovery child execution（`queued`，`superseded_from=父` + `recovery_mode`——有安全 session→`session_resume`/无或失效→`full_replay`）;④ 写统一的 `recovery_resumed{source:manual}` + child `queued` 事件（child 流、遵全局 id 顺序;事件名统一为 `recovery_resumed`、无 `manual_recovery` 第二名，第十三轮 P1-C）。**父 execution 保持 `recovery_blocked` 永久不变、父定局 attempt（如 `orphaned`）指针永久不变、SHALL NOT 发第二个 terminal/superseded 事件**（终态不可逆，见「终态无出边」约束）。**SHALL NOT 复用 `finish_execution()`**（Review 第十一轮 P1-A）——定局动作（attempt/execution 终态、owner retire、terminal event、committed 推进）在首次 `finish_execution()` 落 `recovery_blocked` 时已完成，`resume_blocked_execution()` **不再** retire owner、**不再**发父 terminal、**不改** `final_attempt_id`、**不推进** committed，避免重复定局。**`superseded_from` 语义 = 「统一 recovery predecessor」，SHALL NOT 新增 `recovered_from` 第二套父子键**（自动交棒的 superseded 父与人工恢复的 recovery_blocked 父共用同一恢复链模型，consistency probe/消费者只认一套）。**🔴 幂等 = 数据库硬约束 `UNIQUE(superseded_from)`（Review 第十一轮 P0-B，删除「或 token」可选性）**：SHALL 建 `CREATE UNIQUE INDEX uq_run_queue_recovery_parent ON run_queue(superseded_from) WHERE superseded_from IS NOT NULL`，保证**一个父 execution 至多一个 recovery child**——人工重复点击、人工点击与后台 reclaim 用**不同 `manual_recovery_token`** 并发时，第二次 INSERT 因 `UNIQUE(superseded_from)` 冲突失败即视为已恢复、不再建 child。`manual_recovery_token` **仅作第二层 API 请求幂等键**（挡同一次 HTTP 重试），**SHALL NOT** 承担父子基数约束——`UNIQUE(recovery_chain_id, manual_recovery_token)` 挡不住「同父不同 token」并发各建一个 child，故父子唯一性 SHALL NOT 依赖它。人工恢复前置 SHALL 与自动 recovery child 一致：据 `blocked_reason` 满足对应前置（如 `process_not_confirmed_dead` 须人工确认「完整进程树已清理」）才允许，SHALL NOT 让人工入口跳过前置确认。

**🔴 四种 `blocked_reason` 的分原因人工恢复矩阵（Review 第十一轮 P1-B）**：不同 `blocked_reason` 的恢复前置、session 处置、预算/协议处置各不相同，SHALL NOT 套用同一个「确认已清理后重试」按钮与前置。下表四种 reason（`process_not_confirmed_dead`/`recovery_budget_exhausted`/`poisoned_session`/`protocol_incompatible`）SHALL 经 `resume_blocked_execution()` 建 `superseded_from=父` 的 recovery child;**第五种 `null_conversation_migration` 例外——它不走 `resume_blocked_execution()`（父 conversation 为 NULL、建 `superseded_from` child 会违反 recovery chain scope 一致），而走 [agent-session-resume] 迁移出口建新 conversation 的普通 successor，见上文 blocked_reason 枚举说明与该 change 三态迁移状态机（第十四轮 P1-2）**。`resume_blocked_execution()` SHALL 按 `blocked_reason` 分派校验，任一前置不满足 SHALL 拒绝创建 child（不产生注定再次 blocked 的 child）：

| blocked_reason | 人工恢复前置 | session 处置 | 预算/协议处置 |
|---|---|---|---|
| `process_not_confirmed_dead` | fencing 生效 **AND** 完整进程树退出/containment 清理（人工确认或可靠 reclaim） | 安全 session 可 `session_resume` | recovery budget 未超限才建 child |
| `recovery_budget_exhausted` | 人工**明确增加/重置**具体某级预算并**记审计** | 按 session 健康度决定 resume/full_replay | child 带**新 budget**;SHALL NOT 原值重启后立刻再次 blocked |
| `poisoned_session` | 清除 poisoned session pointer、**禁止 resume 旧 session** | **强制 `full_replay`** | 保留 external pending intent（不丢用户新指令） |
| `protocol_incompatible` | 已有满足 compatibility floor 的 Worker/readiness | 通常无需进程清理;按 session 健康度处理 | 不满 floor 时 SHALL NOT 建注定再次 blocked 的 child |

每种 reason 的 external pending intent SHALL 都不丢，**kill 仍是唯一取消 pending 的路径**。该矩阵 SHALL 同步进 spec、tasks 与 Runtime 产品动作（前端据 `blocked_reason` 给不同确认表单/前置提示），每种 reason SHALL 有独立 API/事务测试，避免「UI 都显示确认清理」但后台缺真正恢复条件。

**🔴 分原因的真实写操作 SHALL 全部纳入 `resume_blocked_execution()` 单一原子事务（Review 第十二轮 P1-B）**：各 `blocked_reason` 除「校验前置」外还各有**副作用写入**，SHALL NOT 让它们在建 child 之外单独提交（否则「预算已加但 child 没建」「session 已清但恢复失败」半提交）。事务内固定步骤：校验前置 → 写 reason 对应副作用 → 定 `recovery_mode` → 转移/保留 external pending → 插唯一 child（`superseded_from=父`） → 写统一的 `recovery_resumed`+child `queued` 事件（child 流、无 `manual_recovery` 第二名，第十三轮 P1-C） → 单次提交，任一步失败整体回滚。各 reason 的副作用写入：
- `recovery_budget_exhausted`：按**唯一 `grant_delta > 0`** 语义给该 recovery chain 补预算（`budget_limit += grant_delta`、`budget_remaining += grant_delta`、`version += 1` + 写审计 `last_reset_by/at/reason`，见下方「人工增/重置预算」的 grant_delta 定义），child 带新 budget（SHALL NOT 原值重启后立刻再 blocked）;SHALL NOT 用「写新预算值/覆盖 remaining」旧口径（绝对覆盖须走独立 admin override API）;
- `poisoned_session`：**清除 poisoned session pointer**（禁 resume 旧 session）、强制 `recovery_mode=full_replay`;
- 所有 reason：把 external pending intent 的归属/水位**转移到 child**（不丢用户新指令）;
- 所有 reason：写恢复审计（`recovery_resumed`）。

**唯一冲突时 SHALL NOT 重复 apply 副作用（Review 第十二轮 P1-B）**：并发或重试触发第二次 `resume_blocked_execution()` 时，`UNIQUE(superseded_from)` 冲突后 SHALL 先比对 payload——**payload 一致**（同 token 重试或不同 token 同意图）SHALL **读回并返回已存在 child、`idempotent_replay=true`**;**payload 不同**（不同恢复意图）SHALL 返回 **409 `already_resolved`**（见下方三分支）——两种情况都 SHALL NOT 再次加预算 / 再次清 session / 再次转移 pending / 再写一条审计，副作用只在首次成功建 child 的事务里发生一次。

**🔴 不同 token 并发输家 SHALL 按 payload 是否与赢家一致分两种结局（第十五轮 P1-5A + 第十七轮 P1-D）**：`UNIQUE(superseded_from)` 冲突的**输家**（不同 `request_token`、child 已被赢家建）虽不再 insert child，但 SHALL 先比对自己的 `payload_hash` 与赢家 child 的 `canonical_request_id`（赢家事务写入的规范 payload 指纹）：**(a) payload 一致**=同一恢复意图的并发/重放，走幂等 replay——独立事务插入 `{token, payload_hash, child_execution_id=已存在 child, outcome='idempotent_replay', canonical_request_id=赢家}` 映射、返回 `idempotent_replay=true`;**(b) payload 不同**=不同恢复意图（如不同新预算/不同 recovery_mode），父已被别的请求定局，SHALL **返回 409 `already_resolved`（带赢家 `child_execution_id`/`canonical_request_id`）**、只写 `{token, payload_hash, outcome='rejected_already_resolved'}` 审计映射、SHALL NOT 把不同意图静默映射到赢家 child（否则用户以为自己的新预算生效、实际用的是赢家预算）。故 SHALL 明确三个事务分支：
```text
首次获胜：reason 副作用 + budget CAS + child insert + recovery_requests(本 token, payload_hash, canonical_request_id, outcome='winner'→child) + events，同一事务
不同 token·payload 一致（幂等输家）：回滚失败的 child-insert 事务（不在失败事务里补偿写）
                              → 独立事务：不重复副作用 → 插 recovery_requests(本 token, payload_hash, outcome='idempotent_replay' → existing child)
                              → 返回 idempotent_replay=true
不同 token·payload 不同（冲突输家，第十七轮 P1-D）：回滚 → 独立事务：不重复副作用
                              → 插 recovery_requests(本 token, payload_hash, outcome='rejected_already_resolved')
                              → 返回 409 already_resolved（附赢家 child_execution_id/canonical_request_id）
```
三分支下 `recovery_requests` 的 `UNIQUE(parent_execution_id, request_token)` 保证每个 token 恰好一行映射;副作用（预算/session/pending/审计 event）只在赢家事务发生一次;**同 token 不同 payload 仍返回 409（既有）、不同 token 不同 payload 也返回 409（第十七轮 P1-D），两者都 SHALL NOT 静默替换赢家意图**。

**🔴 普通终局失败不得丢失 external pending intent（Review 第八轮 P0-A）**：SHALL 把「自动 retry/recovery 判断」与「执行期间新到达的外部用户 intent 判断」**分开**——`retryable=false` 或 `retryable=true 且预算耗尽` 使 execution 落 `failed` 时，若执行期间已有外部 pending intent，SHALL 在同一 `finish_execution()` 事务内建**恰好一个普通 successor** 承接该 intent（session 安全→resume/增量;poisoned/失效→`full_replay`），SHALL NOT 因「失败」而丢弃用户新指令。**只有用户明确 kill 才取消 pending**。terminal failure 创建 pending successor SHALL 与父 execution 终态、owner retire、事件写入仍在同一 `finish_execution()` 事务内（半提交防护同其他路径）。recovery budget exhausted 落 `recovery_blocked` 时 external pending intent SHALL 持久保留、由人工出口恢复（不自动建 successor，但不丢）。此矩阵是研发实现的**真相源**，与 ASR「execution failed 后新到达 intent 仍建 successor」口径一致，不留两种合法理解。

**🔴 retryable failure = 同 execution 新 attempt，绝不走 recovery child（Review 第七轮 P0-1，修上一轮引入的两套模型回归）**：普通瞬时重试 SHALL 固定为「attempt#N→`failed`、同 execution 回 `queued`、下次 claim 建 attempt#N+1」，SHALL NOT 转 recovery child——recovery child 只用于 supersede/交棒（父 execution→`superseded` + 新 recovery child execution→`queued`）。execution:attempt 一对多模型本就是为「同一 execution 下多次瞬时重试」设计，两条路径 SHALL NOT 并存。

**🔴 retryable terminal attempt 的 owner SHALL 退休（Review 第七轮 P0-1）**：attempt#N 落 `failed` 是 attempt **终态**，据 ASR「attempt 终态后 owner 必退休」不变量，`finish_execution()` SHALL 在同事务内 retire 该 owner（`current_task_run_id` 归 NULL 或 epoch+1），使 attempt#N 的迟到 pin/final 全部 CAS 失败。**保留 `session_id`（非 poisoned）不等于保留旧 attempt 的写权限**;下一次 claim 的 attempt#N+1 SHALL 重新 acquire 新 owner epoch。若不退休，会形成时间窗：attempt#N 已 failed、execution 已 queued、但 `current_task_run_id` 仍指 attempt#N、其迟到流事件仍满足 owner CAS——下次 claim 因退避/并发上限/服务暂停长时间未发生时，旧 attempt 可持续改 session pointer。回队后 `current_attempt_id`/`final_attempt_id` 均 SHALL 归 NULL（retry 未定局），下次 claim 才写新 `current_attempt_id`。

**retry 事件 SHALL 用 `retry_scheduled` 控制事件、SHALL NOT 写 execution terminal event（Review 第七轮 P0-1/P1-6）**：attempt#N 失败回队同事务写 `retry_scheduled{attempt_id, attempt_no, next_retry_at, failure_stage, failure_class, retryable}`（字段与 SSE Requirement 主定义一致、**含 `attempt_id`**，第八轮 P1-F 统一）;execution terminal event 只在 execution 真正落终态（done/failed/killed/superseded/recovery_blocked）时出现。同一 execution 多 attempt 共用一条事件流，前端靠 `retry_scheduled` + 下一个 `run_started` 解释中间失败、退避与 attempt 编号变化，而非先后看到两个 `run_started` 无从解释。

**同事务内写入顺序 SHALL 固定为**：先把父 execution 转出 active 终态（转 `superseded`/终态并清 active 谓词）→ 再插入同 `(conversation, agent)` 的 successor（`queued`）→ 再按全局 id 顺序写事件（父 terminal/superseded event → **supersede 路径追加 child `recovery_resumed{source=reclaim}` event（第十五轮 P1-1，与 SSE Requirement 自动写序逐字一致）** → 子 queued event）→ 单次提交。SHALL NOT 先插 successor 再转父终态（active partial unique index 会因「父仍 active + 新 successor 同 conversation active」瞬时并存而拒绝插入）。

#### Scenario: 收尾任一写入点失败则全回滚
- **WHEN** `finish_execution()` 提交过程中，attempt 终态/execution 终态/session final/committed 推进/owner retire/successor 创建/事件写入中任一 CAS 或唯一约束失败
- **THEN** 整个事务回滚，不出现「committed 已推进但 execution 仍 active」「attempt 成功但无 terminal event」「execution 终态但 successor 丢失」等半提交状态，调用方重读状态后退出或重试

#### Scenario: successor 与父终态同事务不撞唯一索引
- **WHEN** 成功收尾需在建 recovery/backlog successor 的同时把父 execution 转终态
- **THEN** 事务内固定顺序为「父转出 active 终态 → 插同 conversation successor(queued) → 按全局 id 写事件 → 单次提交」，active partial unique index 不因「父仍 active + 新 successor」瞬时并存而拒绝插入;SHALL NOT 先插 successor 再转父终态

#### Scenario: 失败/被杀/交棒路径不推进 committed 水位
- **WHEN** execution 走 retryable failure（回 queued）、kill、supersede 或 poisoned failure 任一路径完成
- **THEN** `finish_execution()` SHALL NOT 推进 `committed_msg_id`——本批原始消息未被成功消费;仅 normal success / pending success 两条路径推进 committed;恢复后这批消息 SHALL 仍可被重新消费而非误判已消费而丢失

#### Scenario: 各路径字段写入符合矩阵
- **WHEN** 分别以 normal success / pending success / retryable failure / kill / supersede / poisoned failure / history_backlog success / unsafe orphan recovery_blocked / recovery budget exhausted / full_replay child 触发 `finish_execution()`
- **THEN** 各路径对 attempt 终态、execution 终态、committed 推进、session pointer、owner retire、successor 六项的写入 SHALL 与字段矩阵一致;**retryable failure 回队时 owner 也 SHALL retire（epoch+1）、仅保留 session_id**（Review 第七轮 P0-1，非旧口径的「回队不 retire」）;supersede 恰好建 1 个 recovery child、kill 不建 successor、poisoned 自动恢复不建但外部 pending 建 full_replay 普通 successor

#### Scenario: backlog 收尾只建一类 successor 不双建
- **WHEN** `history_backlog` run 成功收尾——分「仍有未扫描尾部」与「backlog 已完成且有 pending」两情形
- **THEN** 前者只建 1 个 `history_backlog` successor 续消费尾部;后者只建 1 个普通 successor 处理 pending;两者 SHALL NOT 同时建、SHALL NOT 都不建而丢尾部/pending;推进 committed 到 `batch_scan_end`

#### Scenario: recovery_blocked 各来源符合矩阵且带 blocked_reason
- **WHEN** 分别因 unsafe orphan（未证明旧进程停）/ 恢复预算耗尽 / poisoned session / 协议不兼容 落 `recovery_blocked`
- **THEN** attempt 终态、`final_attempt_id` 指向、session/owner 处置均符合矩阵，且 `blocked_reason` 分别为 `process_not_confirmed_dead`/`recovery_budget_exhausted`/`poisoned_session`/`protocol_incompatible`，人工出口据此给不同处理建议

#### Scenario: 非重试终局失败有 external pending 则建 successor
- **WHEN** attempt `retryable=false` 使 execution 落 `failed`，且执行期间已有外部用户新触发合并成 pending intent
- **THEN** 同一 `finish_execution()` 事务内建**恰好一个**普通 successor 承接 pending（session 安全→resume/增量;poisoned/失效→`full_replay`），committed 不推进，SHALL NOT 因失败丢弃用户新指令

#### Scenario: 预算耗尽终局失败有 external pending 则建 successor
- **WHEN** `retryable=true` 但 retry/recovery 预算耗尽使 execution 落 `failed`，且有 external pending
- **THEN** 同 `finish_execution()` 事务建恰好一个普通 successor 承接 pending;「自动 retry 已耗尽」与「外部新 intent」分开判断，前者停后者不丢

#### Scenario: 终局失败无 pending 不建 successor
- **WHEN** execution 落 `failed` 且无 external pending intent
- **THEN** SHALL NOT 建 successor;committed 不推进;owner retire

#### Scenario: 终局失败建 pending successor 的原子性
- **WHEN** execution 落 `failed` + 建 pending successor 的收尾事务中任一写入点失败
- **THEN** 整个 `finish_execution()` 回滚（父终态/owner retire/successor 创建/事件同事务），不出现「父已 failed 但 pending successor 丢失」半提交

#### Scenario: recovery budget exhausted 的 pending 由人工恢复
- **WHEN** 恢复预算耗尽落 `recovery_blocked`，且有 external pending intent
- **THEN** 自动不建 successor，但 pending intent SHALL 持久保留;人工出口据 pending 恢复时才创建 successor，intent 不丢

#### Scenario: protocol_incompatible 在 pre-claim 不污染 execution
- **WHEN** 协议版本不兼容在 **claim 前** 被 readiness fail-closed 发现（Worker 不满足 protocol floor）
- **THEN** Worker 不领取，execution **保持 `queued` 不变**、等兼容 Worker 领取，SHALL NOT 为表示 Worker 不兼容而把 queued execution 批量污染成 `recovery_blocked`

#### Scenario: protocol_incompatible 在 claimed 阶段 gate 未释放可安全回队（Review 第九轮 P0-C）
- **WHEN** 协议不兼容在 **claimed/preparing 阶段被发现，且 launch gate 尚未释放**（CLI 从未启动、无任何残留进程）
- **THEN** attempt 落 `abandoned`，execution 回 `queued` 等兼容 Worker 重领、owner 退休、pending 保留;因 CLI 从未起、无双执行风险，回队安全;仅当反复不兼容且恢复预算耗尽才落 `recovery_blocked`(`blocked_reason=protocol_incompatible`)

#### Scenario: protocol_incompatible 在 CLI 已起时不得回队（Review 第九轮 P0-C，防双执行;第十轮 source=running）
- **WHEN** 协议不兼容被发现时 **launch gate 已释放/CLI 已启动**——按启动闸门 Requirement，gate 释放在「CAS 转 running」之后，故此时 attempt/execution 已是 `running`（进程未确认退出）
- **THEN** attempt 从 `running` 落 `orphaned`（**source 为 `running` 非 `claimed`**，与 unsafe orphan 同口径），execution 落 `recovery_blocked`(`blocked_reason=process_not_confirmed_dead`)、`final_attempt_id` 指向该 orphaned attempt、owner 退休;**SHALL NOT 回 `queued`**——否则新 Worker 重领同 execution、旧 CLI 若存活则并行双执行

#### Scenario: CLI 已起的 protocol mismatch orphan 不被重新 claim
- **WHEN** 上一 scenario 产生的 `recovery_blocked`(`process_not_confirmed_dead`) execution 存在，调度器扫描可领取 execution
- **THEN** 该 execution **不出现在可 claim 集合**（claim CAS 谓词排除 `recovery_blocked`）;只有完整进程树清理并经人工「确认已清理后重试」或可靠 reclaim（fencing 生效 AND 进程树确认退出）确认后，才允许生成 recovery child 或重新排队

#### Scenario: recovery_blocked 人工恢复走 resume_blocked_execution 且父终态不变（第十一轮 P0-A/P1-A）
- **WHEN** 某 `recovery_blocked(process_not_confirmed_dead)` execution，人工在 Runtime 点击「确认已清理后重试」
- **THEN** 走独立事务 `resume_blocked_execution()`，在一次提交内建恰好一个 recovery child（`queued`，`superseded_from=父`，有安全 session→`session_resume`/否则 `full_replay`）+ 写统一的 `recovery_resumed{source:manual}`/子 `queued` 事件（child 流、遵全局 id 顺序、无 `manual_recovery` 第二名）;**父 execution 保持 `recovery_blocked` 永久不变、父 `final_attempt_id` 仍指向 orphaned attempt、SHALL NOT 发第二个 terminal/superseded 事件、不重复 retire owner、不推进 committed**;任一步失败整体回滚;复用 `superseded_from` 因果键，SHALL NOT 改父状态、SHALL NOT 新增 `recovered_from`、SHALL NOT 复用 `finish_execution()`

#### Scenario: 终态父不产生第二个终态事件（late_event_after_terminal 保护）
- **WHEN** 父 execution 已发 `terminal(recovery_blocked)`，随后人工恢复建 child
- **THEN** 父 execution 事件流保持封闭、**不出现第二个 terminal/superseded 事件、也不追加恢复事件**;统一的 `recovery_resumed{source:manual, recovery_reason:manual_blocked_resume, recovery_parent_id, child_execution_id, blocked_reason}` 作为 **child 事件流的控制事件**（进统一 event_type、无 `manual_recovery` 第二名，第十三轮 P1-C;`recovery_reason` 必填，第十七轮 P2-B），child `queued` 紧随;progress/Runtime/失败归因/`final_attempt_id` 层间映射不失真

#### Scenario: 父终态后经 POST 响应/查询发现 child 而非父流迟到事件（Review 第十二轮 P0-B）
- **WHEN** 客户端在父 `recovery_blocked` 终态后关闭了父订阅，随后人工恢复建 child
- **THEN** 触发恢复的调用端从 `POST /executions/{parent_id}/resume` 响应体直接取得 `child_execution_id` 并订阅 child 流;其他客户端经 `GET /executions/{parent_id}` 查询得到 `recovery_child_id`/`recovery_resolution` 发现 child;两条发现路径都 SHALL NOT 依赖向已封闭父流追加的迟到事件

#### Scenario: 人工恢复至多一个 child 由 UNIQUE(superseded_from) 保证（第十一轮 P0-B）
- **WHEN** 人工用同一 token 连续提交多次、用**不同 `manual_recovery_token` 并发**提交、或人工点击与后台 reclaim 用两个独立连接同时尝试恢复同一 `recovery_blocked` execution
- **THEN** 数据库硬约束 `UNIQUE(run_queue.superseded_from) WHERE superseded_from IS NOT NULL` 保证**至多一个 recovery child**;第二个 INSERT 因唯一约束冲突失败即视为已恢复、不再建 child;真正阻止重复的 SHALL 是 `UNIQUE(superseded_from)` 而非应用层先查后插，也 SHALL NOT 仅靠 `manual_recovery_token`（挡不住同父不同 token 并发）

#### Scenario: 分原因恢复前置各不相同（第十一轮 P1-B）
- **WHEN** 分别对 `process_not_confirmed_dead`/`recovery_budget_exhausted`/`poisoned_session`/`protocol_incompatible` 四种 blocked_reason 触发人工恢复
- **THEN** `resume_blocked_execution()` 按 reason 校验不同前置——process 须确认全树退出、budget 须人工按 `grant_delta > 0` 补预算+审计、poisoned 须清 session 强制 full_replay、protocol 须有满足 floor 的 Worker;前置不满足 SHALL 拒绝建 child（不产生注定再次 blocked 的 child）;每种 reason 的 external pending 都不丢，kill 仍是唯一取消 pending 的路径

#### Scenario: reason 副作用写入与建 child 同一原子事务（第十二轮 P1-B）
- **WHEN** 对 `recovery_budget_exhausted` 恢复（按 `grant_delta > 0` 补预算+审计）、对 `poisoned_session` 恢复（清 poisoned pointer + 强制 full_replay），在插入 child 前后任一写入点注入故障
- **THEN** 预算 `grant_delta` 写入/审计/清 session/转移 pending/插 child/写事件全在 `resume_blocked_execution()` 单次提交内，任一失败整体回滚，不出现「预算已加但 child 没建」「session 已清但恢复失败」半提交;成功则 child 带新预算（`budget_remaining += grant_delta`，不原值立刻再 blocked）、poisoned 走 full_replay

#### Scenario: 唯一冲突读回已存在 child 不重复 apply 副作用（第十二轮 P1-B）
- **WHEN** 并发/重试触发第二次 `resume_blocked_execution()`，`UNIQUE(superseded_from)` 冲突
- **THEN** 读回并返回已存在 child、`idempotent_replay=true`，SHALL NOT 再次加预算/再次清 session/再次转移 pending/再写审计——副作用只在首次成功建 child 的事务里发生一次

#### Scenario: 人工恢复不得跳过 blocked_reason 前置确认
- **WHEN** 某 `recovery_blocked(process_not_confirmed_dead)` execution 的旧进程树尚未确认清理，人工尝试恢复
- **THEN** 人工入口 SHALL 要求先确认「完整进程树已清理」才放行恢复，SHALL NOT 让人工点击跳过该确认直接建 child（否则重新引入旧进程与新 attempt 双执行）

#### Scenario: 普通 retry 始终同 execution 新 attempt，不建 recovery child
- **WHEN** attempt#1 瞬时失败（`failed`+`retryable=true`，未达恢复上限）
- **THEN** 同一 execution 回 `queued`、下次 claim 建 attempt#2，SHALL NOT 创建 recovery child（recovery child 只用于 supersede/交棒）;状态词汇表与 finish 矩阵不再并存两套 retry 模型

#### Scenario: retry 回队后旧 attempt owner 已退休，迟到写失败
- **WHEN** attempt#1 落 `failed` 回队后，下一次 claim 因退避/并发上限长时间未发生，此时注入 attempt#1 的迟到 pin/final
- **THEN** 因 owner epoch 已在 `finish_execution()` 同事务退休，所有迟到 pin/final CAS 失败;`session_id` 仍保留（非 poisoned）供 attempt#2 acquire 新 owner epoch 后 resume;`current_attempt_id`/`final_attempt_id` 均为 NULL

#### Scenario: retry 产生 retry_scheduled 边界事件而非 execution terminal
- **WHEN** attempt#1 `failed` → 退避 → attempt#2 claim
- **THEN** 事件流出现 `retry_scheduled{attempt_id,attempt_no,next_retry_at,failure_stage,failure_class,retryable}` 与随后的 attempt#2 `run_started`，中间 SHALL NOT 出现 execution terminal event;execution terminal 只在 execution 真正落终态时出现

### Requirement: 人工恢复 API 契约与恢复审计（POST /resume + 查询发现）

系统 SHALL 为 `recovery_blocked` execution 的人工恢复提供明确的 API 契约，使调用端从**响应体**获得 child、其他客户端从**查询**发现 child，而非依赖向已封闭父事件流追加迟到事件（Review 第十二轮 P0-B/P1-C）。

**恢复端点** `POST /executions/{parent_id}/resume` SHALL 满足：
- **请求体**：`{blocked_reason_ack, recovery_request}`——`recovery_request` 按 `blocked_reason` 携带对应前置证据（如 `process_not_confirmed_dead` 的「进程树已清理」确认、`recovery_budget_exhausted` 的 `grant_delta > 0` 补预算量 + 审计理由）;客户端 SHALL 携带 `manual_recovery_token`（请求幂等键）。
- **鉴权**：SHALL 要求人工恢复的操作者身份（`actor`），记入审计。
- **成功响应**（201/200）：`{parent_execution_id, child_execution_id, recovery_mode(session_resume|full_replay), recovery_source: manual, idempotent_replay: bool}`——调用端据 `child_execution_id` 订阅 `GET /executions/{child_execution_id}/events`。
- **幂等**：同 `manual_recovery_token` 重试 SHALL 返回**同一** `child_execution_id` 且 `idempotent_replay=true`（第二层请求幂等）;父子基数由 `UNIQUE(superseded_from)` 硬约束保证（第一层）——**同父不同 token 并发且 payload 一致时**，第二个 INSERT 冲突后 SHALL 读回并返回已存在 child、`idempotent_replay=true`，SHALL NOT 报 500。
- **父已被别的意图定局**（409 `already_resolved`，第十七轮 P1-D）：**同 token 不同 payload**、或**不同 token 不同 payload**（父已有赢家 child、且本请求的 `payload_hash` 与赢家 `canonical_request_id` 不同）SHALL 返回 409 `already_resolved`（附赢家 `child_execution_id`/`canonical_request_id`），SHALL NOT 把不同恢复意图（不同预算/不同 recovery_mode）静默映射到赢家 child。
- **前置不满足**（409/422）：`blocked_reason` 对应前置未满足（如进程树未确认清理）SHALL 拒绝并返回结构化原因，SHALL NOT 建 child。
- **父非 recovery_blocked**（409）：父不在 `recovery_blocked` 终态时拒绝。
- 事务 SHALL 走 `resume_blocked_execution()`（父终态不变，见上）。

**查询发现** `GET /executions/{parent_id}` SHALL 在响应中返回 `recovery_child_id`（无则 null）与 `recovery_resolution`（`unresolved`/`resumed`），使未持有 POST 响应的客户端能发现 child。**`null_conversation_migration` 父（第十七轮 P1-A）SHALL 额外返回 `resolution_kind='migration'`、`migration_successor_id`（承接普通 successor id，无则 null，来源 `migration_from_execution_id=父`）、`process_cleanup_state`**——客户端据此区分「recovery 类看 recovery_child_id / migration 类看 migration_successor_id」，`recovery_resolution` 对 migration 类由 `migration_successor_id` 是否存在派生;其余 reason `resolution_kind='recovery'`、`migration_successor_id=null`。

**`manual_recovery_token` 持久化** SHALL 用 `(parent_execution_id, manual_recovery_token)` 唯一索引 + 请求 `payload_hash` + 生成的 `child_execution_id` + 赢家 `canonical_request_id` + `outcome(winner|idempotent_replay|rejected_already_resolved)`（第十七轮 P1-D），使「响应丢失后重试」返回同一 child、「同 token 不同 payload」返回 409、「**不同 token·payload 一致**」返回已存在 child（`idempotent_replay=true`）、「**不同 token·payload 不同**」返回 409 `already_resolved`（第十七轮 P1-D，SHALL NOT 静默映射不同意图到赢家 child）。它 SHALL NOT 承担父子基数（那是 `UNIQUE(superseded_from)`）。

**恢复控制事件 SHALL 统一为唯一的 `recovery_resumed{source: manual|reclaim, recovery_reason, actor?, recovery_parent_id, child_execution_id, blocked_reason?, request_token?}`（Review 第十三轮 P1-C + 第十七轮 P2-B，删除 `manual_recovery` 第二事件名）**——**自动 reclaim（后台交棒/接管）产生的恢复 `source=reclaim` 且不记为人工操作**，人工 POST 恢复 `source=manual` 带 `actor`;**`recovery_reason` SHALL 为必填枚举 `handover|orphan_reclaim|manual_blocked_resume`（第十七轮 P2-B）**——`source=manual` 恒 `manual_blocked_resume`（父必为 `recovery_blocked`、`blocked_reason` 必填）;`source=reclaim` 为 `handover`（正常交棒，父 `superseded`）或 `orphan_reclaim`（孤儿接管），**父为 `superseded` 时无 `blocked_reason`、该字段 SHALL 缺省/为 null，消费者 SHALL NOT 假定 `recovery_resumed` 恒带 `blocked_reason`**（reclaim 交棒父没有阻塞原因）;仅当承接的父处于 `recovery_blocked`（manual_blocked_resume，或 orphan_reclaim 命中 recovery_blocked 孤儿）时 `blocked_reason` 才必填。它是 **child 事件流的控制事件、进入统一 event_type 真相源**，SHALL NOT 追加到已封闭的父事件流、SHALL NOT「child SSE 或独立审计表」两种实现并存（统一 child SSE 控制事件，另需审计则作其落库派生）。**同事务写序 SHALL 固定为**：`insert child execution → recovery_resumed(child_execution_id) → child queued(child_execution_id) → commit`,使 `recovery_resumed` 与 child `queued` 的全局 id 均在 child 事件流内单调递增。**child 订阅回放语义 SHALL 明确**：调用端从 `POST /resume` 响应拿到 `child_execution_id` 后，订阅 child 流 SHALL 从 child 自身起点回放（`WHERE execution_id=:child ORDER BY id`），SHALL NOT 携带父的较大全局 `Last-Event-ID` 而跳过 child 首个 `recovery_resumed`/`queued` 事件;经父 superseded 跳转的续订走 SSE Requirement 既有的「携带当前全局 Last-Event-ID + child queued 全局 id 必然更大」协议，两种口径不并存。

#### Scenario: POST /resume 返回 child_execution_id 供订阅
- **WHEN** 人工对某 `recovery_blocked(process_not_confirmed_dead)` execution 调用 `POST /executions/{parent_id}/resume`，前置已满足
- **THEN** 走 `resume_blocked_execution()` 建恰好一个 recovery child，响应返回 `{parent_execution_id, child_execution_id, recovery_mode, recovery_source: manual, idempotent_replay: false}`，调用端据 `child_execution_id` 订阅 child 流;父事件流保持封闭

#### Scenario: 同 token 重试与同父不同 token·同 payload 并发都返回同一 child
- **WHEN** 恢复响应丢失后客户端用**同一** `manual_recovery_token` 重试；另有后台 reclaim 用**不同 token 但相同 payload** 并发恢复同一父
- **THEN** 同 token 重试经 `(parent_execution_id, token)` 唯一索引返回同一 `child_execution_id`、`idempotent_replay=true`;不同 token·payload 一致时 `UNIQUE(superseded_from)` 冲突后比对 `payload_hash`==赢家 `canonical_request_id` 通过、读回已存在 child 返回、`idempotent_replay=true`，两路径都 SHALL NOT 建第二个 child、SHALL NOT 报 500

#### Scenario: 不同 token·不同 payload 并发返回 409 already_resolved（第十七轮 P1-D）
- **WHEN** 两个操作者以**不同 token 且不同 payload**（如不同新预算值/不同 recovery_mode）并发恢复同一 `recovery_blocked` 父，其一先胜出建 child
- **THEN** 输家 `UNIQUE(superseded_from)` 冲突后比对 `payload_hash` != 赢家 `canonical_request_id`，返回 **409 `already_resolved`**（附赢家 `child_execution_id`/`canonical_request_id`）、独立事务只写 `recovery_requests(token, payload_hash, outcome='rejected_already_resolved')` 审计映射;SHALL NOT 把输家的不同意图静默映射到赢家 child（否则输家误以为自己的新预算/模式生效）、SHALL NOT 建第二个 child、SHALL NOT 报 500

#### Scenario: 前置不满足或父非 recovery_blocked 拒绝恢复
- **WHEN** 对进程树尚未确认清理的 `process_not_confirmed_dead` execution、或对一个非 `recovery_blocked` 的 execution 调用 resume
- **THEN** 分别返回 409/422 结构化原因，不建 child、不改父状态

#### Scenario: 自动 reclaim 恢复不记为人工操作
- **WHEN** 后台交棒/可靠 reclaim 自动为某 execution 生成 recovery child
- **THEN** 统一的 `recovery_resumed` 事件 `source=reclaim`、不带人工 `actor`;人工 POST 恢复才 `source=manual` 且带 `actor`;两者都写 **child 事件流的 `recovery_resumed` 控制事件**（同一 event_type、无 `manual_recovery` 第二名），不追加父流

#### Scenario: GET 详情暴露 recovery_child_id 供查询发现
- **WHEN** 某客户端未持有 POST 响应，需知道 `recovery_blocked` 父是否已恢复
- **THEN** `GET /executions/{parent_id}` 返回 `recovery_child_id`（已恢复则为 child id，否则 null）与 `recovery_resolution`（`unresolved`/`resumed`），据此发现并订阅 child，不依赖父流迟到事件

#### Scenario: recovery_resumed 进入统一 event_type 且唯一（第十三轮 P1-C）
- **WHEN** 检查 SSE 控制事件 event_type 枚举真相源
- **THEN** 含唯一的 `recovery_resumed`，**不含 `manual_recovery` 第二事件名**;旧客户端遇未知 event_type 按兼容策略不崩;前端消费者、回放协议、事件事务探针一并覆盖 `recovery_resumed`

#### Scenario: recovery child 事件写序与全局 id 单调（第十三轮 P1-C）
- **WHEN** `resume_blocked_execution()`/reclaim 建 child
- **THEN** 同事务写序固定为 `insert child execution → recovery_resumed(child_execution_id) → child queued(child_execution_id) → commit`;任一点故障整体回滚（无「有 child 无 recovery_resumed」「有事件无 child」半提交）;child 流内 `recovery_resumed` 与 `queued` 的全局 id 单调递增

#### Scenario: child 订阅从自身起点回放不漏首事件（第十三轮 P1-C）
- **WHEN** 调用端从 `POST /resume` 响应得 `child_execution_id` 后订阅 child 流
- **THEN** 从 child 自身起点回放（`WHERE execution_id=:child ORDER BY id`），能看到 `recovery_resumed`/`queued` 首事件;SHALL NOT 携带父的较大全局 `Last-Event-ID` 而跳过 child 首事件;经父 superseded 跳转的续订仍走 SSE Requirement 既有全局游标协议，两种口径不并存

### Requirement: recovery_blocked 的 resolved/unresolved 派生态与链叶消费矩阵

`recovery_blocked` 是**永久终态**、父状态不因恢复而改变（见「终态无出边」约束），故「该 execution 是否还阻塞」SHALL 是一个**派生态**而非父 `run_queue.status` 的新值，所有消费者 SHALL 从「是否已存在承接 child/successor」关系派生，SHALL NOT 依赖改写父 status（Review 第十二轮 P1-A）。**承接关系按 `blocked_reason` 分两种因果键（第十七轮 P1-A）**：前四种 reason（`process_not_confirmed_dead`/`recovery_budget_exhausted`/`poisoned_session`/`protocol_incompatible`）经 `resume_blocked_execution()` 建 `superseded_from=父` 的 recovery child;**`null_conversation_migration` 经迁移出口建 `migration_from_execution_id=父` 的普通 successor（不挂 `superseded_from`）**——resolved 派生 SHALL 同时认这两个键，否则 NULL migration 父永远 `unresolved`、永久卡在 dead-letter/待办/告警。派生定义：

```text
resolution_kind      = CASE blocked_reason WHEN 'null_conversation_migration' THEN 'migration'（认 migration_from_execution_id）
                                            ELSE 'recovery'（认 superseded_from）END
unresolved blocked   = 父 status='recovery_blocked' AND NOT EXISTS(承接 WHERE 对应因果键=父)
                       （recovery 类查 superseded_from=父;migration 类查 migration_from_execution_id=父）
resumed blocked      = 父 status='recovery_blocked' AND EXISTS(承接 WHERE 对应因果键=父)
effective chain state= 沿 superseded_from 链一路走到叶子 execution，取叶子的 status
                       （叶子可能是 running/done/failed/又一个 recovery_blocked…；
                        migration successor 不在 superseded_from 链上、由 task 级 active 聚合承接）
```
**migration 类额外前置（第十七轮 P0）**：`null_conversation_migration` 父仅在 `process_cleanup_state=confirmed`（或从未 claim 的 queued / 无残留进程的 claimed）时才**可**建 migration successor;`process_cleanup_state=unconfirmed` 期间即便无 successor 也 SHALL 归入「待清理」而非「待迁移」待办。

**🔴 effective chain 只表 recovery lineage，普通 successor 另由 task 级 active 聚合（用户拍板，第十三轮 P1-D）**：`effective chain state` 仅沿 `superseded_from` 恢复链聚合——它只覆盖 supersede/交棒/人工恢复产生的 recovery child。**recovery child 后续因 external pending 或 history backlog 创建的普通 successor 是新的 execution、不挂在 `superseded_from` 恢复链上**（它们用各自的因果字段 `history_backlog_from_execution_id` 等），故 SHALL NOT 期望「沿 `superseded_from` 走叶子」覆盖普通 successor。任务整体完成度 SHALL 由 **task 级 active execution 聚合**判定（该 task 是否还有任一 active execution / 最新 execution 终态），recovery lineage 只回答「这条被中断的执行线是否已续上、续到哪」。SHALL NOT 默认「只沿 `superseded_from` 就覆盖所有后续执行」。

各消费者 SHALL 按下表派生，SHALL NOT 把「父仍是 recovery_blocked」直接当「仍阻塞待人工」：

| 消费者 | 口径 |
|---|---|
| dead-letter / 人工待办 | 只列 `unresolved blocked`（有承接 child/successor 即移出待办，**recovery 类看 superseded_from、migration 类看 migration_from_execution_id**，第十七轮 P1-A）;SHALL NOT 把 `resumed blocked` 仍列为待恢复;SHALL NOT 因只查 superseded_from 而把已迁移的 NULL 父永久留在待办 |
| Runtime/RunRow 人工「确认已清理后重试」按钮 | `unresolved blocked` 才可点;`resumed blocked` 后按钮禁用/改为「查看恢复 child」跳转;**`null_conversation_migration` 类按钮语义为「迁移到新会话」而非「确认已清理后重试」，且 `process_cleanup_state=unconfirmed` 时按钮为「继续清理」不可迁移** |
| progress / 任务完成度 | 沿链取 `effective chain state`——叶子 `done` 才算该任务线完成，父 `recovery_blocked` 不使整条链判失败/判阻塞 |
| 告警 | 只对 `unresolved blocked` 持续超阈值告警;`resumed blocked` 解除该父的阻塞告警（转而观察 child） |
| 失败率统计 | `recovery_blocked` 本身不计业务失败率（同 orphaned 口径）;链最终叶子的业务终态才参与统计口径 |

可选：为查询效率增设 `resolved_at`/`resolved_by` 冗余列，但 SHALL 与 child insert **在同一 `resume_blocked_execution()` 事务内写入**，SHALL NOT 作为独立可漂移的真相源——权威判定仍是 `EXISTS(child WHERE superseded_from=父)`。

#### Scenario: dead-letter 只列 unresolved、有 child 即移出
- **WHEN** 一个 `recovery_blocked` execution 先无 child（unresolved），随后经 `resume_blocked_execution()` 建了 recovery child（resumed）
- **THEN** dead-letter/人工待办在建 child 前列出它、建 child 后即移出;判定走 `EXISTS(child WHERE superseded_from=父)`，SHALL NOT 因父仍是 `recovery_blocked` 而永久留在待办

#### Scenario: 人工按钮按派生态启停
- **WHEN** 用户在 Runtime 查看一个已 resumed 的 `recovery_blocked` 父
- **THEN** 「确认已清理后重试」按钮禁用（避免再建 child，`UNIQUE(superseded_from)` 也会挡），改为「查看恢复 child」跳转;只有 `unresolved blocked` 的父按钮可点

#### Scenario: progress 沿链取叶子态
- **WHEN** 某任务线经历 running→recovery_blocked→(child) running→done，progress 聚合其完成度
- **THEN** 沿 `superseded_from` 链取 `effective chain state`=叶子 `done`，该任务线判为完成;父的 `recovery_blocked` 终态不使整条链判阻塞或判失败

#### Scenario: 告警只对 unresolved 持续报
- **WHEN** 一个 `recovery_blocked` 父长时间无人恢复（unresolved），随后被恢复（resumed）
- **THEN** 告警在 unresolved 期间持续（超阈值升级），resumed 后解除该父的阻塞告警、转而观察 child 链;SHALL NOT 对已 resumed 的父继续报阻塞

#### Scenario: recovery child 后的普通 successor 由 task 级聚合而非 superseded_from 链
- **WHEN** 某 recovery child 续跑成功后，因 external pending 或 history backlog 创建了普通 successor（新 execution、不挂 `superseded_from`）
- **THEN** 沿 `superseded_from` 的 `effective chain state` 停在该 recovery child（recovery lineage 已到叶子），任务整体完成度由 **task 级 active execution 聚合**判定（普通 successor 作为新的 active execution 参与）;SHALL NOT 因 progress 只沿 `superseded_from` 而停在旧 recovery child、漏看普通 successor

#### Scenario: NULL migration 父的 resolved 认 migration_from_execution_id、迁移后移出待办（第十七轮 P1-A）
- **WHEN** 一个 `recovery_blocked(null_conversation_migration)` 父先无承接（unresolved），经迁移出口建了 `migration_from_execution_id=父` 的普通 successor（不挂 `superseded_from`）
- **THEN** resolved 派生对该父走 `resolution_kind='migration'` 认 `migration_from_execution_id`——建 successor 后即从 `unresolved`→`resumed`、移出 dead-letter/待办/解除阻塞告警;SHALL NOT 因 resolved 只查 `superseded_from` 而把已迁移的 NULL 父永久留在 unresolved;`GET /executions/{父}` 返回 `resolution_kind='migration'`+`migration_successor_id`+`process_cleanup_state`

#### Scenario: NULL migration 父未确认清理时归「待清理」非「待迁移」（第十七轮 P0+P1-A）
- **WHEN** 一个 `recovery_blocked(null_conversation_migration)` 父 `process_cleanup_state=unconfirmed`、尚无 migration successor
- **THEN** 该父在待办中归「待清理」（按钮为「继续清理」）而非「待迁移」，SHALL NOT 允许建 migration successor;仅 `confirm_null_process_cleanup()` 翻 `confirmed` 后才转「待迁移」、放行建 successor

### Requirement: task 级完成度聚合的确定性口径（第十七轮 P2-A）

「任务整体完成度由 task 级 active execution 聚合判定」SHALL 有**确定性规则**，SHALL NOT 让「是否有 active execution / 最新 execution 终态」在多 execution 并存时产生歧义（否则 recovery child、普通 successor、migration successor、并存 pending 会让不同消费者得出不同完成度）。规则：

1. **active 定义**：一个 execution 是 active ⟺ `status IN (queued, claimed, running)`。task 有任一 active execution ⟹ task 仍在执行中（progress 不判完成/失败）。
2. **「最新 execution」确定性排序**：无 active 时，task 完成度取**最新终态 execution**——排序键 SHALL 为 `(created_at DESC, id DESC)` 的**确定性全序**（`id` 全局单调、tie-break 唯一），SHALL NOT 用「随便取一条终态」或非确定聚合。
3. **lineage 归并**：recovery child（`superseded_from`）与 migration successor（`migration_from_execution_id`）作为**新的独立 execution** 参与该排序，其前驱（`superseded`/`recovery_blocked` 父）SHALL NOT 再单独计入完成度（前驱已被承接）——即完成度看的是**每条 lineage 的叶子 execution 的终态**，多条并行 lineage 取最新叶子。
4. **migration/recovery 终态不使 task 判失败**：叶子为 `recovery_blocked` 且 unresolved ⟹ task 归「阻塞待人工」而非「失败」;叶子 `done` ⟹ 完成;叶子 `failed`（业务失败终局）⟹ 失败。
5. **确定性 SHALL 跨消费者一致**：progress 聚合、Runtime 总览、任务自动流转、告警 SHALL 全部用同一排序与归并口径，SHALL NOT 各自实现。

#### Scenario: 多 execution 并存时完成度取确定性最新叶子
- **WHEN** 一个 task 同时存在多条 lineage（如一条 recovery chain 叶子 `done`、一条因历史 backlog 建的普通 successor 仍 `running`），或有多条终态 execution
- **THEN** 有任一 active（此处 `running` successor）⟹ task 判「执行中」不判完成;全部终态后按 `(created_at DESC, id DESC)` 全序取最新叶子终态判完成度;SHALL NOT 因消费者各自取不同 execution 而得出矛盾完成度

#### Scenario: migration successor 参与 task 聚合、NULL 父不重复计入
- **WHEN** 一个 `recovery_blocked(null_conversation_migration)` 父经迁移建了普通 successor，successor 后续 `done`
- **THEN** task 完成度取该 migration successor 叶子的 `done`、判完成;NULL 父作为已承接前驱 SHALL NOT 再单独计入使 task 判阻塞;排序/归并与 recovery child 同一口径

### Requirement: recovery chain 结构完整性与安全遍历

`UNIQUE(superseded_from)` 只保证「一个父至多一个 child」，**不保证链本身合法**（Review 第十三轮 P1-D）——仍可能出现悬空父引用、自环、两节点环、跨 task/conversation/agent 的错链、或 `recovery_attempt` 与 budget 不一致。沿 `superseded_from` 遍历（求 leaf / effective chain state）若不设防，遇损坏链会无限递归或给出错误聚合。故 SHALL 固化下列结构约束与遍历防护：

1. **parent 存在性**：`superseded_from` SHALL 引用一个存在的 parent execution（无原生 FK 时以应用层/迁移探针实现 FK 等价校验）,SHALL NOT 悬空。
2. **禁自环**：`child.id != child.superseded_from`（禁单节点自环）。
3. **scope 一致**：child 与 parent 的 `task_id / conversation_id / agent_slug / recovery_chain_id` SHALL 全部一致——恢复链不跨 task、不跨 conversation、不跨 agent。
4. **recovery_attempt 单调**：`child.recovery_attempt = parent.recovery_attempt + 1`。**`recovery_attempt` 与 `budget_remaining` 在建 child 事务中原子推进（attempt+1、remaining-1），但互不作对方真相源（第十六轮 P1-E 修「同一真相源」旧句）**——`recovery_attempt`=不可回退的 lineage 深度、`budget_remaining`=预算唯一真相源;超 budget（`budget_remaining=0`）SHALL NOT 建 child。
5. **安全遍历**：任何递归 CTE / 应用遍历 SHALL 记录 visited set、设置**最大深度**，检测到环或损坏链时 **fail-closed 并报警**，SHALL NOT 无限递归;progress/Runtime 遇损坏链 SHALL bounded fail-closed（给出「链损坏、需人工」而非卡死或错误完成）。
6. **拒绝错链构造**：人工/迁移构造的自环、两节点环、跨 task parent、跨 conversation/agent parent SHALL 全部被拒绝（建 child 时校验 2/3/4，遍历时校验 5）。

**🔴 recovery chain 根语义与持久化 schema（第十四轮 P1-3，编码前须定死可直接建表迁移）**：结构规则之上 SHALL 固化根初始化、持久化 schema 与最低硬约束层级，SHALL NOT 让实现者自行选择：

```text
普通根 execution（非 recovery child，正常 dispatch/普通 successor 创建）：
  recovery_chain_id = 自身 execution id（run_queue.id）
  recovery_attempt  = 0
recovery child：
  recovery_chain_id = parent.recovery_chain_id
  recovery_attempt  = parent.recovery_attempt + 1
  superseded_from   = parent execution id
```

- **`recovery_requests` 表（人工恢复请求真相源，承接 spec 上文「`manual_recovery_token` 持久化」要求）**：`{parent_execution_id, request_token, payload_hash, child_execution_id, actor, created_at}`，`UNIQUE(parent_execution_id, request_token)`（第二层 API 请求幂等键）。**winner/loser 两分支（第十六轮 P1-E，与上文「不同 token 并发输家」一致、不再矛盾）**：**winner request**（首次成功建 child）SHALL 与 recovery child insert 同 `resume_blocked_execution()` 事务写入（不独立提交，否则「有 request 无 child」孤立行）;**loser request**（不同 token 命中已存在 child）在失败的 child-insert 事务回滚后、SHALL 在**独立事务**里只写 `{本 token, payload_hash → existing child}` 映射、不重复副作用（SHALL NOT 在失败事务里补偿写）。父子基数仍由 `UNIQUE(superseded_from)` 保证，`recovery_requests` 不承担父子基数。
- **`recovery_budgets` 表（恢复预算唯一真相源，chain 维度）**：`{recovery_chain_id (PK), budget_limit, budget_remaining, version, last_reset_by, last_reset_at, last_reset_reason}`。**预算 SHALL 只用 `budget_remaining` 单一真相源（第十五轮 P1-5B：字段名从 `budget_current` 改为 `budget_remaining` 消歧「已消费 vs 剩余」），SHALL NOT 与「按 `recovery_attempt` 比对 `budget_limit`」两套口径并存**（否则 remaining 计数与 lineage depth 两个真相源会漂移）。语义固定：
  ```text
  根 execution 同事务创建 recovery_budgets：budget_remaining = budget_limit，version = 1
  每建 recovery child（同事务）：CAS budget_remaining > 0 AND version = expected
                                SET budget_remaining = budget_remaining - 1, version = version + 1
  budget_remaining = 0 时 SHALL NOT 建 child、落 recovery_blocked(recovery_budget_exhausted)
  recovery_attempt：只表示不可回退的 lineage 深度（parent+1），人工补预算时 SHALL NOT 重置 attempt
  ```
  **人工增/重置预算（`recovery_budget_exhausted` 恢复）SHALL 只保留 `grant_delta`（第十六轮 P1-E#3，删「追加 delta 或覆盖 remaining 二选一」——二选一会产生两套 API/审计行为）**：普通 resume 仅支持 `grant_delta > 0`（`budget_limit += grant_delta`、`budget_remaining += grant_delta`、`version += 1`、写审计 `last_reset_by/at/reason`），与 child insert 同 `resume_blocked_execution()` 事务提交;child 带新预算、SHALL NOT 原值立刻再 blocked。**若必须绝对覆盖 remaining，SHALL 另设独立 admin override API**（带 `expected_version` CAS + before/after 审计），SHALL NOT 与普通恢复混用同一 API/审计路径。
- **最低硬约束层级（用户第十四轮拍板：应用层 + 迁移探针等价校验，不重建表加原生 FK）**：SQLite 无法对已有 `run_queue` 表 `ALTER` 加外键，故 **parent 存在性（规则 1）、scope 一致（规则 3）、recovery_attempt 单调（规则 4）SHALL 由中心化 child-insert 事务 + 迁移/CI 探针做 FK 等价校验**（不悬空、不跨 scope、attempt 连续），SHALL NOT 依赖「先查后插」应用层竞态判断（校验与 insert 须同一 SQL/事务原子完成）。**仅自环（规则 2）可用列级 `CHECK(id != superseded_from)`**（加列时即可带 CHECK，无需重建表）。所有 recovery child 创建 SHALL 经统一 helper（`resume_blocked_execution()` / reclaim 建 child），SHALL NOT 散落多处各写一遍校验。
- **🔴 FK 等价 SHALL 覆盖创建后的删除与修改（第十五轮 P1-5C，不只 child insert）**：应用层 FK 等价若只在建 child 时校验，后续 cleanup/人工修复仍可破坏链完整性。故 SHALL 补：① execution 创建后其 `task_id/conversation_id/agent_slug/recovery_chain_id/recovery_attempt/superseded_from` 字段**永久不可变**（与 conversation 快照不可变一致）;② 删除仍被 child 引用（`EXISTS(superseded_from=该 execution)`）的 parent SHALL 被拒绝，或整条 chain 原子归档;③ cleanup 任务 SHALL NOT 单独删除 chain 中间节点;④ 删除 `recovery_budgets`/`recovery_requests` 行导致链失预算/审计真相源 SHALL 被拒绝;⑤ 周期性/迁移后 integrity probe SHALL 扫描 dangling parent、scope drift、attempt drift、孤立 budget/request;⑥ delete/update 竞态 SHALL 有与 insert 同等级的事务测试。

#### Scenario: 普通根 execution 与 recovery child 的 chain 基值
- **WHEN** 正常 dispatch 创建一个普通根 execution，随后它被 supersede 生成 recovery child，child 再被 supersede 生成孙 child
- **THEN** 根 `recovery_chain_id=自身 id`、`recovery_attempt=0`、`budget_remaining=budget_limit`;child `recovery_chain_id=根 chain_id`、`recovery_attempt=1`;孙 `recovery_attempt=2`，三者 `recovery_chain_id` 相同、每建 child CAS `budget_remaining -= 1`+version+1（唯一真相源，非按 attempt 比对）;普通 successor（非 recovery child）开启自己的新 recovery chain（`recovery_chain_id=自身 id`、`recovery_attempt=0`、新 budget 行）

#### Scenario: recovery_requests 与 child insert 同事务
- **WHEN** `resume_blocked_execution()` 建 recovery child，在写 `recovery_requests` 行与 insert child 之间任一点注入故障
- **THEN** 整体回滚，不留「有 request 无 child」或「有 child 无 request」半提交;`UNIQUE(parent_execution_id, request_token)` 保证同请求重试幂等、父子基数仍由 `UNIQUE(superseded_from)` 保证

#### Scenario: 悬空 parent 被 FK 等价校验拒绝
- **WHEN** 尝试建一个 `superseded_from` 指向不存在 parent execution 的 recovery child
- **THEN** 中心化 child-insert 事务的 FK 等价校验（应用层 + 迁移探针）拒绝该 child，SHALL NOT 悬空;校验与 insert 同一事务原子完成，SHALL NOT 走「先查后插」竞态

#### Scenario: 自环与两节点环被拒绝
- **WHEN** 尝试构造 `child.superseded_from = child.id`（自环）或 A.superseded_from=B 且 B.superseded_from=A（两节点环）
- **THEN** 建 child 校验 `child.id != superseded_from` + scope 一致拒绝自环;遍历 visited set 检测两节点环并 fail-closed 报警，SHALL NOT 无限递归

#### Scenario: 跨 scope parent 被拒绝
- **WHEN** 尝试建一个 parent 与 child 的 `task_id/conversation_id/agent_slug/recovery_chain_id` 不一致的 recovery child
- **THEN** scope 一致性校验拒绝该 child;恢复链 SHALL NOT 跨 task/conversation/agent

#### Scenario: recovery_attempt 必须父+1 且不超 budget
- **WHEN** 建 recovery child 时其 `recovery_attempt` 不等于 `parent.recovery_attempt + 1`，或已达 recovery budget 上限
- **THEN** 分别拒绝创建（attempt 不连续视为损坏链、超 budget（`budget_remaining=0`）落 `recovery_blocked(recovery_budget_exhausted)` 而非再建 child）;recovery_attempt 与 budget_remaining 在建 child 事务原子推进但互不作对方真相源（第十六轮 P1-E）

#### Scenario: 损坏链遍历 bounded fail-closed
- **WHEN** 人工注入一条损坏链（悬空 parent 或超最大深度的 100 层异常链），Runtime/progress 遍历它
- **THEN** 遍历在最大深度处停止并报警、fail-closed（标「链损坏需人工」），SHALL NOT 无限递归、SHALL NOT 错误判为完成

### Requirement: 反向代理平滑切换（自动无损重连）

系统 SHALL 通过反向代理(Nginx)对外暴露统一入口,支持蓝绿切换：更新时起新 API 实例、健康检查通过后切换 upstream 并平滑 reload,旧实例排空退出。SSE SHALL 每 15~30s 主动轮换断开、客户端按 `Last-Event-ID` 自动重连，给旧实例确定的排空上限;切换过程中 SHALL 保证进行中的用户请求正常完成、SSE 经自动重连后状态与日志不重不漏，**验收口径为「自动无损重连」而非绝对「同一连接不断」**。

#### Scenario: 蓝绿切换自动无损重连
- **WHEN** 改代码后起新 API 实例,健康检查通过,反向代理 reload 切换到新实例
- **THEN** 切换期间进行中的请求正常完成、SSE 流(或其重连)不报错,用户几乎无感知

#### Scenario: 新实例健康后才切流量
- **WHEN** 新 API 实例尚未通过 `/api/health`
- **THEN** 反向代理不将流量切到新实例,避免把请求打到未就绪的实例

### Requirement: 历史数据迁移与回滚兼容

新状态机与新列 SHALL 与存量数据兼容，且迁移 SHALL 在 SQLite ALTER 能力限制下（不支持给已有列加约束、加 `UNIQUE`/`NOT NULL`）闭环，并 SHALL 走**真正可回滚的 expand → activate → contract 三段式**（Review P0-2），不在 expand 阶段破坏历史数据。

**`run_queue` 成功态命名 SHALL 保持 `done` 不改**（Review P0-2 更简做法）：`run_queue` 成功继续叫 `done`、`task_runs` 成功叫 `succeeded`，两层各留命名，SHALL NOT 为字符串统一而把历史 `done` 改写成 `succeeded`——旧二进制回滚后仍按 `status='done'`/`IN('queued','running')` 精确判断，改写会导致旧代码漏判。新增状态（`claimed/superseded/recovery_blocked`）SHALL 经 **feature flag** 控制写入：先把「兼容读取版」同时部署到 **API 与 Worker**（都能识别新状态、不误判），再开 flag 允许写入新状态，观察稳定后才进 contract。SHALL NOT 只让 Worker 先兼容——新 Worker 一旦写 `claimed/superseded`，仍在线的旧 API/progress 也会误判任务状态。

`task_runs.run_queue_id + attempt_no` SHALL 分步落地（加两个可空列 → 反向回填存量关联、存量行 `attempt_no` 置 1 → 建 `UNIQUE(run_queue_id, attempt_no) WHERE run_queue_id IS NOT NULL` partial unique index → 新写入路径强制非空且每次 claim 递增 attempt_no），SHALL NOT 追溯要求历史行非空、SHALL NOT 用 `run_queue_id` 单列唯一。折叠 partial unique index 建立前 SHALL 先归并/取消存量重复 active 行。所有新列 SHALL 带安全默认（NULL/0）使存量行走首次执行/全量分支不报错。回滚到旧代码时旧代码 SHALL 能忽略新列、对未知状态保守兜底不崩。迁移前 SHALL 先安全在线备份（`VACUUM INTO`/online backup API，非 cp）。

**旧 `run_queue.task_run_id` 在模型 A 下的语义 SHALL 采用方案③**（Review 第五轮 P1-1 拍板；现有代码把它当「本队列项产生的唯一 `task_runs.id`」用于产出判断与因果链，模型 A 后一个 execution 有多条 attempt、语义失配）。方案③ = 新增两个显式指针 + attempt 反向归属：

```text
run_queue.current_attempt_id   -- 当前在跑 attempt;claim 时原子更新、终态/回队时清 NULL
run_queue.final_attempt_id     -- 最终定局 attempt;execution 落终态时原子更新（Review 第六轮 P1-3 命名拍板：final 而非 winning——失败/被杀 execution 也有 final attempt，「定局」词义偏正向）
task_runs.run_queue_id         -- attempt 反向归属其 execution
```

**两指针 SHALL 按 execution 状态回填/维护（Review 第六轮 P1-3），不是「终态才写 final、其余写 current」的二分**：
- **queued**（含存量首次执行、尚未 claim）：`current_attempt_id = NULL`、`final_attempt_id = NULL`。
- **claimed/running**（在跑）：`current_attempt_id = 当前 attempt`、`final_attempt_id = NULL`。
- **终态**（done/failed/killed/superseded/recovery_blocked）：`current_attempt_id = NULL`、`final_attempt_id = 定局 attempt`（成功=succeeded attempt;失败/被杀=最后一个 running/prestart attempt;superseded=触发交棒的 attempt;**running NULL migration（第十七轮 P0）=该 `orphaned` attempt，`process_cleanup_state` 为 confirmed/unconfirmed 均指同一 orphaned attempt、SHALL NOT 因确认清理改写为 abandoned**;claimed NULL migration=该 `abandoned` attempt）。
- **🔴 终态 `final_attempt_id = NULL` 的唯一例外（第十六轮 P1-B + 第十七轮 P1-C）**：`status=recovery_blocked AND blocked_reason=null_conversation_migration AND terminal_source_status=queued`（从未 claim、无任何 attempt）时 `final_attempt_id = NULL` 合法（引用固化不可变列 `terminal_source_status`，非运行期 `source_status`）。**除此唯一例外，所有终态 SHALL `final_attempt_id` 非 NULL**——SHALL 用 DB CHECK 等价校验（应用层/迁移探针）拒绝其他终态 `final=NULL`;GET detail / Runtime / progress / terminal serializer SHALL 有该空指针分支（recovery_blocked 且 final=NULL 时展示「NULL 会话待迁移」、不报错/不误判）;expand/backfill SHALL NOT 为该例外制造虚假 attempt（queued NULL 隔离为该终态时不回填 final、不新建占位 attempt）。
- **retry 回 queued**（lease 回收/瞬时失败重试）：清 `current_attempt_id = NULL`（旧 attempt 已落 abandoned/failed），下次 claim 再写新 attempt;`final_attempt_id` 保持 NULL 直到真正终态。

expand 期 SHALL 按上表用旧 `task_run_id` + execution 现状态回填两指针（存量终态行→`final_attempt_id`=旧值、`current_attempt_id`=NULL;存量在跑行→`current_attempt_id`=旧值、`final_attempt_id`=NULL;存量 queued 行→两者 NULL）;新消费者切换到读 `final_attempt_id`（终态产出）/`current_attempt_id`（在跑 attempt）后，contract 阶段 SHALL 删除旧 `run_queue.task_run_id` 列。选方案③而非「动态重定义旧列」因其**显式区分「当前尝试」与「最终定局尝试」**、最不易误读。SHALL NOT 保留「旧列语义不变 + 多 attempt」的模糊并存态——否则旧消费者可能读到第一次失败的 attempt 而忽略最终定局 attempt。

**回滚边界 SHALL 收窄为「兼容读取版」而非任意旧版本**（Review 第四轮 P1-3）：expand/activate/contract 的回滚保证是「activate（开新状态写入 flag）后可回滚到**已支持读取新状态的 compatibility release**」，SHALL NOT 声称可回滚到完全不认识 `claimed/superseded/recovery_blocked` 的 pre-expand 二进制。系统 SHALL 明确：① activate 后允许回滚到的**最老 protocol_version / compatibility floor**;② 若必须回滚到 pre-expand 旧版本，SHALL 先关新状态写入 flag、停写并排空在跑 execution、把所有新状态存量处理为旧版本可识别的终态，再启动旧版本;③ `mixed_version_probe` 中的「旧 API」SHALL 指兼容读取版，SHALL NOT 指完全未升级的旧 API。`worker_state.protocol_version` 与 DB schema 不匹配时 SHALL fail-closed 不接管（承接原有约定）。

#### Scenario: 混合版本不误判（真可回滚）
- **WHEN** 新 Worker 已开始写 `claimed/superseded`，但旧 API 实例（尚未升级或已回滚）仍在线读取 run_queue
- **THEN** 因成功态仍叫 `done`、且兼容读取版已先于写入 flag 部署到 API+Worker 两侧，旧 API 不会把 `claimed`/`superseded` 误判为空闲或成功，任务状态不被错误推进

#### Scenario: run_queue_id + attempt_no 分步加约束不伤存量
- **WHEN** 给已有 `task_runs` 表引入 `(run_queue_id, attempt_no)` 关联
- **THEN** 系统先加可空列并回填存量（attempt_no=1）、再建 partial unique index `UNIQUE(run_queue_id, attempt_no) WHERE run_queue_id IS NOT NULL`（容忍历史空值），新写入强制非空且逐次递增 attempt_no，不因 SQLite 无法直接加 NOT NULL/UNIQUE 而失败

#### Scenario: 折叠索引前补列 + 清存量重复
- **WHEN** 建立 `run_queue(conversation_id, agent_slug)` active partial unique index（Review 第五轮 P1-5 方案 B）
- **THEN** 系统先 `ALTER ADD COLUMN conversation_id` 从 tasks 回填、再归并/取消存量同 `(conversation, agent)` 多条 active 行，索引建立不因缺列或存量重复而失败;`conversation_id` 为空的历史 run 不进入该约束

#### Scenario: 多 attempt 下旧 task_run_id 不指向失败 attempt
- **WHEN** 某 execution 的 attempt#1 失败、attempt#2 成功，消费者读取该 execution 的产出/因果链
- **THEN** 消费者读 `run_queue.final_attempt_id`（方案③）得到最终定局 attempt#2，SHALL NOT 仍指向失败的 attempt#1;在跑期读 `current_attempt_id` 得到当前 attempt、终态后 `current_attempt_id` 归 NULL

#### Scenario: 两指针随 execution 状态迁移而维护
- **WHEN** 一个 execution 依次经历 queued → claimed(attempt#1) → running → 瞬时失败回 queued → claimed(attempt#2) → running → 终态 done
- **THEN** 各阶段指针为：queued 时 `current=NULL,final=NULL`;claimed/running attempt#1 时 `current=attempt#1,final=NULL`;回 queued 后 `current=NULL,final=NULL`（attempt#1 落 abandoned/failed）;claimed/running attempt#2 时 `current=attempt#2,final=NULL`;终态 done 时 `current=NULL,final=attempt#2`;SHALL NOT 在在跑期写 `final_attempt_id`、SHALL NOT 在终态后仍留 `current_attempt_id` 非空

#### Scenario: 失败/被杀 execution 也有 final_attempt_id
- **WHEN** 某 execution 的最后一个 attempt 落 `failed`（非重试终局）或 execution 被 `killed`
- **THEN** `final_attempt_id` 指向该最后 attempt（不因「非成功」而留空），`current_attempt_id` 归 NULL;消费者可据 `final_attempt_id` 取到失败/被杀的定局 attempt 做归因，印证「final 而非 winning」命名——终态产出指针不预设成功

#### Scenario: queued NULL 隔离是 final_attempt_id=NULL 的唯一合法例外（第十六轮 P1-B）
- **WHEN** 一个从未 claim 的 `conversation_id IS NULL` queued 行被隔离为 `recovery_blocked(null_conversation_migration)`（无任何 attempt）
- **THEN** 该终态 `final_attempt_id=NULL` 合法（唯一例外：`status=recovery_blocked AND blocked_reason=null_conversation_migration AND terminal_source_status=queued`——**第十七轮 P1-C：例外条件 SHALL 引用持久固化的不可变列 `terminal_source_status`，SHALL NOT 引用运行期推导的 `source_status`**，否则 DB CHECK 无列可依、事后无法判定该行是否合法例外）;`terminal_source_status` 在进入终态那一刻写入、之后不可变;GET detail/Runtime/progress/terminal serializer 走空指针分支展示「NULL 会话待迁移」不报错;expand/backfill SHALL NOT 为它造虚假 attempt

#### Scenario: 非例外终态 final=NULL 被拒绝（第十六轮 P1-B）
- **WHEN** 构造一个 `done`/`failed`/非 null-migration 的 `recovery_blocked` execution 却 `final_attempt_id=NULL`;或构造 `terminal_source_status=claimed/running` 的 null-migration execution 却 final=NULL（claimed/running 必有 attempt）
- **THEN** DB CHECK 等价校验（应用层/迁移探针，引用 `status + blocked_reason + terminal_source_status` 三列）拒绝该状态;final pointer 探针同时覆盖「合法 `terminal_source_status=queued` 例外放行」与「其他终态或 `terminal_source_status=claimed/running` 时 final=NULL 拒绝」两侧

#### Scenario: activate 后仅回滚到 compatibility floor
- **WHEN** 已 activate（开启新状态写入）后需要回滚
- **THEN** 系统只允许回滚到已支持读取 `claimed/superseded/recovery_blocked` 的 compatibility release;尝试启动低于 protocol floor 的更旧 Worker/API 时 readiness fail-closed，须先关写入 flag + 停写排空 + 处理新状态存量后才允许降级到 pre-expand 版本
