# Tasks — platform-graceful-restart

> 规划态,分阶段落实,**每阶段验证门通过才进下一步**;每阶段含「代码 + 新探针 + 全量回归」,一步一提交、可独立回退。经技术负责人 Review 修订（2026-07-16）：先补执行协议状态机，Worker 剥离优先、resume 后置。

## 跨-change 执行顺序（Review 修订版：阶段 0-6，Worker 先、resume 后）

| 阶段 | 归属 | 内容 | 依赖 | 验证门 |
|---|---|------|------|------|
| **0** | 本 change | 补 durable execution 状态机 + 7 不变量 + 修正 3 处事实错误 | — | `--strict` + Review 认可 |
| **1** | 本 change | DB 协议地基：WAL/备份/索引、统一入队、两段式 dispatch、原子 claim(CAS)、触发合并、task_run↔queue 关联、统一 finish_execution 收尾事务 | 0 | `atomic_claim_probe`/`sse_tail_probe`/`dispatch_idempotency_probe`/`trigger_coalesce_probe`/`combined_finish_transaction_probe` |
| **2** | 本 change | Worker 剥离 + supervisor/heartbeat + generation + 进程 containment + kill request/ack | 1 | `worker_split_probe`/`worker_containment_probe`/`kill_ack_probe` |
| **3** | [agent-session-resume] | Claude resume（建表/pin/水位/降级/poisoned） | 1 | ASR S1-S3 探针 |
| **4** | [agent-session-resume] | Codex app-server（每 run 一进程/thread resume） | 1 | ASR S4 探针 |
| **5** | 本 change | 交棒 + 有界恢复：generation/claim barrier/defer 5min/CAS supersede+child 同事务/bounded recovery/fencing/launch 围栏 | 3·4·2 | `worker_handover_probe`/`recovery_budget_probe`/`fencing_probe`/`launch_epoch_barrier_probe` |
| **6** | 本 change | Nginx 蓝绿 + SSE 主动轮换 + expand/contract | 2 | 手动压测 + `blue_green_probe` |

**阶段 capability matrix（Review 第五轮 P1-3 + 第六轮 P0-2：消除前向依赖 + 阶段1拆可执行子阶段）**——阶段 1 的原子 claim 必须写 `status='claimed'`、依赖 `worker_state` 的 generation/owner/lease，故**最小 generation/lease 地基前移到阶段 1**；且**新状态的 activate 不能拖到阶段 6**（否则阶段 1~5 跑不了 claimed），阶段 1 SHALL 拆成 expand→compatibility→activate→observe 四子阶段，阶段 6 只做 Nginx 蓝绿与最终 contract、**不再首次 activate 核心状态机**：

| 阶段 | 已存在字段/表 | feature flag 状态 | 允许 Worker 数 | 必过验证门 |
|---|---|---|---|---|
| 1A expand | 加 execution/attempt/event/`worker_state` 最小地基列/表/索引；仍用旧写协议 | 新状态写入关闭 | 单 Worker | 迁移不伤存量、`--strict` |
| 1B compatibility | API + 当前内嵌执行端/Worker 全部升级为**能读取新状态的兼容版** | 新状态写入仍关闭 | 单 Worker | 混合版本不误判 |
| 1C activate | 开启 `claimed`、新 dispatch、attempt/event/`finish_execution` 协议 | **新状态写入打开** | 单 Worker | 原子 claim 写 claimed / finish 事务 |
| 1D observe | 跑阶段 1 探针并观察 | 开 | 单 Worker | 失败只回滚到 compatibility floor |
| 2 | Worker 独立进程与 containment（**依赖 1C/1D**） | API 内执行关闭 | 单 Worker | API 重启不中断 Worker |
| 3/4 | session owner、水位、resume | backend 分别灰度 | 单 Worker | resume/fallback 探针 |
| 5 | generation/lease/接管**完整协议** | recovery 打开 | 单 active generation | launch/fencing 探针 |
| 6 | 蓝绿 API + **最终 contract** | 路由切换打开（**不再首次 activate 状态机**） | 双 API | SSE/兼容回滚探针 |

阶段 1 的 `worker_state` 最小地基只需保证「单 Worker 恒为当前 generation/owner、lease 持续续租」，claim CAS 即可稳定校验 generation/owner/lease;完整的多 generation 接管/交棒仍在阶段 5。**Worker readiness 在 1C 未完成时对新协议 fail-closed**（阶段 2 不得在 activate 前起）。

**执行纪律**：① 验证门不过不推进;② 每阶段可独立回退;③ 需重启验证先问用户、按单实例流程;④ DB 建表/加列前用**安全在线备份**（WAL 下不可直接 cp，见 1.1b）。**本 change 覆盖阶段 0/1/2/5/6;阶段 3/4 归 [agent-session-resume]。**

## 阶段 0 — 补执行协议规格（纯文档，已完成）
- [x] 0.1 design 决策 0：execution 状态机 `queued→claimed→running→{done/failed/killed/superseded/recovery_blocked}`（**execution 成功=`done` 非 `succeeded`；`succeeded` 属 attempt 层**，Review 第四轮 P0-1;**删 accepted，Review P0-A**）+ 7 不变量 + claimed lease 恢复 + 状态消费者矩阵前移（已写入）
- [x] 0.2 spec 新增 Requirement：状态机与不变量、原子 claim、两段式 dispatch、进程 containment、generation/ack/fencing、SSE 统一事件序列、历史数据迁移（已写入）
- [x] 0.3 修正 3 处事实错误：claim 非原子、`run_queue` 无 conversation_id 列（折叠改用 `(task_id,agent_slug)` partial index）、resume mismatch 判定（本 change + ASR 已改）
- [x] 0.4 第二轮 Review 6 个 P0（A-F）+ 关键 P1 全部落文档：删 accepted、折叠 DB 唯一性、连续前缀分批、硬崩溃 generation 接管、SSE 统一游标、历史迁移闭环
- [x] 0.5 第三轮 Review 4 个 P0 + 7 个 P1 全部落文档：① P0-1 execution:attempt 一对多（模型 A，`UNIQUE(run_queue_id, attempt_no)`）② P0-2 done 不改名 + expand/activate/contract 真可回滚 ③ P0-3 event 全局自增游标 + 双写同事务 ④ P0-4 backlog 自动续批 + `batch_scan_end` 原始扫描水位统一（后续轮次改名，替代早期 `committed_batch_end` 提法）;P1：claim 真原子/worker_instance_id/session CAS 字段/1.4 claimed 折叠/状态矩阵前移阶段1/lease 时间语义/recovery_blocked 产品闭环
- [x] 0.6 第四轮 Review P0/P1 落文档：① P0-1 全文统一 `run_queue.done`/`task_runs.succeeded` + 双层状态词汇表（spec 新增 Requirement，proposal/design/tasks/枚举/前端文案单一真相源）② P0-5 删除「successor 独立 id 序列」表述、统一全局 Last-Event-ID + 固定事件插入顺序 ③ P1-1 模型 A 下旧 `run_queue.task_run_id` 语义收口 ④ P1-2 attempt 状态消费者矩阵 ⑤ P1-3 回滚 floor 收窄为 compatibility protocol floor ⑥ P1-4 design 残留旧表述同步（seq→全局 id、worker_instance_id、_log 措辞）
- [x] 0.7a 第五轮 Review 4 P0 + 9 P1 落文档：backlog 方案A（默认关闭+observe 硬门禁）、conversation 粒度 active 索引、统一 `finish_execution()` 收尾事务、launch generation 最终启动围栏、旧 task_run_id 语义方案③
- [x] 0.7b 第六轮 Review 3 P0 + 9 P1 落文档：P0-1 session owner acquire lease 字段归一（`run_queue.claim_lease_until` / `worker_state.lease_expires_at`，五档资格谓词）、P0-2 阶段1拆 1A/1B/1C/1D 且 activate 前移出阶段6、P0-3 无 session 恢复统一 full_replay recovery child;P1：conversation 粒度全文统一 + NULL 两互补索引、prestart_failed 归一 `failed`+`failure_stage`、指针改 `current_attempt_id`+`final_attempt_id` 状态感知回填、POSIX CAS 前启动 gate、finish 路径字段矩阵、多 Worker owner 模型阻塞 concurrency 阶段2、backlog 原消息至少完整投递一次、心跳失败分级 self-fence、文档残余清理
- [ ] 0.8 **文档一致性全文清理 + `spec_consistency_probe`（Review 第六轮 P1-9）**：阶段 0 用 `rg` 对旧粒度（`(task_id, agent_slug)` 非 NULL 兜底语境）、旧水位（`committed_batch_end` 非「历史替代说明」语境）、强承诺（「上下文不丢/续跑不丢上下文」）、过时字段（`prestart_failed` 状态名、`winning_attempt_id`、`lease_until` 裸名）、`start_new_session` 冒充 containment 等做一次全文清理;`spec_consistency_probe` = CI 静态检查禁止词/字段清单（除白名单的「已替代/已取消」说明行外，三份 change 正文不得出现上述过时提法）
- [ ] 0.7 `--strict` 校验通过 + 当前轮 Review 复核认可（放行阶段 1）；本轮尚待技术负责人勾选 `0.7`

## 阶段 1 — DB 协议地基（消灭双领与触发丢失；流式可重连）
- [ ] 1.1 SQLite 连接开 `PRAGMA journal_mode=WAL`（初始化一次）+ `PRAGMA busy_timeout`（**每条新连接**都设）
- [ ] 1.1b 安全在线备份：改用 SQLite online backup API 或 `VACUUM INTO`（**不可直接 cp 主库**——WAL 下会漏未 checkpoint 的 `-wal` 数据）；建表/加列前先备份
- [ ] 1.1c 加索引：`run_logs(run_id,id)`、`run_queue(status,next_retry_at,id)`、active 唯一索引**两组互补**（第六轮 P1-1 conversation 粒度）——`UNIQUE(conversation_id, agent_slug) WHERE conversation_id IS NOT NULL AND status IN ('queued','claimed','running')` + `UNIQUE(task_id, agent_slug) WHERE conversation_id IS NULL AND status IN ('queued','claimed','running')`（先补 `run_queue.conversation_id` 列并归并存量，见 1.5b②）
- [ ] 1.2 **原子 claim(CAS)**：`_claim_one` 改单语句条件更新（`UPDATE...WHERE id=(子查询) AND status='queued' RETURNING *`），杜绝双领
- [ ] 1.3 **统一入队 + 两段式 dispatch**：所有触发（人工@/auto/mention/leader）经队列;`POST /tasks/{id}/dispatch` 接 idempotency key、幂等持久化用户消息、入队、返回 `execution_id`,**不在请求内跑 CLI**;`GET /executions/{execution_id}/events` 独立 SSE 订阅
- [ ] 1.4 **触发合并（不丢，非丢弃；含 claimed 与完整终态折叠，Review P1-4）**：同 (conversation,agent)（第六轮 P1-1 粒度，NULL 走 task 兜底）已有 queued→原子 `MAX/COALESCE` 合并消息水位到该行;**claimed/preparing→同样记 pending intent**（不能漏在 queued/running 两分法之外）;running→持久化 `rerun_requested`+`pending_through_message_id`+触发来源;收尾事务内据 pending 至多建一个 successor。各终态折叠规则：**failed**→仍据 pending 建 successor;**killed**→取消 pending 不建;**superseded**→pending 由 recovery child 继承不另建
- [ ] 1.4a **状态消费者矩阵前移到阶段 1（Review P0-A/P1-5）**：本阶段引入 claimed/superseded/recovery_blocked 的同时，同步更新所有状态消费者（progress 聚合、Runtime 总览、任务自动流转、孤儿巡检、失败归因、前端 RunRow.vue/Runtime.vue 状态色）识别新状态 + 落地完整状态矩阵与允许转换表。**不留到阶段 6**（阶段 6 的同名任务删除，见 6.x）
- [ ] 1.5 **execution:attempt 一对多关联（Review P0-1，模型 A）**：`run_queue`=稳定 execution，`task_runs`=attempt。claim 同事务建**新 attempt**（`task_runs` 行，`attempt_no = SELECT COALESCE(MAX(attempt_no),0)+1 WHERE run_queue_id=?`）+ 写 `run_queue_id + attempt_no`;约束 `UNIQUE(run_queue_id, attempt_no)`（**非** `run_queue_id` 单列唯一——否则 lease 回收/重试第二次 claim 撞键）。SQLite 分步——`ALTER ADD COLUMN run_queue_id INTEGER` + `ALTER ADD COLUMN attempt_no INTEGER`(均可空) → 反向回填存量(attempt_no=1) → `CREATE UNIQUE INDEX ... ON task_runs(run_queue_id, attempt_no) WHERE run_queue_id IS NOT NULL` → 新写入强制非空且递增。lease 回收/瞬时失败重试：execution 回 queued、旧 attempt 落 `abandoned`（或 `failed`+`failure_stage=prestart`，第六轮 P1-2 方案 B），下次 claim 建 attempt_no+1;execution 终态由获胜 attempt 决定
- [ ] 1.5a **改造现有 retry 与 attempt 模型对齐（Review P0-1）**：`collab.py:895` 现在把同一 run_queue 行改回 `queued` 重试——保留此 execution 级回队机制，但下次领取时**新建 attempt 行**而非复用/覆盖，使 `_process_one` 的异常重试与 claimed lease 回收统一走 attempt 模型;SSE 事件绑稳定 execution_id（run_queue.id），多 attempt 事件进同一流
- [ ] 1.5a2 **旧 `run_queue.task_run_id` 语义收口 = 方案③（Review 第五轮 P1-1 拍板 + 第六轮 P1-3 命名/回填细化）**：新增 `run_queue.current_attempt_id`（当前在跑 attempt;claim 写、终态/回队清 NULL）+ `run_queue.final_attempt_id`（最终定局 attempt;终态时原子更新——命名用 **final 而非 winning**，失败/被杀 execution 也有 final attempt）+ `task_runs.run_queue_id`（反向归属）;两指针 SHALL 按 execution 状态维护：queued→`current=NULL,final=NULL`;claimed/running→`current=当前,final=NULL`;终态→`current=NULL,final=定局`;retry 回 queued→清 `current=NULL`。expand 期按 execution 现状态由旧 `task_run_id` 回填两指针（终态行→final、在跑行→current、queued 行→均 NULL），新消费者切读 final/current 后 contract 删旧列。**禁止「旧语义不变+多 attempt」模糊并存**（旧消费者会读到失败 attempt 而漏定局 attempt）;探针 `legacy_task_run_pointer_probe`（attempt#1 失败、#2 成功，验消费者读 `final_attempt_id` 得 #2、旧列不指向失败 attempt 或已不被读取）、`attempt_pointer_migration_probe`（存量 queued/在跑/终态三类行回填后两指针符合状态表）、`attempt_pointer_retry_probe`（瞬时失败回 queued 后 `current_attempt_id` 归 NULL、`final_attempt_id` 仍 NULL，下次 claim 才写新 current）
- [ ] 1.5a3 **attempt 状态消费者矩阵（Review 第四轮 P1-2）**：补 `task_runs` attempt 层消费矩阵——各终态（succeeded/failed/killed/abandoned/superseded）是否计失败率、Runtime/RunRow 展示、是否触发任务失败/自动流转;失败统一为 `failed` 并用正交字段 `failure_stage(prestart|running)+failure_class(infrastructure|configuration|business)+retryable` 区分（第六轮 P1-2 方案 B，不再单列 `prestart_failed`）;retryable failure（不论 stage）用 `failed`+恢复计数、回队重试不计失败率;claimed 阶段被 kill→attempt/execution=`killed`;非重试 prestart failure→`failed`+`failure_stage=prestart`+`retryable=false`（终局失败，计失败率）;非获胜 attempt（abandoned/superseded）SHALL NOT 计失败率或触发流转;attempt 允许转换表与 execution 转换表一并落地
- [ ] 1.5b **阶段1 四子阶段 expand→compatibility→activate→observe（Review P0-F/P0-2 + 第六轮 P0-2）**：**1A expand** 加 schema/列/表/索引、仍用旧写协议不写新状态;**1B compatibility** API+执行端/Worker 全部升级为能读新状态的兼容版（**不能只让 Worker 先兼容**，旧 API 在线会误判）;**1C activate** 开 feature flag 写 `claimed`/新 dispatch/attempt/event/`finish_execution` 协议;**1D observe** 跑探针、失败只回滚到 compatibility floor。① **`run_queue` 成功态保持 `done` 不改名**（task_runs 成功=`succeeded`，不改写历史 `done`）;② `run_queue` active partial unique index 建立前先补 `conversation_id` 列并归并/取消存量同 `(conversation,agent)` 多 active 行;③ 新列带安全默认（NULL/0）存量走首次执行/全量分支不报错;④ 迁移前先 `VACUUM INTO` 安全备份（承接 1.1b）可回滚;探针 `protocol_flag_stage_probe`(1A/1B 无新状态、1C 后 claim 才写 claimed)、`stage_dependency_probe`(1C 未完成时阶段2 Worker readiness fail-closed)、`compatibility_activate_rollback_probe`(1C 后只回 compatibility floor;关 flag+排空+转存量后才回 pre-expand)
- [ ] 1.6 **SSE 统一事件表 + 全局自增游标 + 双写同事务（Review P0-E/P0-3）**：建 `execution_events(id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id, event_type, payload_json, created_at)`;**游标用全局自增 `id`**（非自算 `MAX(seq)+1`——并发写会竞争/撞键;间隙无所谓）;`run_logs` 加 `meta_json` 列（channel/tool/tool_input/tool_output）;所有事件（log + 控制事件 `queued/run_started/superseded/terminal`）先落表再推，`id:<全局id>`+`Last-Event-ID` 为唯一游标（heartbeat 不落表）;断连 `WHERE execution_id=? AND id>? ORDER BY id` 回放
- [ ] 1.6a **双写同事务（Review P0-3）**：状态/数据写入与其事件写入同一事务提交——POST 消息+queued execution+queued event;run_log+log event;claimed/running+run_started event;终态+terminal event;superseded+recovery child+superseded event。防「有日志无 event」「已终态无 terminal」「有 child 无 superseded 跳转」半提交;successor 切换时客户端沿用同一条全局 `id` 游标（Review 第四轮 P0-5：无 per-execution 独立序列），靠固定事件插入顺序「建 child→写父 superseded event→写 child queued event→提交」保证 child queued 全局 id 大于父 superseded，`WHERE execution_id=child AND id>last_global_id` 无损续订
- [ ] 1.6b **🔴 统一收尾事务 `finish_execution()`（Review 第五轮 P0-4 + 第六轮 P1-5 字段矩阵，跨 PGR/ASR 单一提交）**：把「定局 attempt 终态 + execution 终态 + session final 或 poisoned/fallback 清理 + committed 水位推进 + session owner retire + backlog/pending/recovery successor 创建及因果关联 + terminal/superseded/queued 事件写入（遵全局 id 顺序）」并入**一次提交**;任一 CAS/唯一约束失败整体回滚，调用方重读状态后退出或重试，不补偿式续提交;覆盖 pending/backlog/normal success/supersede/poisoned failure 五路径;**按路径字段矩阵实现**——仅 normal/pending success 推进 `committed_msg_id`，retryable failure/kill/supersede/poisoned failure **不推进 committed**;retryable failure 回队时 owner **不 retire**、终局/被杀/交棒/poisoned 才 retire;固定写入顺序「父转出 active 终态→插同 conversation successor(queued)→按全局 id 写事件→提交」，SHALL NOT 先插 successor 再转父终态;ASR 的 session final/committed/owner retire SHALL 复用此同一事务，SHALL NOT 分别提交;探针 `combined_finish_transaction_probe`（每个 DB 写入点依次注入失败，验全提交或全回滚，覆盖五路径）、`finish_path_field_matrix_probe`（六路径逐一验字段写入符合矩阵、失败/被杀/交棒/poisoned 不推进 committed）、`finish_write_order_probe`（先插 successor 再转父终态时被 active 唯一索引拒绝，正确顺序通过）
- [ ] 1.7 探针：`atomic_claim_probe`(N并发只1中)、`dispatch_idempotency_probe`(同key只1execution)、`trigger_coalesce_probe`(100次触发不丢)、`sse_tail_probe`(断连续传不重不漏，含断线期间 superseded 控制事件重连必达、全局 id 有序)、`late_event_after_terminal_probe`(Review 第五轮 P1-8：terminal 后注入旧 attempt 日志，owner/generation/status CAS 失败被丢弃/隔离，用户事件流无终态后旧日志)
- [ ] 1.7a **attempt 模型探针（Review 故障注入 1/2）**：`attempt_lease_probe`(claim 建 task_run 后、running 前强杀 Worker，lease 回收后建新 attempt，不违反 `UNIQUE(run_queue_id,attempt_no)`)、`attempt_retry_probe`(同 execution 连续两次瞬时失败，attempt 1/2/3 均保留、终态由获胜 attempt 决定)
- [ ] 1.7b **event 事务探针（Review 故障注入 3/4/5/6/9）**：`event_seq_concurrency_probe`(API/Worker/日志/终态线程并发写全局 id 不冲突不乱序)、`event_txn_probe`(run_log 写成功、event 写前崩溃→同事务回滚;终态与 terminal event 间崩溃→无「无终态事件」;superseded+child+event 任一点故障→三者原子)、`successor_global_cursor_probe`(Review 第四轮 P0-5：父 execution 断线期间原子 supersede + 建 child，重连收到父 superseded 后带全局 Last-Event-ID 切 child，child queued/log/terminal 全部恰好一次、global id 单调，不因切 execution 重置游标漏事件)
- [ ] 1.7b2 **状态词汇表探针（Review 第四轮 P0-1）**：`status_vocabulary_probe`(执行成功后断言 `run_queue.status='done'` 且获胜 `task_runs.status='succeeded'`;progress、Runtime、`terminal{status}` event、partial index、前端状态色均按各自层的正确值消费，无 execution=`succeeded` 或 attempt=`done` 混用)
- [ ] 1.7c **迁移回滚探针（Review 故障注入 9/10）**：`mixed_version_probe`(新 Worker 写 claimed/superseded 时旧 API 在线不误判;此处「旧 API」SHALL 指已部署的**兼容读取版**、非完全未升级版)、`rollback_probe`(真回滚旧代码，历史 done/新状态不致任务提前完成或永久卡住)、`rollback_floor_probe`(Review 第四轮 P1-3：activate 后只允许回滚到 compatibility protocol floor;尝试启动更旧 Worker/API 时 readiness fail-closed;执行关 flag + 停写排空 + 处理新状态存量后才允许降级)
- [ ] 1.8 回归全量探针,确认流式不退化、读 run_logs 逻辑不回归

## 阶段 2 — Worker 剥离（重启 API 不断 Agent；崩溃可清可拉起）
- [ ] 2.1 `_running` 内存计数 → 查 DB（claimed+running 计数）;单 Worker 用进程内 semaphore 控本地上限
- [ ] 2.2 新增 `worker.py` 入口：`reclaim_orphan_runs` + `_loop` 并发池 + 孤儿巡检,不起 HTTP
- [ ] 2.3 API `startup` 不再 `start_loop()`,只保留建库/seed/静态托管
- [ ] 2.4 **进程 containment（Review P0-5/P0-D + 第六轮 P1-4：containment 与 CAS 前启动闸门是两个独立问题）**：Windows Job Object + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`;**POSIX 真 containment（Review 第五轮 P1-6：`start_new_session=True` ≠ 父死清理）**——拍板 `PR_SET_PDEATHSIG`（处理 fork/exec 竞态）/ 每 attempt 独立 cgroup / systemd scope `KillMode=control-group` 至少一种，且验证孙进程与工具子进程也清理（非只查 CLI 主 pid）;**containment 只解决「Worker 死后进程树清理」，SHALL NOT 被当作「CAS 前挡用户代码执行」的启动闸门**（后者见 2.4a：Windows CREATE_SUSPENDED / POSIX pipe·eventfd gate）;持久化 `pid + pid_create_time + worker_generation + worker_instance_id`（Review P1-2：attempt 行同时存 generation 与 instance id，注入 `jian` 环境供 fencing 比对）;探针 `posix_process_tree_cleanup_probe`（SIGKILL Worker + CLI 再拉孙进程，验证整棵子树退出）
- [ ] 2.4a **🔴 launch generation 最终启动围栏（Review 第五轮 P0-3 + 第六轮 P1-4 双平台启动闸门）**：堵死「claim 成功→心跳暂停→新 Worker 接管 generation→旧 Worker 恢复起 CLI」时间窗。固定不可省略顺序——① 起进程前重校验 DB 当前 generation/instance/worker lease/attempt lease，任一不匹配立即 self-fence;② **CAS 前启动闸门（挂起点），两平台分别落地**——**Windows**：`CREATE_SUSPENDED` 起子进程 → `AssignProcessToJobObject`，**尚未 Resume**;**POSIX**：launcher fork 后阻塞在**匿名 pipe/eventfd gate**、**尚未 `exec` 真实 CLI**（父死 containment 用 `PR_SET_PDEATHSIG`/cgroup/systemd scope 并行完成，见 2.4）;**`start_new_session`/父死清理 SHALL NOT 冒充 suspended launch**（只解决父死后清理，挡不住 exec 后·CAS 前用户代码执行）;③ CAS 持久化 `pid+pid_create_time` 并转 attempt/execution 为 `running`，CAS **同时校验当前 `worker_state` generation/instance/lease**;④ **仅 CAS 成功才放行闸门**（Windows `ResumeThread` / POSIX 释放 pipe·eventfd gate 让 launcher `exec`），失败立即销毁挂起进程树/整个 scope;⑤ **心跳/lease 续租失败分级 self-fence（第六轮 P1-8）**——单次/短时失败只停新 claim/launch（**不杀在跑 CLI**）→ 在本地单调时钟安全截止点（早于 `lease_expires_at`）前重试续租，成功则恢复 → 重试至截止点仍失败（无法证明租约有效）才终止本 generation 全部进程并退出，不靠本地缓存;未实现 gate 的平台（如当前 POSIX）readiness **fail-closed** 不放行，不阻塞 Windows。探针 `launch_epoch_barrier_probe`（挂起创建前后切 generation，验旧 generation 用户代码零执行）、`posix_launch_gate_probe`（POSIX launcher gate 前后切 generation，验 CAS 前真实 CLI 零执行、失败销毁完整 scope、`start_new_session` 不冒充 gate）、`heartbeat_transient_failure_probe`（单次/短抖动心跳失败但 lease 未过期→停 claim 不杀 CLI、续租成功恢复）、`heartbeat_lease_expiry_probe`（持续 DB 不可达至安全截止点→终止本 generation 进程退出、不 finalize）
- [ ] 2.5 **Worker supervisor/heartbeat/readiness**：Windows Service/WinSW/NSSM 或 systemd/Docker restart 拉起;Worker 写 heartbeat/version/generation/draining/active count;`/api/health` readiness 检查 DB 可写+迁移完成+Worker 可用+协议兼容;Runtime 页显示 Worker offline/draining
- [ ] 2.5b **API/Worker 协议回滚兼容 + compatibility floor（Review P1-6 / 第四轮 P1-3）**：明确旧 Worker 遇到新状态/新 payload 版本时的行为——`protocol_version` 不匹配时**fail-closed**（不领取、不接管高版本 run，避免误处理未知状态），而非静默忽略;配合 expand/contract 部署，保证滚动升级/回滚期新旧进程并存不产生错误处理。**回滚边界收窄为 compatibility floor**：activate 后只保证回滚到「已支持读取 `claimed/superseded/recovery_blocked` 的 compatibility release」，SHALL NOT 声称可回滚到 pre-expand 二进制;明确 activate 后允许的最老 protocol_version;若必须回滚到 pre-expand 版本，先关新状态写入 flag→停写排空在跑 execution→处理新状态存量为旧版本可识别终态→再启动旧版本;探针 `rollback_floor_probe`（低于 floor 的旧 Worker/API readiness fail-closed）
- [ ] 2.6 **kill request/ack 带生命周期（Review P1-2）**：kill 请求持久化 `request_id / target_task_run_id / target_generation / state(requested→acked→done) / acked_at / outcome`;API 写 `kill_requested`→Worker 轮询→**校验 target_generation == 当前活跃 generation**（防旧 kill 标记误伤后续恢复 run）→按 pid+create_time 指纹 `taskkill /F /T` 杀树+落终态(`killed`)+**ack**;kill **不触发续跑**（用户主动停）;过期/世代不符的 kill 请求作废不执行
- [ ] 2.7 探针：`worker_split_probe`(两进程不重复领取、停 API 保 Worker 后 run 仍推进)、`worker_containment_probe`(Worker 崩溃后无孤儿 CLI)、`kill_ack_probe`
- [ ] 2.8 更新启动脚本/文档：分别拉起 API 与 Worker + supervisor
- [ ] 2.9 回归全量探针在「API+Worker 双进程」下通过

## 阶段 3/4 — resume（归 [agent-session-resume]，本 change 只依赖其产出）
- [ ] 3.x Claude resume（ASR S1-S3）：`agent_sessions` 表 + 流中 pin + committed/planned 水位 + `--resume` + mismatch/失败降级 + poisoned 分类
- [ ] 4.x Codex app-server（ASR S4）：每 run 一个 `codex app-server --listen stdio://` + `thread/resume`→`thread/start` + threadId pin + rollout/workdir 检查 + transport fail-fast
- [ ] 依赖对接：阶段 5 前确认 ASR 已产出可用 `session_id`（含流中途 pin，供中断续跑）

## 阶段 5 — 交棒 + 有界恢复（改执行层：能等则零中断，超时 resume 续跑）
- [ ] 5.1 **温和重启 defer 窗口**：重启意图→停领新活→轮询 `activeTasks`,全部收尾即零中断;等待上限 **5 分钟**（参数化）内未清零才转交棒
- [ ] 5.2 **generation + claim barrier（Review P0-D/P1-1/P1-6）**：`worker_state` 单行表(current_generation/**owner_instance_id**/state/heartbeat_at/**lease_expires_at**/**protocol_version**)+`task_runs.worker_generation`+`task_runs.worker_instance_id`;两类接管——① 优雅交棒 3 态 `running→draining→done`（有 ack）② **硬崩溃 lease 过期 CAS 抢占**（`WHERE current_generation=g AND lease_expires_at<now SET generation=g+1,owner_instance_id=new`，并发唯一接管，不干等 done）;protocol_version 与 DB schema 不匹配 fail-closed 不接管
- [ ] 5.2a **claim 真原子互斥（Review P1-1）**：claim SQL 的 WHERE 同时校验 `worker_state.state='running'`（非 draining/done）+ `claim_generation==当前活跃 generation` + owner_instance 匹配 + lease 未过期，**不只靠本地进程标志**;draining 后该 CAS 天然不命中，关闭「停领与新任务刚进」竞态
- [ ] 5.2b **lease 时间语义 + 续租失败分级（Review P1-6 + 第六轮 P1-8）**：以**数据库时间**为准（防 Worker 与 DB 时钟漂移）;定义 `heartbeat_interval`、`lease_duration`(=N×heartbeat_interval)、允许的短暂暂停窗口（DB 卡顿/GC），使短停顿不误触发接管;心跳周期推进 lease_expires_at。**续租失败三级处置**（防 DB 短抖动大面积误杀）——① 单次/短时失败：停新 claim/launch，**不杀在跑 CLI**;② 在本地**单调时钟**安全截止点（SHALL 早于 `lease_expires_at`，留时钟/调度余量）前重试续租，成功即恢复;③ 重试至截止点仍失败才终止本 generation 全部进程退出;判定用单调时钟、不用可回拨墙钟
- [ ] 5.3 **交棒硬路径**：旧 Worker 对每个在跑 run `taskkill /F /T` 杀树 + 父 execution **CAS supersede**（`WHERE status='running' AND worker_generation=?`，**不触发自动流转**）+ **恰好入队一个子 execution recovery child**（`superseded_from`+系统恢复标记+`recovery_mode`：有 session=`session_resume`/无 session=`full_replay`，Review 第六轮 P0-3 统一状态机，**不再「无则落 failed 从头」**）**同一事务**;置 generation done → 新 Worker 确认 done 后 `g+1` 接管（ack）
- [ ] 5.4 新 Worker 领续跑 run → **重发原始任务 prompt** + resume 路径起 CLI;resume 未落地时 prompt 前置「新会话」披露
- [ ] 5.5 **续跑豁免防死循环配额**：续跑标系统恢复类 → 豁免 `MAX_MENTION_CHAIN`、不占 `MAX_RUNS_PER_TASK`;与 Agent 自发 @ 严格区分
- [ ] 5.6 **副作用 at-least-once**：靠 session 记忆 + prompt 约束（「先检查再动手」「只做一次，即便非零退出也不重试」）;平台自有写操作可加 `recovery_chain_id + logical_op_key` 幂等键;外部副作用保留执行链+告警+人工确认入口;**明确写规格:恢复语义 = at-least-once，非 exactly-once**
- [ ] 5.7 **bounded recovery**：独立维护 `recovery_attempt` + 指数退避 + 每 chain 最大恢复次数 + dead-letter/人工介入;普通 mention-chain 配额与系统恢复配额**分开统计**
- [ ] 5.8 `reclaim_orphan_runs`（Review P0-D，**撤销「running 已死」无条件假设**）：**先接管**（CAS 升 generation → fencing 旧世代，杜绝孤儿 CLI 双写）→ **再判死**（`pid + pid_create_time` 探活，不存在或 create-time 不匹配才判停）→ **才续跑**（确认停或已 fencing+清理时，据 session 追加入队续跑 `superseded_from`）;poisoned 已丢 session 的不续;**既不能证明已停又不能保证 fencing/清理 → 置 `recovery_blocked` 进 dead-letter 待人工，不建 child**（宁可不续不双执行）;fencing 校验同时比对 worker_generation + owner_instance_id 防复用误判
- [ ] 5.9 防双续幂等：`superseded_from` 唯一约束——一个父 run 至多一个 recovery child;交棒杀+reclaim 兜底对同一 run 至多入队一条
- [ ] 5.10 **generation fencing**：`jian` 写接口带 run 的 generation，DB 校验 == 当前活跃 generation，拒 superseded/过期 generation 旧进程写平台
- [ ] 5.11 更新 Worker 重启脚本：发重启意图 → defer 等 →（超时才）交棒 → 等旧 Worker done → 起新 Worker
- [ ] 5.12 探针：`worker_handover_probe`(defer零中断/超时交棒/supersede+child同事务/重发原prompt续/防双续/kill不续/续跑不吃配额)、`recovery_budget_probe`(crash-loop 达上限进 dead-letter/退避生效)、`fencing_probe`(旧 generation 被拒写)、`hard_crash_takeover_probe`(lease 过期 CAS 抢占唯一接管/先 fencing 再判死/不能证明已停则 recovery_blocked 不双执行)、**（第六轮 P0-3）**`handover_without_session_probe`(强杀未 pin session 的 CLI → 父恰好 superseded、恰好一个 full_replay child queued)、`recovery_mode_probe`(有/无 session 分走 resume/full_replay 但父子因果链·预算·事件协议一致)、`no_terminal_requeue_probe`(断言 done/failed/killed/superseded/recovery_blocked 任一都不能再转 queued)
- [ ] 5.13 回归全量探针在「交棒重启 Worker」场景下通过

## 阶段 6 — 连接平滑（Nginx 蓝绿）
- [ ] 6.1 新增 `deploy/nginx.conf`：`upstream` 指向 API 实例,反代 `:8100`,SSE 需 `proxy_buffering off` + 合理超时
- [ ] 6.2 **SSE 主动轮换**：每 15~30s 主动断,客户端按 `Last-Event-ID` 自动重连——给旧 API 确定排空上限（不靠「同一连接不断」）
- [ ] 6.3 蓝绿脚本：起新 API 实例 → readiness(DB可写+迁移完成+Worker可用) 通过 → 切 upstream → `nginx -s reload` → 旧 API drain timeout 后退
- [ ] 6.4 **最终 contract 清旧（Review 第六轮 P0-2：activate 已在阶段 1C，本阶段不再首次开启新状态写入）**：核心状态机的 expand/compatibility/activate 已在阶段 1A/1B/1C 完成，本阶段只做——① 观察稳定后 contract 清理旧列/旧兼容读逻辑;② `run_queue` 成功态保持 `done` 不改名（不改写历史）;③ 蓝绿路由切换 + queue `payload_version` readiness 检查、旧 Worker 遇高版本 payload fail-closed。SHALL NOT 把「首次 activate 核心状态机」放在本阶段
- [ ] 6.5 **（已前移到阶段 1 的 1.4a）** 状态影响面完整矩阵不再留在阶段 6——见 1.4a。阶段 6 仅保留蓝绿部署相关，状态矩阵随阶段 1 引入新状态时同步落地
- [ ] 6.6 手动验证 + `blue_green_probe`：压测/SSE 进行中蓝绿切换,自动重连、状态与日志不重不漏、Agent 不受影响
- [ ] 6.7 文档：更新 README 部署段(Nginx + API + Worker + supervisor 拉起顺序);记忆 `backend-restart-single-instance` 手动流程由 Nginx reload 取代

## 收尾（每阶段完成后）
- [ ] 固化：本 change 完成并验证后,把能力规格从 change delta 固化进 `specs/platform-graceful-restart/spec.md`,change 移入 `changes/archive/`
- [ ] 联动更新 [platform-concurrency-scaling]：其 2.1（状态外置）标注「由本 change 阶段 1/2 提供」、2.2（多 worker 无状态消费）标注「本 change 出可多进程消费地基（含原子 claim CAS），concurrency 在其上做并发规模与公平」

