# Tasks — platform-graceful-restart

> 规划态,分阶段落实,**每阶段验证门通过才进下一步**;每阶段含「代码 + 新探针 + 全量回归」,一步一提交、可独立回退。经技术负责人 Review 修订（2026-07-16）：先补执行协议状态机，Worker 剥离优先、resume 后置。

## 跨-change 执行顺序（Review 修订版：阶段 0-6，Worker 先、resume 后）

| 阶段 | 归属 | 内容 | 依赖 | 验证门 |
|---|---|------|------|------|
| **0** | 本 change | 补 durable execution 状态机 + 7 不变量 + 修正 3 处事实错误 | — | `--strict` + Review 认可 |
| **1** | 本 change | DB 协议地基：WAL/备份/索引、统一入队、两段式 dispatch、原子 claim(CAS)、触发合并、task_run↔queue 关联 | 0 | `atomic_claim_probe`/`sse_tail_probe`/`dispatch_idempotency_probe`/`trigger_coalesce_probe` |
| **2** | 本 change | Worker 剥离 + supervisor/heartbeat + generation + 进程 containment + kill request/ack | 1 | `worker_split_probe`/`worker_containment_probe`/`kill_ack_probe` |
| **3** | [agent-session-resume] | Claude resume（建表/pin/水位/降级/poisoned） | 1 | ASR S1-S3 探针 |
| **4** | [agent-session-resume] | Codex app-server（每 run 一进程/thread resume） | 1 | ASR S4 探针 |
| **5** | 本 change | 交棒 + 有界恢复：generation/claim barrier/defer 5min/CAS supersede+child 同事务/bounded recovery/fencing | 3·4·2 | `worker_handover_probe`/`recovery_budget_probe`/`fencing_probe` |
| **6** | 本 change | Nginx 蓝绿 + SSE 主动轮换 + expand/contract | 2 | 手动压测 + `blue_green_probe` |

**执行纪律**：① 验证门不过不推进;② 每阶段可独立回退;③ 需重启验证先问用户、按单实例流程;④ DB 建表/加列前用**安全在线备份**（WAL 下不可直接 cp，见 1.1b）。**本 change 覆盖阶段 0/1/2/5/6;阶段 3/4 归 [agent-session-resume]。**

## 阶段 0 — 补执行协议规格（纯文档，已完成）
- [x] 0.1 design 决策 0：状态机 `queued→claimed→running→{succeeded/failed/killed/superseded}`（**删 accepted，Review P0-A**）+ 7 不变量 + claimed lease 恢复 + 状态消费者矩阵前移（已写入）
- [x] 0.2 spec 新增 Requirement：状态机与不变量、原子 claim、两段式 dispatch、进程 containment、generation/ack/fencing、SSE 统一事件序列、历史数据迁移（已写入）
- [x] 0.3 修正 3 处事实错误：claim 非原子、`run_queue` 无 conversation_id 列（折叠改用 `(task_id,agent_slug)` partial index）、resume mismatch 判定（本 change + ASR 已改）
- [x] 0.4 第二轮 Review 6 个 P0（A-F）+ 关键 P1 全部落文档：删 accepted、折叠 DB 唯一性、连续前缀分批、硬崩溃 generation 接管、SSE 统一游标、历史迁移闭环
- [ ] 0.5 `--strict` 校验通过 + Review 复核认可（放行阶段 1）

## 阶段 1 — DB 协议地基（消灭双领与触发丢失；流式可重连）
- [ ] 1.1 SQLite 连接开 `PRAGMA journal_mode=WAL`（初始化一次）+ `PRAGMA busy_timeout`（**每条新连接**都设）
- [ ] 1.1b 安全在线备份：改用 SQLite online backup API 或 `VACUUM INTO`（**不可直接 cp 主库**——WAL 下会漏未 checkpoint 的 `-wal` 数据）；建表/加列前先备份
- [ ] 1.1c 加索引：`run_logs(run_id,id)`、`run_queue(status,next_retry_at,id)`、active `(task_id,agent_slug,status)`
- [ ] 1.2 **原子 claim(CAS)**：`_claim_one` 改单语句条件更新（`UPDATE...WHERE id=(子查询) AND status='queued' RETURNING *`），杜绝双领
- [ ] 1.3 **统一入队 + 两段式 dispatch**：所有触发（人工@/auto/mention/leader）经队列;`POST /tasks/{id}/dispatch` 接 idempotency key、幂等持久化用户消息、入队、返回 `execution_id`,**不在请求内跑 CLI**;`GET /executions/{execution_id}/events` 独立 SSE 订阅
- [ ] 1.4 **触发合并（不丢，非丢弃）**：同 (task,agent) 已有 queued→合并消息水位到该行;running→持久化 `rerun_requested`+`pending_through_message_id`+触发来源;收尾事务内据 pending 至多建一个 successor
- [ ] 1.5 **task_run↔queue 不可变关联**：claim 同事务建 `task_runs` 行 + 写 `task_runs.run_queue_id`;SQLite 不能给已有表直接加 `NOT NULL UNIQUE`，分步——`ALTER ADD COLUMN run_queue_id INTEGER`(可空) → 反向回填存量 → `CREATE UNIQUE INDEX ... WHERE run_queue_id IS NOT NULL`(partial 容忍存量空) → 新写入路径强制非空
- [ ] 1.5b **历史数据迁移闭环（Review P0-F）**：① `run_queue.status` 值域扩展 `+claimed/succeeded/killed/superseded/recovery_blocked`，一次性 `UPDATE ... SET status='succeeded' WHERE status='done'`，**读取层保留 `done→succeeded` 兼容映射**（旧代码回滚期不炸）;② `agent_sessions` 折叠 partial unique index 建立前先归并/取消存量同 (task,agent) 多 active 行（否则建索引失败）;③ 新列（worker_generation/pid_create_time/owner_instance_id/planned_through_msg_id/committed_msg_id/meta_json 等）带安全默认（NULL/0），存量行走首次执行/全量分支不报错;④ 回滚兼容——旧代码读到未知状态/新列按保守兜底，不因 schema 前进崩;⑤ 迁移前先 `VACUUM INTO` 安全备份（承接 1.1b），失败可回滚
- [ ] 1.6 **SSE 统一事件序列 + 续传契约（Review P0-E，游标用 seq 非 run_logs.id）**：建 `execution_events(execution_id, seq, event_type, payload_json, created_at)` `UNIQUE(execution_id,seq)`;`run_logs` 加 `meta_json` 列（channel/tool/tool_input/tool_output）;所有事件（log + 控制事件 `queued/run_started/superseded/terminal`）先落 execution_events 再推，`id:<seq>`+`Last-Event-ID` 为唯一游标（heartbeat 不占 seq）;断连 `WHERE execution_id=? AND seq>? ORDER BY seq` 回放，控制事件与 log 统一有序不重不漏（含断线期间的 superseded/terminal）、run 未起发 queued 占位、终态回放全量
- [ ] 1.7 探针：`atomic_claim_probe`(N并发只1中)、`dispatch_idempotency_probe`(同key只1execution)、`trigger_coalesce_probe`(100次触发不丢)、`sse_tail_probe`(断连续传不重不漏，**含断线期间 superseded 控制事件重连必达、seq 有序**)
- [ ] 1.8 回归全量探针,确认流式不退化、读 run_logs 逻辑不回归

## 阶段 2 — Worker 剥离（重启 API 不断 Agent；崩溃可清可拉起）
- [ ] 2.1 `_running` 内存计数 → 查 DB（claimed+running 计数）;单 Worker 用进程内 semaphore 控本地上限
- [ ] 2.2 新增 `worker.py` 入口：`reclaim_orphan_runs` + `_loop` 并发池 + 孤儿巡检,不起 HTTP
- [ ] 2.3 API `startup` 不再 `start_loop()`,只保留建库/seed/静态托管
- [ ] 2.4 **进程 containment（Review P0-5/P0-D）**：Windows Job Object + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，按 `CREATE_SUSPENDED → AssignProcessToJobObject → ResumeThread` 顺序创建防「已跑起未进 Job」逃逸窗口;POSIX `start_new_session` 进程组;持久化 `pid + pid_create_time + worker_generation`（可比对进程身份）
- [ ] 2.5 **Worker supervisor/heartbeat/readiness**：Windows Service/WinSW/NSSM 或 systemd/Docker restart 拉起;Worker 写 heartbeat/version/generation/draining/active count;`/api/health` readiness 检查 DB 可写+迁移完成+Worker 可用+协议兼容;Runtime 页显示 Worker offline/draining
- [ ] 2.5b **API/Worker 协议回滚兼容（Review P1-6）**：明确旧 Worker 遇到新状态/新 payload 版本时的行为——`protocol_version` 不匹配时**fail-closed**（不领取、不接管高版本 run，避免误处理未知状态），而非静默忽略;配合 expand/contract 部署，保证滚动升级/回滚期新旧进程并存不产生错误处理
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
- [ ] 5.2 **generation + claim barrier（Review P0-D）**：`worker_state` 单行表(current_generation/**owner_instance_id**/state/heartbeat_at/**lease_expires_at**/**protocol_version**)+`task_runs.worker_generation`;心跳周期推进 lease_expires_at;两类接管——① 优雅交棒 3 态 `running→draining→done`（有 ack）② **硬崩溃 lease 过期 CAS 抢占**（`WHERE current_generation=g AND lease_expires_at<now SET generation=g+1,owner_instance_id=new`，并发唯一接管，不干等 done）;protocol_version 与 DB schema 不匹配 fail-closed 不接管;draining 后不再 claim（与 claim 同一受保护决策点）
- [ ] 5.3 **交棒硬路径**：旧 Worker 对每个在跑 run `taskkill /F /T` 杀树 + 旧 run **CAS supersede**（`WHERE status='running' AND worker_generation=?`，**不触发自动流转**）+ 依 session 入队续跑 run（`superseded_from`+resume 意图+系统恢复标记）**同一事务**;置 generation done → 新 Worker 确认 done 后 `g+1` 接管（ack）
- [ ] 5.4 新 Worker 领续跑 run → **重发原始任务 prompt** + resume 路径起 CLI;resume 未落地时 prompt 前置「新会话」披露
- [ ] 5.5 **续跑豁免防死循环配额**：续跑标系统恢复类 → 豁免 `MAX_MENTION_CHAIN`、不占 `MAX_RUNS_PER_TASK`;与 Agent 自发 @ 严格区分
- [ ] 5.6 **副作用 at-least-once**：靠 session 记忆 + prompt 约束（「先检查再动手」「只做一次，即便非零退出也不重试」）;平台自有写操作可加 `recovery_chain_id + logical_op_key` 幂等键;外部副作用保留执行链+告警+人工确认入口;**明确写规格:恢复语义 = at-least-once，非 exactly-once**
- [ ] 5.7 **bounded recovery**：独立维护 `recovery_attempt` + 指数退避 + 每 chain 最大恢复次数 + dead-letter/人工介入;普通 mention-chain 配额与系统恢复配额**分开统计**
- [ ] 5.8 `reclaim_orphan_runs`（Review P0-D，**撤销「running 已死」无条件假设**）：**先接管**（CAS 升 generation → fencing 旧世代，杜绝孤儿 CLI 双写）→ **再判死**（`pid + pid_create_time` 探活，不存在或 create-time 不匹配才判停）→ **才续跑**（确认停或已 fencing+清理时，据 session 追加入队续跑 `superseded_from`）;poisoned 已丢 session 的不续;**既不能证明已停又不能保证 fencing/清理 → 置 `recovery_blocked` 进 dead-letter 待人工，不建 child**（宁可不续不双执行）;fencing 校验同时比对 worker_generation + owner_instance_id 防复用误判
- [ ] 5.9 防双续幂等：`superseded_from` 唯一约束——一个父 run 至多一个 recovery child;交棒杀+reclaim 兜底对同一 run 至多入队一条
- [ ] 5.10 **generation fencing**：`jian` 写接口带 run 的 generation，DB 校验 == 当前活跃 generation，拒 superseded/过期 generation 旧进程写平台
- [ ] 5.11 更新 Worker 重启脚本：发重启意图 → defer 等 →（超时才）交棒 → 等旧 Worker done → 起新 Worker
- [ ] 5.12 探针：`worker_handover_probe`(defer零中断/超时交棒/supersede+child同事务/重发原prompt续/防双续/无session兜底/kill不续/续跑不吃配额)、`recovery_budget_probe`(crash-loop 达上限进 dead-letter/退避生效)、`fencing_probe`(旧 generation 被拒写)、`hard_crash_takeover_probe`(lease 过期 CAS 抢占唯一接管/先 fencing 再判死/不能证明已停则 recovery_blocked 不双执行)
- [ ] 5.13 回归全量探针在「交棒重启 Worker」场景下通过

## 阶段 6 — 连接平滑（Nginx 蓝绿）
- [ ] 6.1 新增 `deploy/nginx.conf`：`upstream` 指向 API 实例,反代 `:8100`,SSE 需 `proxy_buffering off` + 合理超时
- [ ] 6.2 **SSE 主动轮换**：每 15~30s 主动断,客户端按 `Last-Event-ID` 自动重连——给旧 API 确定排空上限（不靠「同一连接不断」）
- [ ] 6.3 蓝绿脚本：起新 API 实例 → readiness(DB可写+迁移完成+Worker可用) 通过 → 切 upstream → `nginx -s reload` → 旧 API drain timeout 后退
- [ ] 6.4 **expand/contract 部署**：① expand-only migration ② Worker 先兼容新旧 payload/schema ③ 切 API 写新格式 ④ 稳定后 contract 清旧;queue payload 加 `payload_version`,readiness 检查 API/Worker 协议兼容
- [ ] 6.5 **`superseded` 状态影响面完整矩阵**：`run_queue`/`task_runs` schema+status 注释、`_process_one` 终态 UPDATE、`runner._finish_run`、progress/auto-flow、Runtime 总览+失败率+运行时间统计、`RunRow.vue`/`RunTranscriptDialog.vue`/`Runtime.vue` 状态颜色文案、lineage/重跑/孤儿巡检/失败归因——**出完整状态矩阵+允许转换表**，杜绝未知状态落入成功图标
- [ ] 6.6 手动验证 + `blue_green_probe`：压测/SSE 进行中蓝绿切换,自动重连、状态与日志不重不漏、Agent 不受影响
- [ ] 6.7 文档：更新 README 部署段(Nginx + API + Worker + supervisor 拉起顺序);记忆 `backend-restart-single-instance` 手动流程由 Nginx reload 取代

## 收尾（每阶段完成后）
- [ ] 固化：本 change 完成并验证后,把能力规格从 change delta 固化进 `specs/platform-graceful-restart/spec.md`,change 移入 `changes/archive/`
- [ ] 联动更新 [platform-concurrency-scaling]：其 2.1（状态外置）标注「由本 change 阶段 1/2 提供」、2.2（多 worker 无状态消费）标注「本 change 出可多进程消费地基（含原子 claim CAS），concurrency 在其上做并发规模与公平」

