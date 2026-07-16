# platform-graceful-restart (delta)

## ADDED Requirements

### Requirement: durable execution 状态机与并发不变量

系统 SHALL 以持久化执行状态机承载每次执行：一条 `run_queue` 行 = 一个 execution，状态 `queued → claimed → running → {succeeded | failed | killed | superseded | recovery_blocked}`（**无独立 accepted 态**：POST 同事务写用户消息 + 建 queued execution、提交后才返回 execution_id;`recovery_blocked` = 无法安全恢复而进 dead-letter 待人工的终态，见异常重启 Requirement）。claimed 时 SHALL 同事务创建其唯一关联的 `task_runs` 行并写 `lease_until`;`claimed` 超 `lease_until` 未转 running SHALL 可被 CAS 回收为 `queued`（prepare-lease 过期）;`claimed→running` SHALL 用 CAS 校验 claim_owner/claim_generation。引入 `claimed`/`superseded` 后，所有状态消费者（progress 聚合、Runtime 总览、任务自动流转、孤儿巡检、失败归因、前端状态色）SHALL 同步识别新状态，SHALL NOT 把 `claimed` 误判空闲或把 `superseded` 落入成功显示;完整状态矩阵 + 允许转换表 SHALL 在阶段 1 引入状态时同步落地。系统 SHALL 满足以下不变量：① 一个 queue item 同一时刻只被一个 Worker generation 持有;② 同一 `(task, agent)` 最多一个 active（queued/claimed/running）且最多一个持久化 pending intent;③ `running→succeeded` 与 `running→superseded` 通过 CAS 竞争只能一个成功;④ `superseded + recovery child 入队` 同一事务提交;⑤ 旧 generation 不能 finalize、不能写平台（fencing）;⑥ recovery chain 有次数上限 + 退避 + dead-letter;⑦ task_run/run_queue/session 水位/消息投递之间有明确事务边界。所有终态转换 SHALL 用 `WHERE status='running' AND worker_generation=?` 的 CAS 落定。

#### Scenario: 自然完成与交棒互斥
- **WHEN** 一个 running run 同时被「自然完成」与「交棒 supersede」触发
- **THEN** 两者通过 CAS 竞争，只有一个成功落终态；结果只能是「succeeded 且无 recovery child」或「superseded 且恰好一个 recovery child」，不出现互相覆盖或半提交

#### Scenario: supersede 与 recovery child 同事务
- **WHEN** 一个 running run 被交棒中断需要续跑
- **THEN** 「旧 run 落 superseded」与「recovery child 入队」在同一事务提交，不出现「已 superseded 但无 child」的半提交状态

#### Scenario: 旧 generation 被 fencing
- **WHEN** 一个属于旧 Worker generation 的进程（含崩溃后残留的孤儿 CLI）尝试 finalize run 或调用 `jian` 写平台
- **THEN** 系统按 generation 校验拒绝该写入（当前活跃 generation 不匹配），防止旧执行与恢复执行双写

### Requirement: 原子 claim（CAS 单语句领取）

系统 SHALL 用单语句条件更新原子领取 queued run：`UPDATE run_queue SET status='claimed', claim_owner=?, claim_generation=?, claimed_at=... WHERE id=(子查询选一条 queued) AND status='queued' RETURNING *`。`AND status='queued'` 的 CAS 条件 SHALL 保证多个 Worker 竞争同一行时至多一个成功。SHALL NOT 使用「先 SELECT 再无条件 UPDATE」的两步领取。并发上限判断在多 Worker 下 SHALL 使用原子容量机制而非「先 COUNT 再 claim」（单 Worker 阶段可用进程内 semaphore）。

#### Scenario: 并发竞争同一行只有一个成功
- **WHEN** 多个 Worker 同时尝试领取同一个 queued run
- **THEN** CAS 条件使至多一个 UPDATE 命中，该 run 只被一个 Worker 领取，其余落空去领下一条，不重复不遗漏

#### Scenario: claim 即建 task_run 关联
- **WHEN** 一个 run 被原子领取（进入 claimed）
- **THEN** 同一事务内创建其 `task_runs` 行并写 `task_runs.run_queue_id`（UNIQUE NOT NULL），建立不可变关联，不再等执行结束才回填

### Requirement: 两段式 dispatch（提交与订阅分离）

系统 SHALL 把所有触发（人工 @、auto-dispatch、mention、leader 协同）统一经持久化队列，采用两段式协议：① `POST /tasks/{id}/dispatch` 接收客户端 idempotency key，**同一事务**内幂等持久化用户消息 + 创建 `queued` execution、提交后返回稳定 `execution_id`，SHALL NOT 在请求内直接执行 CLI;② `GET /executions/{execution_id}/events` 独立 SSE 订阅，按 execution_id 尾随，API 重启后可重新订阅。idempotency 作用域 SHALL 为 `UNIQUE(task_id, actor_id, idempotency_key)`;相同 key 重试 SHALL 只产生一条用户消息与一个 execution;同 key 不同 payload SHALL 返回 409。

#### Scenario: 提交不在请求内执行
- **WHEN** 用户人工 @ 一位成员触发执行
- **THEN** API 同事务写消息+queued execution、提交后返回 execution_id，不在 POST 请求内同步跑 CLI；执行由 Worker 领取，API 重启不影响该执行

#### Scenario: POST 幂等
- **WHEN** 相同 idempotency key 的 dispatch 请求被重试（网络抖动/前端重发）
- **THEN** 系统只产生一条用户消息与一个 execution，不重复入队

### Requirement: 子进程 containment（Worker 死则 CLI 死）

系统 SHALL 保证 CLI 子进程不因 Worker 崩溃而成为继续运行的孤儿：Windows SHALL 用 Job Object + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，且 SHALL 按 `CREATE_SUSPENDED` → `AssignProcessToJobObject` → `ResumeThread` 顺序创建以杜绝「已跑起但未进 Job」的逃逸窗口；POSIX SHALL 用 `start_new_session` 独立进程组 + 父死清理。系统 SHALL 持久化 `pid + pid_create_time + worker_generation`（不只存 pid、不只在内存），使重启后可比对进程身份。恢复中断的 run 前系统 SHALL 先确认旧进程树已清理；无法证明旧执行已停止时 SHALL NOT 创建 recovery child（宁可不续，不可双执行），并以 generation fencing 作为兜底。

#### Scenario: Worker 崩溃后无孤儿 CLI
- **WHEN** Worker 进程崩溃退出
- **THEN** 其启动的 CLI 子进程树被 OS（Job Object / 进程组机制）连带清理，不留下继续运行的孤儿进程

#### Scenario: 无法证明旧进程已停则不续跑
- **WHEN** 恢复某中断 run 前，无法确认其旧 CLI 进程树已被清理
- **THEN** 系统不创建 recovery child，避免旧 CLI 与恢复 CLI 双执行；generation fencing 兜底拒绝旧进程写平台

### Requirement: Worker generation 与交棒 ack

系统 SHALL 用单调递增的 Worker generation 标识执行世代，持久化于 `worker_state` 单行表（current_generation/owner_instance_id/state/heartbeat_at/lease_expires_at/protocol_version）与每个 `task_runs.worker_generation`。系统 SHALL 支持两类接管，共用同一 generation 机制：① **优雅交棒（有 ack）** 走 3 态 `running(g) → draining(g)`（旧 Worker 停领、杀在跑 CLI、supersede + 入队 recovery child、置 done）`→` 新 Worker 确认旧 generation 为 done 后 `g+1` 接管;② **硬崩溃接管（无 ack）** 旧 Worker 未置 done 即死，新 Worker SHALL 以 `lease_expires_at < now` 为据、用 CAS `WHERE current_generation=g AND lease_expires_at<now SET current_generation=g+1, owner_instance_id=<new>` 抢占接管（并发拉起时 CAS 保证唯一接管者），SHALL NOT 无限干等 done。心跳 SHALL 周期推进 `lease_expires_at`;`protocol_version` 与 DB schema 不匹配时 SHALL fail-closed 不接管。fencing 校验 SHALL 同时比对 `worker_generation` 与 `owner_instance_id`，防 pid/generation 复用误判。claim 与 draining 检查 SHALL 在同一受保护决策点完成（draining 后不再 claim），关闭「停领与新任务刚进」的竞态。

#### Scenario: 新 Worker 确认旧世代 done 才接管（优雅交棒）
- **WHEN** 旧 Worker 进入 draining 并完成 kill/supersede/入队 recovery child、置 generation 为 done
- **THEN** 新 Worker 读到 done 后才升 generation 接管领取，不与旧 Worker 并发持有同一 run

#### Scenario: 硬崩溃 lease 过期抢占接管
- **WHEN** 旧 Worker 未置 done 即崩溃（心跳停摆，`lease_expires_at` 已过期），一个或多个新 Worker 被拉起
- **THEN** 新 Worker 以 lease 过期为据 CAS 抢占接管，只有一个成功升 generation，其余失败退让；接管后按「先 fencing 再判死再续跑」处理残留 running run

#### Scenario: draining 后不再领新活
- **WHEN** Worker 已进入 draining 状态
- **THEN** 该 Worker 不再 claim 新 run，避免「正在停机却又领了新任务」的竞态

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

### Requirement: 流式输出尾随 run_logs 且可续传

API 的 SSE 端点 SHALL 把输出近实时推送给前端而非直连 CLI stdout。**续传游标 SHALL 用统一事件序列 `execution_events(execution_id, seq, event_type, payload_json)` 的 `seq`，SHALL NOT 用 `run_logs.id`**——控制事件（queued/run_started/superseded/terminal）不写在 run_logs，用 log id 做游标会导致断线期间的控制事件（如 superseded 跳转）重连后漏投或错序。所有 SSE 事件（log 与全部控制事件）SHALL 先落 `execution_events` 再推，`id: <seq>` 为唯一游标;`run_logs` SHALL 加 `meta_json` 列承载 log 事件的结构化附加信息（channel/tool/tool_input/tool_output），log 事件可从 run_logs + meta 完整重建。CLI 输出写 `run_logs` 的现有路径(Worker 线程 → `_log()`)SHALL 保持不变。SSE 断连重连 SHALL 携带 `Last-Event-ID: <seq>` 从该 seq 之后回放，控制事件与 log 事件**统一有序、不重不漏**，使 API 重启后前端可无损续。

#### Scenario: 尾随近实时呈现
- **WHEN** CLI 持续产出输出、Worker 写入 run_logs 并落 log 类 execution_events
- **THEN** 前端经 SSE 近实时(亚秒级延迟)逐段看到新输出,体验与直连 stdout 基本一致

#### Scenario: SSE 断点无损续传（含控制事件）
- **WHEN** API 进程重启或网络抖动导致 SSE 断开,前端携带 `Last-Event-ID: <seq>` 重连
- **THEN** 服务端 `WHERE execution_id=? AND seq > ? ORDER BY seq` 回放,log 与控制事件统一有序、不重复已收、不遗漏（含断线期间发生的 superseded/terminal）

#### Scenario: 中途进入回放已结束 run
- **WHEN** 用户在某 run 已终态后进入其详情
- **THEN** 一次性回放该 run 的全量输出与收尾态,不需要实时尾随

### Requirement: Worker 温和重启 + resume 续跑（改执行层代码）

更新执行层代码需重启 Worker 时,系统 SHALL 优先「温和重启」：收到重启意图后停止领新活、等待在跑 run 自然收尾一个上限窗口（默认 5 分钟，参数化可调）,窗口内全部收尾则零中断重启。仅当超过等待上限仍有在跑 run,才 SHALL 转入「中断 + resume 续跑」硬路径：旧 Worker 收交棒标记后停领新活、杀在跑 CLI 子进程树、把这些 run 落 `superseded` 终态（`superseded` SHALL NOT 触发子任务 done / 父任务 reviewing 等自动流转）,并对每个有可用 `session_id` 的 run 入队一条「续跑 run」(携带 `superseded_from`、resume 意图、系统恢复标记),然后退出;新 Worker 领取续跑 run 后 SHALL **重发原始任务 prompt** 并依 [agent-session-resume] 的 resume 从上次上下文续跑。在跑 Agent SHALL 允许秒级中断,但上下文 SHALL NOT 丢失、SHALL NOT 需要人工从头重跑。无可用 session 的 run（首次执行尚无 session、或 poisoned 已丢 session）SHALL 落 failed 重排队。续跑 run SHALL 标记为系统恢复类,豁免 mention-chain 空转链计数与单任务运行数配额,不与 Agent 自发触发混淆。续跑 SHALL 靠 session 记忆 + prompt 约束防副作用重复,SHALL NOT 依赖服务端精确幂等键。

本能力**依赖** [agent-session-resume] 提供 per-agent `session_id`（claude+codex 均已接入）、流中途 pin 落库、续跑重发原 prompt 语义;本 change 只负责「重启时的 defer 等待、中断、落终态与续跑入队」,不实现 resume 本身。

#### Scenario: 温和重启等空闲窗口零中断
- **WHEN** 触发执行层重启,当前有在跑 run,且这些 run 在等待上限窗口内自然收尾
- **THEN** 系统等其全部收尾后再重启 Worker,在跑 Agent 零中断、无需 resume

#### Scenario: defer 超时转交棒 resume 续跑
- **WHEN** 等待上限窗口内仍有在跑 run,某 run 有可用 session_id
- **THEN** 旧 Worker 杀该 CLI、旧 run 落 `superseded`（不触发自动流转）、入队带 resume 意图+系统恢复标记的续跑 run;新 Worker 领取后重发原 prompt 以 resume 续跑,Agent 从上次上下文继续,不从头重跑

#### Scenario: 续跑不吃防死循环配额
- **WHEN** 系统因重启多次为某任务的 run 入队续跑
- **THEN** 这些续跑标记为系统恢复类,不计入 mention-chain 空转链、不占单任务运行数配额,不触发误熔断

#### Scenario: 续跑防副作用重复
- **WHEN** 被中断的 run 已产生副作用（如已建卡/已评论/已改状态）,续跑 resume 后可能重复
- **THEN** 系统靠 CLI session 记忆 + prompt 约束（聚焦本轮、只做一次、先检查再动手）抑制重复,不依赖服务端精确幂等

#### Scenario: 无 session 的 run 走从头重跑
- **WHEN** 交棒时某在跑 run 无可用 session_id（首次执行尚未产生 session,或 poisoned 已丢 session）
- **THEN** 该 run 落终态并重排队从头重跑,不因缺 session 而卡死或丢任务

#### Scenario: 防双续幂等
- **WHEN** 交棒杀与 reclaim 兜底可能对同一被中断 run 各触发一次续跑入队
- **THEN** 以「该 run 是否已有 `superseded_from` 子 run」为幂等键,同一被中断 run 至多生成一条续跑 run

### Requirement: 异常重启的 resume 兜底

系统在启动 `reclaim_orphan_runs` 回收残留 running 记录时,**SHALL NOT 无条件假设「running 已死」**（硬崩溃/断电/`kill -9` 时 CLI 子进程可能成孤儿存活）。SHALL 按「先接管 → 再判死 → 才续跑」处理：① 先 CAS 升 generation 接管（决策 8），使旧世代被 fencing、孤儿 CLI 无法写平台（杜绝双写）;② 用持久化 `pid + pid_create_time` 探活，进程不存在或 create-time 不匹配才判定已停;③ **仅在旧执行已确认停 或 已被 fencing 且进程树已清理时**，才据 `session_id` 追加入队续跑 run（带 `superseded_from` 幂等标 + 系统恢复豁免配额）;④ 既不能证明已停又不能保证 fencing/清理时,SHALL 置该 run 为 `recovery_blocked`（进 dead-letter 待人工），SHALL NOT 创建 recovery child。无 `session_id` 的 run 落 `failed`/现状兜底不变。

#### Scenario: 硬崩溃后先接管再判死才 resume
- **WHEN** Worker 非交棒地异常退出(崩溃),重启后 reclaim 发现残留 running run 且其有 session_id
- **THEN** 系统先 CAS 升 generation 接管（fencing 旧世代）、再用 pid+create_time 确认旧 CLI 已停或已清理,才追加入队续跑 run,新 Worker 领取后 `--resume` 续跑

#### Scenario: 无法确认旧执行已停则不续跑进 dead-letter
- **WHEN** reclaim 发现残留 running run，但无法证明其旧 CLI 已停止、也无法保证 fencing/进程树清理已生效
- **THEN** 系统置该 run 为 `recovery_blocked` 进 dead-letter 待人工介入，不创建 recovery child，避免与孤儿 CLI 双执行

#### Scenario: reclaim 不重复续跑
- **WHEN** 交棒流程已为某中断 run 入队续跑,随后 reclaim 又扫到该 run
- **THEN** 幂等键命中,reclaim 不再重复入队续跑

### Requirement: 反向代理平滑切换（自动无损重连）

系统 SHALL 通过反向代理(Nginx)对外暴露统一入口,支持蓝绿切换：更新时起新 API 实例、健康检查通过后切换 upstream 并平滑 reload,旧实例排空退出。SSE SHALL 每 15~30s 主动轮换断开、客户端按 `Last-Event-ID` 自动重连，给旧实例确定的排空上限;切换过程中 SHALL 保证进行中的用户请求正常完成、SSE 经自动重连后状态与日志不重不漏，**验收口径为「自动无损重连」而非绝对「同一连接不断」**。

#### Scenario: 蓝绿切换自动无损重连
- **WHEN** 改代码后起新 API 实例,健康检查通过,反向代理 reload 切换到新实例
- **THEN** 切换期间进行中的请求正常完成、SSE 流(或其重连)不报错,用户几乎无感知

#### Scenario: 新实例健康后才切流量
- **WHEN** 新 API 实例尚未通过 `/api/health`
- **THEN** 反向代理不将流量切到新实例,避免把请求打到未就绪的实例

### Requirement: 历史数据迁移与回滚兼容

新状态机与新列 SHALL 与存量数据兼容，且迁移 SHALL 在 SQLite ALTER 能力限制下（不支持给已有列加约束、加 `UNIQUE`/`NOT NULL`）闭环。`run_queue.status` 扩展新值域时，存量 `done` SHALL 一次性迁移为 `succeeded`，且读取层 SHALL 保留 `done→succeeded` 兼容映射直到全量升级稳定。`task_runs.run_queue_id` SHALL 分步落地（加可空列 → 反向回填存量 → 建 partial unique index `WHERE run_queue_id IS NOT NULL` → 新写入路径强制非空），SHALL NOT 追溯要求历史行非空。折叠 partial unique index 建立前 SHALL 先归并/取消存量重复 active 行。所有新列 SHALL 带安全默认（NULL/0）使存量行走首次执行/全量分支不报错。回滚到旧代码时旧代码 SHALL 能忽略新列、对未知状态保守兜底不崩。迁移前 SHALL 先安全在线备份（`VACUUM INTO`/online backup API，非 cp）。

#### Scenario: 存量 done 迁移且回滚可读
- **WHEN** 迁移把 `run_queue.status='done'` 改为 `'succeeded'`，随后阶段回退到旧代码
- **THEN** 新代码经 `done→succeeded` 兼容映射正确识别既有记录，旧代码回滚后读到 `succeeded` 亦按保守兜底不崩

#### Scenario: run_queue_id 分步加约束不伤存量
- **WHEN** 给已有 `task_runs` 表引入 `run_queue_id` 唯一非空关联
- **THEN** 系统先加可空列并回填存量、再建 partial unique index（容忍历史空值），新写入强制非空，不因 SQLite 无法直接加 NOT NULL/UNIQUE 而失败

#### Scenario: 折叠索引前清存量重复
- **WHEN** 建立 `run_queue(task_id, agent_slug)` active partial unique index
- **THEN** 系统先归并/取消存量同 (task,agent) 多条 active 行，索引建立不因存量重复而失败
