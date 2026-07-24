# Design — 平滑重启 / 热更新

> 目标：改代码可随时重启,**用户访问不断、正在跑的 Agent 不中断**（改 API 零中断;改执行层温和重启:能等则零中断,超时中断后 resume 续跑——恢复承诺按三层口径：平台消息 at-least-once / CLI 原生 session best-effort resume / 外部副作用非 exactly-once，**不承诺「上下文必然不丢」**，见 [agent-session-resume] P1-7）。**分期改为阶段 0-6（先补执行协议状态机再实现，Worker 剥离优先、resume 后置）**——见文末「实施阶段」。本文记录架构、执行状态机与不变量、关键取舍与落地细节,**不含代码改动**。
>
> **本文经技术负责人 Review 修订（2026-07-16）**：核心升级 = 从「resume 功能列表」升级为「持久化执行协议」——先定义 durable execution 状态机 + 并发不变量（见「决策 0」），再谈 resume。修正了原方案 3 处事实错误（claim 非原子、`run_queue` 无 conversation_id 列、resume mismatch 判定过严）。

## 现状（实地核查确认）

| 事实 | 位置 | 对平滑重启的影响 |
|------|------|------------------|
| Agent 执行是 API 进程内 asyncio 协程 | `collab._loop` / `_process_one` | 进程停 → 协程消失 → run 被判死 |
| CLI 是本进程 `subprocess.Popen` 子进程 | `executor/claude_code.py`,`codex.py` | 进程停 → 子进程成孤儿/被清 |
| SSE 直接持有 CLI stdout 边读边推 | `routes/runs.py` `event_stream` | 进程停 → 流式断 |
| 任务领取**非原子**（Review 修正） | `collab._claim_one:826`（先 SELECT queued LIMIT 1，再 UPDATE running，UPDATE **无** `AND status='queued'` CAS 条件） | 🔴 单进程 asyncio 下不被打断故当前安全;**多 Worker 会双领双执行**。原方案「已原子/地基已具备」判断**错误** |
| 人工 @ 主受理人**在 API 请求内同步执行**（Review 修正） | `routes/runs.py:53-105` `dispatch` → `runner.execute_dispatch()`，POST 响应即 SSE 流 | 🔴 不经队列/Worker;API 重启直接断;M2「API 不执行 Agent」未覆盖此主路径,必须改两段式 |
| 重复触发是**丢弃**非折叠（Review 修正） | `collab.py:392`（同 task/agent 已有 queued/running 直接 `return None`） | 🔴 新触发被静默丢;且 `run_queue` **无 conversation_id 列**，原 spec 写的 `(conversation_id,agent_slug)` partial index **建不出来** |
| 并发计数在内存 | `collab._running` 集合 | ✋ 需外置到 DB 才能多进程;「COUNT running 再 claim」仍有超卖竞态,需原子容量判断 |
| pid 指纹在内存,子进程**无 containment**（Review 修正） | `executor/runner._RUN_PIDS:17`;`claude_code.py:80`/`codex.py:62` 的 Popen **无** Job Object/`start_new_session` | 🔴 Worker 崩溃后旧 CLI 可能存活成孤儿→与 resume 后的新 CLI **双执行**;内存指纹崩溃即丢 |
| `task_runs` 只存 pid | DB schema `database.py:155` | ✋ 未存 `pid_create_time`/`worker_generation`,重启后无法证明旧进程身份;`task_run↔run_queue` 关联到结束才回填,交棒中途对不上 |

**核心矛盾**：Agent 执行寿命 = API 进程寿命。**解法：把执行层剥离成独立 Worker 进程。**
**但先决条件（Review 核心结论）**：剥离前必须先有**持久化执行状态机 + 并发不变量**（原子 claim、generation/fencing、task_run↔queue 事务化关联），否则多 Worker/交棒下会出现**双执行**与**触发丢失**两类正确性事故。见决策 0。

## 目标架构

```
          用户浏览器
              │
         ┌────▼────┐   Nginx 反代(:8100)  自动无损重连
         │  Nginx   │   upstream → API 实例(蓝/绿)
         └────┬────┘
              │
       ┌──────▼───────┐   塞任务/查状态/写 kill 标记   ┌──────────────┐
       │  API 进程     │ ─────────────────────────────→ │   SQLite      │
       │ (频繁重启)     │ ←─ 尾随 execution_events 转 SSE ─ │  (WAL 模式)    │
       └──────────────┘                               └──────▲───────┘
                                                             │ 写 run_logs/落状态/读 kill·交棒标记/读写 agent_sessions
                                                     ┌───────┴────────┐
                                                     │  Worker 进程     │ (稳定,少重启)
                                                     │ _loop + CLI 子进程│
                                                     └────────────────┘
```

- **API 进程**：HTTP、SSE、路由、业务逻辑——用户频繁改的这里。**不再跑 Agent**,只塞队列 + 尾随 `execution_events` 转发流式（log event 引用 run_logs，见决策 9）+ 写 kill 标记。无状态(状态全在 DB/Worker)→ 可任意重启、可蓝绿。
- **Worker 进程**：`reclaim_orphan_runs` + `_loop` 并发池 + 孤儿巡检 + 起 CLI 子进程。代码稳定,较少重启;要重启(改执行层)时走「温和重启」:先 defer 等在跑 run 自然收尾(能等则零中断);超时才杀在跑 CLI、把 run 标「待续」重入队 → 新 Worker 领取后依 `agent_sessions.session_id` **resume 续跑**。此时 CLI 秒级中断,上下文靠 session 保留,不需从头重跑。

## 关键设计决策

### 决策 0：durable execution 状态机 + 并发不变量（Review 核心，最优先）

一切平滑重启/resume 都建立在一个**持久化执行状态机**上。先定义状态、转换与不变量，再谈剥离与续跑。

**🔴 execution : attempt = 一对多（Review P0-1，用户拍板模型 A）**：一个 `run_queue` 行 = **稳定 execution**（ID 不变，前端订阅/展示的锚点）；每次 claim（含 lease 回收、瞬时失败重试）在其下创建一个新的 **attempt** = 一条 `task_runs` 行，`UNIQUE(run_queue_id, attempt_no)`（**不是** `run_queue_id` 唯一）。这样「claim 建 task_run」与「lease 过期/瞬时失败把同一 run_queue 回 queued 再领」不再撞唯一键——每次领取递增 `attempt_no`，旧 attempt 落 `abandoned`（此处特指 `abandon_reason=lease_reclaim`·claimed 未起 CLI 就丢 = 非定局;`abandoned` 另有 `null_conversation_migration` 定局子类见状态词汇表，第十六轮 P1-A）/`failed`（起 CLI 前失败 = `failure_stage=prestart`，第六轮 P1-2 方案 B）等非定局终态。`run_queue` 的终态由**最终定局 attempt**（`final_attempt_id`，第六轮 P1-3：final 而非 winning——失败/被杀 execution 也有 final attempt）决定。SSE 绑**稳定 execution_id**，同一 execution 的多个 attempt 事件进同一条事件流（见决策 9）。平滑重启产生的 recovery child 仍是**新的 execution**（新 run_queue 行），用 `superseded_from` 关联父 execution——与「同一 execution 内多 attempt」是两个层级：attempt = 同一 execution 的重试尝试，recovery child = 交棒/中断后另起的续跑 execution。

**状态机**（execution = `run_queue` 行；下挂多个 attempt = `task_runs` 行）。**两层成功态不同名**：execution 成功=`done`，attempt 成功=`succeeded`（Review 第四轮 P0-1，单一真相源见 spec「双层状态词汇表」Requirement）：

```
execution 层（run_queue.status）：
queued → claimed → running ─┬─→ done               (成功；由定局 attempt=succeeded 决定，不叫 succeeded)
   ↑         │              ├─→ failed
   └─────────┘              ├─→ killed             (用户主动 kill，不续跑)
 (lease过期/重试            ├─→ superseded         (交棒/reclaim 中断) + 恰好一个 recovery child 入队
  回 queued，新 attempt)     └─→ recovery_blocked   (无法安全恢复，进 dead-letter 待人工)

attempt 层（task_runs.status）：
claimed/preparing → running ─┬─→ succeeded         (该 attempt 成功 = execution 的定局 attempt)
                             ├─→ failed             (含 failure_stage=prestart|running，第六轮 P1-2 方案 B)
                             ├─→ killed
                             ├─→ abandoned          (无残留进程的非成功中止；正交 abandon_stage(prelaunch|running)+abandon_reason(lease_reclaim|null_conversation_migration·claimed·CLI未起|protocol_incompatible)；定局性由 final_attempt_id 引用决定非 status，第十六轮 P1-A + 第十七轮 P0/P1-B)
                             ├─→ orphaned           (进程树未确认退出的孤儿；进程可能存活，非 abandoned，第八轮 P1-B；blocked_reason 子类：unsafe/protocol mismatch=process_not_confirmed_dead / running NULL 隔离=null_conversation_migration，第十七轮 P0/P1-B)
                             └─→ superseded
  失败正交字段：failure_stage(prestart|running) + failure_class(infra|config|business) + retryable(bool)
  abandoned vs orphaned：abandoned=无残留进程（abandon_reason=lease_reclaim·CLI 从未起 / null_conversation_migration·claimed NULL·CLI 未起 / protocol_incompatible，第十五~十七轮）；
                         orphaned=进程树未确认退出（可能存活），blocked_reason 分子类：unsafe/protocol mismatch → recovery_blocked(process_not_confirmed_dead)；
                         running NULL 隔离 → recovery_blocked(null_conversation_migration)+process_cleanup_state(unconfirmed|confirmed)（第十七轮 P0：attempt 恒 orphaned 不改写、进程确认与否走正交字段）；均不得回队。
  ⚠ abandoned 定局性由 final_attempt_id 是否引用决定（第十六轮 P1-A）：lease_reclaim=非定局·execution 回 queued·不被 final 引用；
    null_conversation_migration(claimed)=定局·被 final 引用·driven execution=recovery_blocked(null_conversation_migration)·不回队；
    protocol_incompatible **按预算拆（第十八轮）**：未耗尽预算=非定局·final=NULL·回 queued(与 lease_reclaim 同源)，预算耗尽=定局·被 final 引用·recovery_blocked(protocol_incompatible)。消费者 SHALL NOT 仅凭 status=abandoned 判定局性/回队性。
  ⚠ 回队性是 execution 处置属性、不是 attempt 属性：abandoned 只表「无残留进程」，SHALL NOT 隐含「一定回队」——
    普通 claim lease 回收的 abandoned 走同 execution 回队重试；claimed NULL migration 的 abandoned 其 execution=recovery_blocked(null_conversation_migration) 不回队、等人工迁移。
  ⚠ 第十七轮 P0：running NULL 隔离的 attempt 恒 orphaned（不再随「已确认退出」改写为 abandoned），两阶段清理由 process_cleanup_state + confirm_null_process_cleanup() CAS 表达、attempt 终态不可逆。
```

- **无 accepted 独立态（Review P0-A，用户拍板删）**：`POST /tasks/{id}/dispatch` 在**同一事务**内写用户消息 + 建 `queued` execution，**事务提交后才返回** `execution_id`（= run_queue_id）。不设「已 accepted 但未 queued」的悬空态，省一个 outbox/promoter + orphan sweeper。
- **queued**：进入可领取队列（execution 级，attempt 尚未创建）。
- **claimed**：被某 Worker generation 原子领取（CAS），**同时**创建一个新 attempt（`task_runs` 行，`attempt_no = 上一个 + 1`）+ 在 `run_queue` 写 `claim_lease_until`（Review 第六轮 P0-1：claim 领取租约，区别于 `worker_state.lease_expires_at`;`task_runs` 无 lease 字段）；尚未起 CLI。**崩溃恢复**：`claimed` 超 `claim_lease_until` 未转 running → 接管者 CAS 回收 execution `claimed→queued`（prepare-lease 过期）、把该 attempt 落 `abandoned`，下次 claim 建 `attempt_no+1` 新 attempt 重领。
- **running**：CLI 已起、`pid + pid_create_time + worker_generation + worker_instance_id` 落该 attempt 行。`claimed→running` 亦用 CAS（校验 `claim_owner`/`claim_generation` 未变才转）。
- **终态**：execution 终态 `done`/failed/killed/superseded/recovery_blocked（成功=`done`，非 `succeeded`），由定局 attempt 决定，均以 CAS 落定（自然完成 `SET status='done' WHERE status='running' AND worker_generation=? AND worker_instance_id=?`，或 reclaim 场景的 generation CAS）；定局 attempt 成功=`succeeded` 驱动 execution=`done`，非定局 attempt 落 `abandoned/superseded`（失败 attempt 一律 `failed` + `failure_stage`，第六轮 P1-2 方案 B）。`recovery_blocked` 为 dead-letter 终态，需人工介入，不自动再生成 recovery child。

**状态消费者矩阵（Review P0-A：从阶段 6 前移到阶段 0/1；第九轮补 `orphaned`）**：引入 `claimed`/`superseded`/`orphaned` 后，所有只认 `queued`/`running` 的消费者（progress 聚合、Runtime 总览、任务自动流转、孤儿巡检、失败归因、前端 `RunRow.vue`/`Runtime.vue` 状态色）**必须同步识别新状态**，否则会把 `claimed` 误判为空闲、把 `superseded` 落入成功图标、把 `orphaned` 误当业务 failed 或 abandoned。`orphaned`（第八轮 P1-B）SHALL 全消费者对齐：不计业务失败率、Runtime 显示「孤儿·进程未确认退出」+ 旧 pid/generation + 人工「确认已清理后重试」入口、`final_attempt_id` 指向它、纳入孤儿巡检但不触发自动流转、SSE terminal/recovery payload 带 orphaned+blocked_reason。**orphaned 的 execution `blocked_reason` 按来源子类分（第十七轮 P0/P1-B，消费者 SHALL NOT 假定 orphaned 恒 `process_not_confirmed_dead`）**：unsafe orphan / protocol mismatch（CLI 已起残留）→ `recovery_blocked(process_not_confirmed_dead)`、人工确认清理后建 recovery child；**running NULL 隔离 → `recovery_blocked(null_conversation_migration)` + `process_cleanup_state(unconfirmed|confirmed)`（attempt 恒 orphaned 不改写），`unconfirmed` 显示「孤儿·待确认清理」、`confirmed` 派生展示「已清理·待迁移」，恢复出口是 migration 普通 successor 而非 recovery child**。完整状态矩阵 + 允许转换表在阶段 1 引入状态时同步落地，不留到阶段 6。

**7 条不变量**（实现必须逐条满足）：

1. 一个 queue item 同一时刻只能被**一个 Worker generation** 持有（claim 原子 + generation）。
2. 同一 `(conversation, agent)` 最多**一个 running**，且最多**一个持久化 pending intent**（重复触发合并进 intent，不丢不并发;Review 第六轮 P1-1 粒度改 conversation，NULL 走 task 级兜底索引）。
3. execution 的 `running→done` 与 `running→superseded` 通过 **CAS 竞争，只能一个成功**（自然完成与交棒不互相覆盖）。
4. `superseded + recovery child 入队` 在**同一事务**提交（不出现「旧 run 已 superseded 但无 child」的半提交）。
5. **旧 generation 及同世代旧 attempt 不能 finalize、不能再调用 `jian` 写平台**（fencing 到 attempt 级：generation+instance+`task_runs.status=running`+`run_queue.status=running`+`current_attempt_id==task_run_id` 全匹配才放行——只看 generation 挡不住同世代旧 attempt，见决策 8 fencing 段与 spec「attempt 级 fencing」Requirement，第七轮 P0-4）。
6. recovery chain 有**明确次数上限 + 退避 + dead-letter**（防 crash-loop 无限 resume 烧 token）。
7. `task_run`、`run_queue`、session watermark、消息投递之间有**明确事务边界**（见决策 6 与 [agent-session-resume] 水位）。

### 决策 1：流式改为「SSE 统一事件序列 + 断点续传」（不做「文件唯一真相源+同步器」）

**背景修正**：早期为「托孤」设想过「CLI 输出直写文件、同步器回填 run_logs」——目的是让 CLI 输出脱离 Worker 进程存活,供托孤后新 Worker 续跟。**改走静默+resume 路线后,重启会中断 CLI,CLI 无需脱离进程独立存活,这套重活整段砍掉。**

**保留现状 + 补统一事件序列续传（游标见决策 9，第二十一轮 P0-1 用 `execution_events` per-execution `event_seq`（run_queue 行锁分配）而非 `run_logs.id`、亦非跨 execution 全局自增 id）**：
- CLI stdout 维持经 Worker daemon 线程 → `runner._log()` 写 `run_logs`（**调用入口与日志语义不变**——`_has_jian_deliverable`/`_has_trailing_stdout`/孤儿巡检 last_ts/转写详情消费语义零改动;但 `_log()` 内部落库因「run_log 行 + log event 同事务」要求会改为同事务写 run_logs+execution_events，非整条路径零改动，Review 第四轮 P1-4）;`run_logs` 仅新增 `meta_json` 列承载结构化附加信息。
- 唯一改造:**API 的 SSE 端点改为尾随 `execution_events`**——log 与所有控制事件先落 `execution_events(execution_id, event_seq, ...)` 再推,记已推最大 `event_seq`（第二十一轮 P0-1 per-execution 行锁分配）,轮询/推送新增到前端,直到 run 终态。这样 **API 重启导致 SSE 断连后,前端带 `Last-Event-ID: <execution_id>:<event_seq>` 重连即从断点续推,控制事件与日志统一不重不漏**（含断线期间的 superseded/terminal，见决策 9）,满足「改 API 用户流式自动无损重连」。
- WAL(决策见下)让「Worker 写 + API 读」不互锁。

**SSE 尾随的边界**：
- run 排队未起 → SSE 发「排队中」占位（execution_events 的一条 queued 事件）,Worker 领取起 CLI 后开始有日志。
- run 已终态(用户中途进入) → 一次性回放全量 + 收尾态,不需实时。
- API 重启导致 SSE 断 → 前端带 `Last-Event-ID: <execution_id>:<event_seq>` 重连续传,不重不丢。
- **Worker 温和重启超时后 run 被中断→resume 续跑** → 续跑是**新的一次执行**(新 run),前端 SSE 会看到该 task 的执行链路衔接到新 run(与现有「一个 task 多条 run」的展示一致);被中断的旧 run 落终态(`superseded`)。

### 决策 2：原子 claim + 调度状态外置（Review 修正：claim 必须改成 CAS）

- **原子 claim（P0-2）**：现 `_claim_one` 是「SELECT queued LIMIT 1 → UPDATE running」两步，UPDATE 无 CAS 条件，多 Worker 会双领。改为**单语句条件更新**：
  ```sql
  UPDATE run_queue
     SET status='claimed', claim_owner=?, claim_generation=?, claimed_at=datetime('now'), claim_lease_until=?
   WHERE id=( SELECT q.id FROM run_queue q LEFT JOIN tasks t ON t.id=q.task_id
              WHERE q.status='queued' AND (q.next_retry_at IS NULL OR q.next_retry_at<=datetime('now'))
              ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, q.id LIMIT 1 )
     AND status='queued'
  RETURNING *;
  ```
  `AND status='queued'` 是 CAS 关键：两 Worker 竞争同一行，只有一个 UPDATE 命中。（未来 Postgres 用 `FOR UPDATE SKIP LOCKED + RETURNING`。）
- **并发计数外置**：`_running` 内存 set → `SELECT COUNT(*) ... status IN ('claimed','running')`。**注意**：「先 COUNT 再 claim」在多 Worker 下仍有超卖竞态——单 Worker 用进程内 semaphore 控本地上限即可（本 change 只跑单 Worker）；多 Worker 的原子容量判断归 [platform-concurrency-scaling]。
- **claim 即建 attempt 关联（P0-6，模型 A）**：claim 成功的同一事务内创建**一个新 attempt**（`task_runs` 行）并写 `task_runs.run_queue_id + attempt_no`，约束 `UNIQUE(run_queue_id, attempt_no)`（**非** `run_queue_id` 单列唯一——那会让 lease 回收/重试的第二次 claim 撞键，见决策 0 P0-1）。建立 execution↔attempt 关联不再等执行结束才回填，交棒中途也能对上。`attempt_no` 从该 execution 现有最大 attempt +1 原子取得（同事务 `SELECT COALESCE(MAX(attempt_no),0)+1 ... WHERE run_queue_id=?`）。
- **与 concurrency 的 2.1 协调**：状态外置由本 change 做，concurrency 复用；但「原子 claim CAS」是本 change 必须先补的正确性前提，非「已具备」。

### 决策 3：跨进程 kill——API 写标记,Worker 自杀其子进程

- 现状 kill 靠内存 `_RUN_PIDS`(pid + 创建时间指纹),API 进程内直接 `taskkill`。剥离后 CLI 由 Worker 起,API 不宜直接杀「别的进程起的子进程树」。
- 方案：**API 收到 kill 请求 → 往 DB 写 `kill_requested`(run_queue 或 task_runs 加一列/一张轻表)→ Worker 轮询到标记 → 用自己进程内的 pid 指纹 `taskkill /F /T` 杀进程树 + 落终态。**
- 沿用现有指纹防复用误杀机制;pid 落 `task_runs.pid`(已存在),Worker 重启后由 `reclaim_orphan_runs` 兜底。
- 备选：API 直接按 `task_runs.pid` + 重算创建时间指纹 `taskkill`(Windows taskkill 按 pid 杀,不要求父子关系)。此法少一次轮询延迟,但「谁起谁杀」更清晰的是标记法。落地时二选一,倾向标记法(职责清晰、Worker 掌握自己进程真相)。

### 决策 4：连接平滑用 Nginx（非 Caddy,用户已拍板）

- 取舍：Nginx 更主流通用、团队熟悉度高;代价是要手写 `upstream` + 健康检查 + `nginx -s reload`(Caddy 那些自动)。对蓝绿场景 Nginx 完全够用,`reload` 本身零断连(旧连接跑完、新连接进新 upstream)。
- 蓝绿流程：起新 API 实例(新端口)→ `/api/health` 通过 → 改 upstream 指向新端口 → `nginx -s reload` → 旧 API 排空退出。
- API 无状态,切换无数据迁移成本。

### 决策 5：温和重启（defer 优先）+ resume 续跑（取代托孤）

**目标**：改 `collab`/`executor` 等执行层代码需重启 Worker 时,在跑的 Agent **允许秒级中断,自动续跑、一般无需人工从头重跑**（恢复按三层口径 best-effort：平台消息 at-least-once、CLI 原生 session best-effort resume、外部副作用非 exactly-once;**不承诺上下文必然不丢**）。

**为何弃托孤**：早期方案想让 CLI 脱离 Worker 独立存活(detached)+ Worker 重启后原子认领续跟(worker_id/心跳)+ 收尾重建(结束标记)+ reclaim 语义反转。评估后确认更优路线是**不做进程托孤**,而是「重启前中断 → 重启后用持久化 `session_id` 让 CLI resume 续」。一旦有了 [agent-session-resume] 的 resume 能力,进程级托孤的复杂度(跨进程认领、心跳、双跑防护、收尾解耦、Windows detached 控制台坑)就**不值得**——中断+resume 对多 Agent 平台已足够健壮,且代码量小一个数量级。

**温和重启机制（依赖 [agent-session-resume] 提供 session_id）**：
- **前置**：[agent-session-resume] 已为每个在跑 run 落了 CLI `session_id`（claude 从 stream-json 抓、codex 从 app-server threadId 抓，且流中途 pin 落库防崩溃丢指针），claude 与 codex 均已接入 resume（均必选）。
- **第 0 步 · 温和重启：先 defer 等空闲窗口**：收到重启意图后,若当前 `activeTasks > 0`（有在跑 run）,**优先不中断,等一个空闲窗口**（轮询 `activeTasks`、总计等待上限 **5 分钟**，参数化可调）——能等到所有在跑 run 自然收尾就零中断重启。仅当超过等待上限仍有在跑 run,才进入下面的「中断 + resume」硬路径。经验上多数重启可落在空闲窗口,根本不触发中断。等待期间**停止领新活**,避免边等边来新活永远等不完。
- **交棒流程（DB 标记,Windows 无优雅 SIGTERM,与 kill 标记同机制）**——仅在 defer 超时仍有在跑 run 时触发：
  1. 写「交棒」标记 → 旧 Worker 轮询到 → **停止领新活**。
  2. 旧 Worker 对每个在跑 run:**杀其 CLI 子进程树**（`taskkill /F /T` + pid 指纹防误杀）、把 run 标为 **「待续」(supersede) 并落终态**（旧 run → `superseded`）、按其 `(conversation, agent)` **重新入队一条续跑 run**（携带 `recovery_mode`：有 session=`session_resume`/无=`full_replay` + `superseded_from`）→ 旧 Worker 退出。**child 启动前置 = 「fencing 生效 AND 完整进程树确认退出」双 AND 条件（第八轮 P0-B）**——generation fencing 只挡 `jian` 平台写、挡不住残留 CLI 改文件/shell/外部 API/占资源，故 kill/containment 失败或超时（未确认全树退出）时 SHALL NOT supersede+建 child，落 `recovery_blocked`+`blocked_reason=process_not_confirmed_dead`、保留 pid/generation/instance/containment 信息待人工确认清理后再建 child。
  3. 起新 Worker（新代码）→ 从队列领取续跑 run → 依 `agent_sessions.session_id` resume 启动 CLI → Agent 从上次上下文续跑。
- **续跑喂什么 prompt**：续跑 run **重发原始任务 prompt**（重新 `build_cli_prompt`）+ resume 指针,**不喂空、不造「继续」指令**。靠 resume 恢复记忆 + prompt 约束（「聚焦本轮、只做一次」）防重复。resume 确认未落地时,prompt 前置「上轮会话未能恢复,这是新会话,请如实告知用户」披露。此语义与 agent-session-resume 的常规 resume 统一（见其 design「续跑时的 prompt 语义」）。
- **副作用重复防护（不做服务端 exactly-once）**：被中断的 run 可能已产生副作用（建卡/comment/改状态,已落库）。续跑靠 **session 记忆 + prompt 约束**（「你之前可能已提交过,先检查再动手」「只做一次,即便非零退出也不重试」）防重复,**不引入服务端精确幂等键**。关键写操作（如建卡）可加轻量自然去重,但不追求 exactly-once。
- **续跑不吃防死循环配额（失败原因白名单区分）**：续跑 run 是**系统触发**（非 Agent 自发 @），标记为系统恢复类,**豁免** `MAX_MENTION_CHAIN` 空转链计数、不占 `MAX_RUNS_PER_TASK` 配额,避免频繁重启啃配额甚至误熔断。
- **无 session 可依的 run 的兜底（Review 第六轮 P0-3：统一 recovery child，不落 failed）**：首次执行尚无 session_id、或 poisoned 失败已丢 session 的 run → 无法 resume → **父 execution 落 `superseded`、子 execution 入队 `recovery_mode=full_replay`**（从平台消息重建上下文，等于从头重跑该次分派），SHALL NOT 落 `failed` 再重排队（`failed` 是终态、状态机无 `running→failed→queued`）。codex 已接入 resume,不再是兜底主因。
- 中断到续跑之间秒级;上下文靠 CLI session 保留,不靠进程存活。

**`reclaim_orphan_runs`——先接管定世代、确认死亡再续跑（消除与决策 7 的矛盾）**：
- **🔴 撤销旧的「running 确实都死了」无条件假设**：优雅重启会杀净 CLI（决策 7 containment），但**硬崩溃/断电/被 `kill -9`** 时，CLI 子进程可能成孤儿存活。若此时仍无条件入队续跑，就与决策 7「无法证明旧执行已停 → 不创建 recovery child」直接冲突，且真的会双执行。
- **统一规则 = 先接管（bump generation 定 fencing）→ 再判死 → 才续跑**：
  1. 新 Worker 启动先**接管**：对 `worker_state` 做 CAS `generation=g AND lease_expires_at < now → generation=g+1`（见决策 8），使旧世代 `g` 被 fencing——即便孤儿 CLI 还活着，`jian` 写接口按 generation 拒其写平台（不变量 5），杜绝**双写**这一真正危害。
  2. 对每个残留 `running` run，用持久化的 `pid + pid_create_time` **探活**：进程不存在、或存在但 create-time 不匹配（pid 已被复用）→ 判定旧执行已停。
  3. **仅在「旧执行已确认停 AND 完整进程树确认退出/containment 已清理」时**（第八轮 P0-B：AND 双条件，fencing 生效不足以放行——挡不住残留 CLI 改文件/shell/外部 API），才据 recovery mode 入队一条续跑 run（`superseded_from` 幂等标 + 系统恢复豁免配额）。
  4. **既不能证明已停、又不能保证进程树清理生效** → **不创建 recovery child**，旧 attempt 落 `orphaned`（第八轮 P1-B：running 未确认死亡的孤儿，非 `abandoned`）、该 run 置 `recovery_blocked`+`blocked_reason=process_not_confirmed_dead`（`final_attempt_id` 指向该 orphaned attempt），保留 pid/create_time/generation/instance/containment 信息，等人工确认清理后再建 child，宁可不续不可双执行。
- **无/失效 `session_id` 的 run（第七轮 P0-2：异常 reclaim 与温和交棒统一，不再落 failed/不续）**：确认旧进程已停或已 fencing+进程树清理后 → **父 execution 落 `superseded` + 子 execution 入队 `recovery_mode=full_replay`**（清 session 后从平台消息重建上下文），SHALL NOT 落 `failed` 或「现状兜底不续」。「session 不可用」只决定 recovery mode（`session_resume` vs `full_replay`），**不把基础设施中断记成业务 `failed`、也不丢弃执行意图**。须区分两类：① **attempt 自身业务/模型 poisoned failure**（如 iteration_limit/api 400/语义静默）→ 按 failure/recovery policy 处理（可禁自动重试）;② **Worker 崩溃时发现存量 session 不可用** → 清 session、走 `full_replay` child。
- 好处:复用现有「队列领取 + 起 CLI + 收尾」全链路,收尾幂等由现有 `_finalize_if_running` + generation CAS 保证;fencing + 判死双保险取代「盲目假设已死」;有 session 与无 session 恢复共用同一 `superseded_from`/recovery budget/事件顺序/`recovery_mode` 契约，不出现两套恢复模型。

**防重复续跑**：续跑 run 入队时打标（如 `superseded_from=<旧 run_id>`),避免交棒杀 + reclaim 兜底对同一 run 各入队一次导致双续。以「旧 run 是否已生成续跑 run」为幂等键。

### 决策 6：所有触发统一入队 + 两段式 dispatch（Review P0-1）

现状人工 @ 主受理人走 `dispatch` → API 请求内 `runner.execute_dispatch()` 同步跑 CLI、POST 响应即 SSE。剥离后这条路径要么随 API 重启断、要么违反「API 不执行 Agent」。**改为两段式协议，人工/auto/mention/leader 所有触发统一经持久化队列**：

1. **`POST /tasks/{id}/dispatch`**（提交）：接收客户端 **idempotency key** → **同一事务**内幂等持久化用户消息 + 建 `queued` execution → **提交后返回稳定 `execution_id`(=run_queue_id)**（无独立 accepted 态，见决策 0）。不在请求内跑 CLI。idempotency 作用域 `UNIQUE(task_id, actor_id, idempotency_key)`；同 key 不同 payload 返回 409。
2. **`GET /executions/{execution_id}/events`**（订阅）：独立 SSE，按 execution_id **尾随 `execution_events`**（log event 引用 `run_logs`，控制事件仅在 `execution_events`；游标用 per-execution `event_seq`，见决策 9，第二十一轮 P0-1）；API 重启后可带 `Last-Event-ID: <execution_id>:<event_seq>` 重新订阅。

幂等：同一 idempotency key 重试 POST 只产生一条 user message + 一个 execution。

### 决策 7：进程 containment——子进程随 Worker 死，杀得净（Review P0-5）

现状 claude/codex 的 `Popen` 无 Job Object/`start_new_session`，Worker 崩溃后 CLI 可能成孤儿继续跑，与 resume 后的新 CLI **双执行**。必须：

- **Windows**：把 CLI 挂进 **Job Object** 并设 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`——Worker 进程（持 Job 句柄）死亡时 OS 自动杀整个 Job 内进程树。Job 句柄由 Worker 持有、随 Worker 生命周期存续。
- **POSIX（Review 第五轮 P1-6：`start_new_session=True` ≠ 父死自动清理）**：`start_new_session=True` 只建新 session/进程组，父 Worker 崩溃后子进程通常仍存活，**不单独满足「Worker 死则 CLI 死」**。Linux 部署 SHALL 拍板至少一种真实 containment：子进程 `PR_SET_PDEATHSIG`（并处理 fork/exec 竞态）、或每 attempt 独立 cgroup 由 Worker/supervisor 销毁、或 systemd scope/service 配合 `KillMode=control-group`;并 SHALL 验证孙进程与工具启动的子进程也被清理，不能只检查 CLI 主 pid。
- **🔴 恢复 CLI 前的 generation 最终启动围栏（Review 第五轮 P0-3）**：claim 到实际起 CLI 之间有时间窗（旧 Worker claim 成功 → 心跳暂停 → 新 Worker 接管 generation → 旧 Worker 恢复起 CLI）。若 `claimed→running` 只校验旧 Worker 自己写的 generation、不在启动临界区重读当前 `worker_state`，旧 CLI 仍会跑起来（fencing 挡脏写但挡不住 CLI 自身文件/工具/外部副作用）。启动 SHALL 固定为不可省略顺序：① 起进程前重校验 DB 当前 generation/instance/worker lease/attempt lease，任一不匹配立即 self-fence;② `CREATE_SUSPENDED` 起子进程 → `AssignProcessToJobObject` 挂进 Job（POSIX 等价父死 containment）——**尚未 resume**;③ CAS 持久化 `pid + pid_create_time` 并把 attempt/execution 转 `running`，CAS **同时校验当前 `worker_state` generation/instance/lease**;④ **仅 CAS 成功才 `ResumeThread` 放行子进程**，CAS 失败立即销毁 suspended 进程树（旧 generation 用户代码零执行）;⑤ Worker 心跳/lease 续租失败后 SHALL 停 claim/停 launch/终止本 generation 进程并退出，不靠本地缓存 generation 运行。这把原「CREATE_SUSPENDED→Assign→Resume」顺序中的 Resume 放到 generation CAS 之后，堵死「已跑起才发现世代已变」的窗口。
- **持久化 `pid + pid_create_time + worker_generation + worker_instance_id`**（不再只存 pid、不再只在内存）——重启后可比对 create-time 证明进程身份。
- **恢复前先安全清理旧进程树并确认成功**；无法证明旧执行已停 → **不创建 recovery child**（宁可不续，不可双执行）。
- **fencing 兜底**（不变量 5）：即便清理有遗漏，`jian` 写接口按 generation 拒绝旧进程写平台。

### 决策 8：交棒有 generation + ack + fencing（Review P0-4；单机简化版）

交棒不是「写标记就杀」，要有世代与确认。**单机单 Worker 采用简化模型**（不建多节点的 `worker_instances`/`restart_requests` 表）：

- **`worker_state` 单行轻表**：`current_generation, owner_instance_id, state(running/draining/done), heartbeat_at, lease_expires_at, protocol_version`。`owner_instance_id`=启动时随机世代实例 id（区分「同一 generation 的同一实例」vs 复用 pid 的新实例）;`lease_expires_at`=心跳续租到期点（心跳按周期推进，崩溃后自然过期）;`protocol_version`=DB 协议/字段版本（Worker 与 DB schema 不匹配时 fail-closed，不误接管）。
- **`task_runs.worker_generation` 与 `task_runs.worker_instance_id`**（Review 第四轮 P1-4：fencing 要同时比对 owner instance，故 attempt 行须同时持久化 generation 与 instance id）：每个 attempt 归属的 Worker 世代与实例。
- **两类接管，同一 generation 机制**：
  - **优雅交棒（3 态 ack）**：`running(g) → draining(g)`（旧 Worker 停领、杀在跑 CLI、superseded+入队 recovery child、置 done） → 新 Worker 读到旧 generation `done` 才 **`g+1` 接管**（ack）。
  - **硬崩溃接管（lease 过期 CAS）**：旧 Worker 未置 `done` 就死（无 ack）→ 新 Worker **不能干等 done**，改判 `lease_expires_at < now`（心跳停摆已过期）→ 用 CAS `WHERE current_generation=g AND lease_expires_at<now SET current_generation=g+1, owner_instance_id=<new>, state=running` 抢占接管;抢占后按决策 5「先 fencing 再判死再续跑」处理残留 running run。CAS 保证并发拉起的多个新 Worker 只有一个接管成功。
- **claim 与 draining 同一受保护决策点**：draining 后不再 claim（关闭「停领与新任务刚进」的竞态）。**权威判定 SHALL 在 claim CAS 的 WHERE 内完成**——同一条 UPDATE 校验 `worker_state.state='running'`（非 draining/done）+ `claim_generation==当前活跃 generation` + `owner_instance_id` 匹配 + lease 未过期（见 spec「原子 claim」Requirement 与任务 5.2a），不只靠进程内标志;进程内标志仅作本地快速短路，不作最终依据。单机不需 OS 分布式锁。
- **fencing（attempt 级，第七轮 P0-4）**：`g` 世代的进程/run 不能 finalize 也不能写平台（不变量 5）;**fencing SHALL 到 attempt 级、不止 generation**——只看 `generation==current` 挡不住同世代旧 attempt（retry 残留线程 / 已 terminal 未升 generation / 交棒 kill 不完整）。`jian` 写平台两层全匹配才放行：`worker_generation==current` + `owner_instance_id==current owner`（属于当前 Worker）+ `task_runs.status='running'` + `run_queue.status='running'` + `run_queue.current_attempt_id==task_run_id`（仍是当前合法执行）+（写 session 时）owner token 匹配。交棒杀进程须确认完整进程树退出，kill 失败/超时不直接 supersede+建 child;child 启动前置 = 「fencing 生效 **AND** 完整进程树确认退出/containment 清理」双条件（第八轮 P0-B，SHALL NOT 用「或先完成 fencing」OR 口径——仅 fencing 挡不住残留 CLI 改文件/shell/外部 API），任一未满足转 `recovery_blocked`(`process_not_confirmed_dead`) 不建 child。
- 多个重启请求：单机场景后到的重启覆盖前一个 draining 意图即可，不需 restart_requests 表管合并。

### 决策 9：SSE 续传完整契约——统一事件序列游标（Review P0-7/P0-E）

SSE 不止「带最大 log id 重连」，需完整契约。**核心修正：Last-Event-ID SHALL NOT 用 `run_logs.id`**——控制事件（`queued/run_claimed/run_started/retry_scheduled/superseded/recovery_resumed/terminal`）不写在 `run_logs` 里，用 log id 当游标会导致这些控制事件在重连后**漏投或错序**（例如断线期间刚好发生 superseded，重连按 log id 续传收不到跳转事件，前端卡在死 run）。`recovery_resumed` 是统一恢复控制事件（第十三轮 P1-C，`source=manual|reclaim` 区分来源，删 `manual_recovery` 第二名），写在 recovery child 事件流。

- **新增 `execution_events` 统一事件表 + per-execution 提交顺序安全游标（Review P0-3；第二十一轮 P0-1 改游标）**：`(execution_id, event_seq, event_type, payload_json, created_at, PRIMARY KEY(execution_id, event_seq))`。**游标用 per-execution `event_seq`，由 run_queue 行锁在事务内原子分配**（PG `SELECT next_event_seq FROM run_queue WHERE id=:eid FOR UPDATE` → 用 → +1 同事务提交），**SHALL NOT 用跨 execution 的全局自增 `id`/`BIGSERIAL` 直接充当已提交水位**——PostgreSQL identity 在 INSERT 时分配、不等提交，late-commit 的小 id 会被 reader 永久越过（第二十一轮 P0-1）。SSE 查询 `WHERE execution_id=? AND event_seq > ? ORDER BY event_seq`，`Last-Event-ID: <execution_id>:<event_seq>`。行锁分配保证同 execution 内 seq 分配与提交串行化、无未提交空洞被越过。SQLite 过渡期单写事务下全局 `AUTOINCREMENT` 无 late-commit 问题、可作 event_seq 过渡实现，但契约真相源是 `(execution_id, event_seq)` 语义。**所有** SSE 事件（含 log 与所有控制事件）都先落这张表再推。
- **🔴 双写同事务（Review P0-3，避免半提交）**：状态/数据写入与其对应事件写入 SHALL 在**同一事务**提交，否则会出现「有日志无 log event」「已终态但 SSE 永远收不到 terminal」「recovery child 已建但父 execution 无 superseded 跳转」。必须同事务的组合：
  - POST 用户消息 + queued execution + `queued` event
  - run_log 行 + `log` event
  - `queued→claimed` 状态转换 + `run_claimed` event（领取、CLI 未起，第九轮）
  - `claimed→running` 状态转换 + `run_started` event（CLI 启动，唯一发 run_started）
  - 终态转换 + `terminal` event
  - `superseded` + recovery child 入队 + `superseded` event + child `recovery_resumed{source=reclaim}` event + child `queued` event（第十五轮 P1-1：自动 supersede/reclaim 建的 child 与人工恢复共用统一 `recovery_resumed`，只是 source=reclaim）
- **`run_logs` 加 `meta_json` 列**：承载 log 事件需要的结构化附加信息（channel/tool/tool_input/tool_output 等），使 log 事件可从 run_logs + meta 完整重建，`execution_events` 的 log 行只需引用 run_log id。
- 事件类型：`queued / run_claimed{task_run_id, attempt_no}（领取、CLI 未起，第九轮） / run_started{task_run_id, attempt_no}（CLI 启动） / retry_scheduled{attempt_id, attempt_no, failure_stage, failure_class, retryable, next_retry_at} / log{run_log_id, channel, tool?}（第二十一轮 P1-6：只存引用+路由字段，content/tool_input/tool_output 只落 run_logs+meta_json、SSE 按 run_log_id 联表加载，SHALL NOT 双份存储大工具输出） / superseded{successor_execution_id} / recovery_resumed{source, recovery_reason(handover|orphan_reclaim|manual_blocked_resume), actor?, recovery_parent_id, child_execution_id, blocked_reason?, request_token?}（第十三轮 P1-C + 第十七轮 P2-B，人工/自动恢复统一控制事件、写在 recovery child 事件流、无 `manual_recovery` 第二名;`recovery_reason` 必填、`blocked_reason` 仅承接 recovery_blocked 父时必填、reclaim 交棒 superseded 父时缺省） / terminal{status} / heartbeat`（heartbeat 不落表、不占游标）。**此枚举 SHALL 与 spec 控制事件 Requirement、tasks 1.6/1.6a/1.6c、前端消费者保持完全一致的唯一真相源，只含 `recovery_resumed`（第十四轮 P1-1）。**
- **重连**：客户端带 `Last-Event-ID: <execution_id>:<event_seq>` → 服务端 `SELECT ... FROM execution_events WHERE execution_id=? AND event_seq > ? ORDER BY event_seq` 回放，控制事件与 log 事件**统一有序、不重不漏**（第二十一轮 P0-1 per-execution seq）。
- **superseded 跳转 + per-execution 游标切 child（Review 第四轮 P0-5 删「独立 id 序列」表述；第二十一轮 P0-1 改为 per-execution `event_seq`、废弃「全局 id 必然更大」假设）**：run 被交棒中断 → 同事务按固定插入顺序「建 recovery child execution → 写父 `superseded{successor_execution_id}` event（父 event_seq）→ 写 child 的 `recovery_resumed{source=reclaim}` event（child event_seq=0）→ 写 child 的 `queued` event（child event_seq=1）→ 提交」（第十四轮 P1-1：自动 reclaim/交棒建的 recovery child SHALL 同人工恢复一样发统一 `recovery_resumed`，只是 `source=reclaim` 不带人工 actor）→ 前端收到父 `superseded{successor_execution_id}` 后**据 `successor_execution_id` 显式切到 child、从 child 的 `event_seq=0` 起回放**（`WHERE execution_id=child AND event_seq > -1 ORDER BY event_seq`），无损取到 child recovery_resumed/queued 及后续。断线期间发生的 superseded 因落在 execution_events，重连按父 execution 的 `event_seq` 续传必能收到、再据其切 child。**父子事件序不需要跨 execution 可比**——父子各自 per-execution 单调即可，SHALL NOT 依赖「child event 全局 id 必然大于父 superseded id」这一 late-commit 下不成立的全局假设（第二十一轮 P0-1）。**人工 `POST /resume` 发现的 child 与自动 superseded 跳转发现的 child 订阅口径归一**：都是「切 child execution_id + 从 child event_seq 起点回放」，per-execution 游标下不再有两种分岔口径（见 resume Requirement）。
- queue 尚未建 attempt 时订阅 → 先收 `queued` 占位（同样是 execution_events 的一条）。
- **SSE 主动轮换**：每 15~30s 主动断，客户端按 `Last-Event-ID` 自动重连——给旧 API（蓝绿排空）**确定的排空上限**，不靠「同一连接不断」。
- 事件清理后带过旧 Last-Event-ID → 明确降级（回放可得部分 + 提示当前最新状态）。
- **验收口径**：写成「自动无损重连、控制事件与日志统一有序不重不漏」，**非绝对「同一连接不断」**。

## 落地技术细节

**T0 — 温和重启 defer 窗口（M2.5 前置）**：重启意图到达后先停领新活、轮询 `activeTasks`；全部在跑 run 自然收尾即零中断重启。等待上限 **5 分钟**（参数化可调）内未清零才转入交棒硬路径。参数化配置,避免长任务把重启无限拖住。

**T1 — SSE 统一事件序列 + 断点续传（M1 唯一改造）**：SSE 端点从「直连 CLI stdout 边读边推」改为「尾随 `execution_events`」——所有 log 与控制事件先落 `execution_events(execution_id, event_seq, ...)` 再推（event_seq 由 run_queue 行锁分配，第二十一轮 P0-1）;查 `execution_events WHERE execution_id=? AND event_seq > <前端已收最大 event_seq> ORDER BY event_seq`,轮询(~200ms)推新增。前端重连带 `Last-Event-ID: <execution_id>:<event_seq>` 即从断点续,控制事件与 log 统一不重不漏（含断线期间的 superseded/terminal，见决策 9）。CLI 写 run_logs 的**调用入口与日志语义不变**(Worker 线程 → `_log()`)，但因 run_log 行与其 log event SHALL 同事务提交（决策 9），`_log()` 内部的 DB 写入必然改造为「同事务写 run_logs + execution_events」——表达为「调用入口/日志语义不变、内部落库改同事务」，而非「整条路径零改动」;log 事件引用 run_logs.id + `run_logs.meta_json`。（单机单 API 不需要跨节点消息中继。）

**T2 — 交棒时的中断与重入队（M2.5 核心）**：交棒标记轮询到后,旧 Worker 对每个在跑 run:① `taskkill /F /T` 杀 CLI 进程树(pid 指纹防误杀);② **确认前置**——`taskkill` 只是发起终止，SHALL 校验「fencing 已生效 **AND** 完整进程树已确认退出/containment 已确认清理（所有后代进程，非只根 PID）」双条件（与决策 5「交棒/reclaim child 启动前置」同一真相源），双条件满足才继续 ③;任一未满足 → 父 execution 落 `recovery_blocked`(`blocked_reason=process_not_confirmed_dead`)、**不建 child**、保留 pid/create_time/generation/instance/containment 待人工;③ 父 execution 落终态(`superseded`,**不触发自动流转**——不误判子任务 done/父任务 reviewing);④ **恰好入队一个子 execution（recovery child）**,标 `superseded_from=父` + 系统恢复标记豁免配额,并按有无可用 session_id 设 `recovery_mode`——有 `session_resume`、无 `full_replay`（Review 第六轮 P0-3：不再「无则落 failed 从头」，统一走 recovery child 状态机）。新 Worker 领取子 execution,`session_resume` 重发原 prompt 交 [agent-session-resume] resume 起 CLI、`full_replay` 从平台消息重建上下文起 CLI。**SHALL NOT 用「kill 后即 supersede+child」或「或先完成 generation fencing」的 OR 口径**——仅 fencing 不挡残留 CLI 的文件/shell/外部 API 副作用。

**T3 — reclaim 增强（异常重启也能续，先接管再判死才续）**：`reclaim_orphan_runs` **不再无条件假设「running 已死」**（硬崩溃可能留孤儿 CLI）——先 CAS 升 generation 接管（fencing 旧世代杜绝双写）→ 用 `pid + pid_create_time` 探活判死 → 仅确认停或已 fencing+清理时才据 `session_id` 追加入队续跑 run（`superseded_from` 幂等标）;既不能证明已停又不能保证 fencing/清理 → 置 `recovery_blocked` 进 dead-letter 不建 child（见决策 5/8）。覆盖非交棒的硬崩溃场景。

**T4 — 防双续幂等**：交棒杀 + reclaim 兜底可能对同一旧 run 各触发一次续跑入队。以「旧 run 是否已有 `superseded_from=该run` 的子 run」为幂等键,已有则不再入队,保证一个中断 run 至多一条续跑。

**T5 — kill 标记 vs 交棒标记（两回事，都走 DB 轮询）**：
- **kill 标记**(run 级)：用户主动终止某 run → 杀 CLI + 落终态,**不续跑**(用户就是要停)。
- **交棒标记**(Worker 级)：重启执行层 → 杀所有在跑 CLI + **续跑**。
- 二者语义相反(kill 不续、交棒续),实现上以标记来源区分:交棒触发的中断带 resume 意图,kill 触发的不带。

**T6 — 收尾幂等（复用现状，不需收尾重建）**：续跑是「新 run 走完整现有收尾链路」,旧 run 已由交棒/reclaim 落终态。收尾幂等靠现有 `_finalize_if_running`(`WHERE status='running'` 条件更新)——无需为托孤设计「任意 Worker 基于日志重建收尾」那套。

**T7 — 历史数据迁移闭环（Review P0-F/P0-2）**：新状态机/新列必须与存量数据兼容，SQLite ALTER 能力有限（不支持改列约束、加 `UNIQUE`/`NOT NULL` 到已有列），且迁移须走**真正可回滚的 expand → activate → contract 三段式**（Review P0-2 纠正上一版「一次性改写 done」的不可回滚做法）：
- **🔴 `run_queue` 成功态保持 `done` 不改名（Review P0-2 更简做法）**：`run_queue` 成功继续叫 `done`、`task_runs` 成功叫 `succeeded`，两层各留命名，**不为字符串统一而把历史 `done` 改写成 `succeeded`**。原因：旧二进制回滚后仍按 `status='done'` / `IN('queued','running')` 精确判断，改写会让回滚出来的旧代码漏判既有记录。上一版「一次性 `UPDATE ... SET status='succeeded' WHERE status='done'` + 读取层兼容映射」被否——读取映射只帮新代码，救不了被回滚的旧二进制。
- **新状态经 feature flag 灰度**：新增 `claimed|superseded|recovery_blocked`（`succeeded` 仅用于 task_runs）。部署顺序 = **expand（加 schema/列，暂不写新状态）→ API + Worker 全部部署「兼容读取版」（都能识别新状态不误判）→ feature flag 开启新状态/新 payload 写入 → 观察稳定 → contract 清理**。**不能只让 Worker 先兼容**：新 Worker 一旦写 `claimed/superseded`，仍在线的旧 API/progress 也会误判任务状态。
- **`task_runs.run_queue_id + attempt_no`（模型 A，非单列唯一）建列**：SQLite 不能直接给已有表加 `NOT NULL UNIQUE` 列。分步：① `ALTER ADD COLUMN run_queue_id INTEGER` + `ALTER ADD COLUMN attempt_no INTEGER`（均可空）;② 用现有 `run_queue.task_run_id` 反向回填存量关联、存量行 `attempt_no=1`;③ 建 `CREATE UNIQUE INDEX ... ON task_runs(run_queue_id, attempt_no) WHERE run_queue_id IS NOT NULL`（partial unique 容忍存量空值，**复合键**而非 `run_queue_id` 单列——见决策 0 P0-1）;④ 新代码写入路径强制非空、每次 claim 递增 attempt_no，旧行保持可空——不追溯改历史行。
- **折叠 partial unique index 前先补列 + 清存量重复 active 行**（见 ASR 决策 1b;Review 第五轮 P1-5 拍板方案 B 改 conversation 粒度）：`run_queue` 现状无 `conversation_id` 列，先 `ALTER ADD COLUMN conversation_id`（从 `tasks.conversation_id` 回填）→ 归并/取消存量同 `(conversation, agent)` 多 active 行 → 建 `partial unique index ON run_queue(conversation_id, agent_slug) WHERE status IN ('queued','claimed','running')`;`conversation_id` 为空的历史 run 不进入该约束范围（避免 NULL 撞键）。
- **新列默认值保证旧行可用**：`worker_generation`/`pid_create_time`/`owner_instance_id`/`planned_through_msg_id`/`committed_msg_id`/`meta_json` 等新列 SHALL 带安全默认（NULL 或 0），存量行走「首次执行/全量回灌」自然分支，不因缺值报错。
- **回滚兼容**：阶段回退到旧代码时，旧代码 SHALL 能忽略新列/新状态（读到未知状态按保守兜底），不因 schema 前进而崩。
- **迁移前安全在线备份**（`VACUUM INTO`/online backup API，非 cp，WAL 下 cp 漏 `-wal`），失败可回滚到备份。

## 实施阶段（Review 修订：Worker 先、resume 后，先补状态机）

| 阶段 | 交付 | 用户价值/目的 | 验证门 |
|------|------|----------------|------|
| **0 补规格** | 本文档决策 0 状态机 + 7 不变量 + 修正 3 处事实错误 | 把「resume 功能列表」升级为「持久化执行协议」 | OpenSpec `--strict` + Review 认可 |
| **1 DB 协议地基** | WAL+每连接 busy_timeout+安全在线备份+索引;所有 dispatch 统一入队;POST 幂等+GET 订阅;**原子 claim(CAS)**;触发合并+delivery receipt;`task_run↔queue` 不可变关联 | 消灭双领与触发丢失;流式可重连 | `atomic_claim_probe`(N并发只1中)、`sse_tail_probe`、`dispatch_idempotency_probe`、`trigger_coalesce_probe` |
| **2 Worker 剥离** | 独立 `worker.py`;API 不执行 Agent;supervisor/heartbeat/readiness;generation/lease/单实例;**Job Object/进程组 containment**;kill 带 request/ack | **重启 API 不断 Agent**;Worker 崩溃可被拉起、旧进程可清 | `worker_split_probe`、`worker_containment_probe`(崩溃后无孤儿 CLI)、`kill_ack_probe` |
| **3 Claude resume** | `agent_sessions`;流中 pin;committed/planned 水位;`--resume`;mismatch/失败降级;poisoned 分类 | claude 省 token+上下文连贯（[agent-session-resume]） | ASR S1-S3 探针 |
| **4 Codex app-server** | 每 run 一个 app-server;`thread/resume`→`thread/start`;threadId pin;rollout/workdir 检查;transport fail-fast | codex 同享 resume | ASR S4 探针 |
| **5 交棒 + 有界恢复** | restart generation;claim barrier;defer 5min 等自然收尾;超时 kill+**CAS supersede+recovery child 同事务**;bounded recovery/退避/dead-letter;stale generation fencing | **改执行层:能等则零中断;超时 resume 续跑（best-effort 恢复，非「上下文必然不丢」）** | `worker_handover_probe`、`recovery_budget_probe`、`fencing_probe` |
| **6 Nginx 蓝绿** | readiness 后切 upstream;SSE 主动轮换+Last-Event-ID;旧 API drain timeout;expand/contract 部署 | **改代码用户连接自动无损重连** | 手动压测切换 + `blue_green_probe` |

- **顺序**：0 → 1 → 2 → 3 → 4 → 5 → 6。阶段 5（交棒续跑）依赖阶段 3/4（resume）+ 阶段 2（Worker+generation）；阶段 6 依赖阶段 2（API 无状态）。
- **达标线**：阶段 1+2 = 「改 API 平滑、消灭双执行地基」;阶段 3+4+5 = 「改执行层温和重启、resume 续跑」;阶段 6 = 「连接无损重连」。
- **本 change 覆盖阶段 0/1/2/5/6**;阶段 3/4（resume 本身）归 [agent-session-resume]。
- **回退**：各阶段独立;阶段 5 遇阻可停在阶段 2（改 API 已平滑、改执行层暂退回「重启从头重跑」）。

### 验收定义（Review）

- **API 平滑重启**：Agent 继续跑;客户端有界时间内自动重连;状态与日志不重不漏;POST 重试不产生重复任务。
- **Worker 温和重启**：drain 窗口内结束的 run 零中断;超时的 run 安全停 + 旧 run superseded + **恰好一个** recovery child;**无旧 CLI 与恢复 CLI 双执行**;恢复保上下文但**副作用语义明确为 at-least-once**。
- **Worker 异常崩溃**：supervisor 拉起;旧进程树确认清理或 fencing;orphan 被恢复或进 dead-letter;恢复次数受限。

## 与 [platform-concurrency-scaling] 的协调（避免重复/冲突）

| 事项 | 本 change (graceful-restart) | concurrency-scaling | 约定 |
|------|------------------------------|---------------------|------|
| WAL + busy_timeout | M1 | 阶段 0.1 | **同一件事,谁先做另一个即满足** |
| 调度状态外置(`_running`/pid→DB) | M2 | 阶段 2.1 | **由本 change M2 实现,concurrency 2.1 复用** |
| 多 worker 无状态消费 | M2 达成「可多进程消费」地基(先跑单 worker) | 阶段 2.2 | 本 change 出地基,concurrency 2.2 在其上做「多 worker 并发规模与公平」 |
| `_claim_one` 原子领取 | **本 change 阶段1 补 CAS**（现状非原子，Review 修正） | 复用本 change 成果 | 原判断「已具备」错误，须先补 |
| 同 slug 串行 / 公平调度 / Postgres | 不涉及 | concurrency 阶段 0/1 负责 | 本 change 不碰,避免动机混淆 |

**分工原则**：本 change 只解决「解耦 + 平滑重启」(单 API + 单 Worker);「多 worker 并发规模、项目公平、Postgres」归 concurrency。两者共享「WAL + 状态外置」地基,一次实现、双方受益。

## 非目标（本 change 明确不做）

- 不做 Postgres 迁移(归 concurrency 阶段 1)。
- 不做多 worker 并发规模与项目公平调度(归 concurrency 阶段 2.2/1.3)。本 change 出「多进程可消费」地基,但只跑单 Worker。
- 不做代码级热替换(`importlib.reload` 对有状态长运行服务不可靠,已排除)。
- **不做 CLI 进程托孤**(detached 独立存活、worker_id 认领、收尾重建、reclaim 语义反转)——已被静默+resume 路线取代,复杂度不值(见决策 5)。
- **不实现 resume 本身**——resume(session_id 抓取/存储/`--resume`/增量回灌)归独立 change [agent-session-resume];本 change 只负责「重启时中断 + 重入队触发续跑」。
- 不追求逐字符零延迟流式(SSE 尾随近实时已满足体验)。
- 不改 Agent 执行模型/不压缩硬墙钟;用 resume 续跑（best-effort 恢复，非「上下文必然不丢」）解决长任务重启续跑,而非把任务拆短或让 CLI 托孤长活。

