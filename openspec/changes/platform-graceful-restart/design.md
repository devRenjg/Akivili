# Design — 平滑重启 / 热更新

> 目标：改代码可随时重启 API,**用户访问不断、正在跑的 Agent 不中断**。分期 M1→M2→M3,每期独立可验证。本文记录架构、关键取舍与落地细节,**不含代码改动**。

## 现状（实地核查确认）

| 事实 | 位置 | 对平滑重启的影响 |
|------|------|------------------|
| Agent 执行是 API 进程内 asyncio 协程 | `collab._loop` / `_process_one` | 进程停 → 协程消失 → run 被判死 |
| CLI 是本进程 `subprocess.Popen` 子进程 | `executor/claude_code.py`,`codex.py` | 进程停 → 子进程成孤儿/被清 |
| SSE 直接持有 CLI stdout 边读边推 | `routes/runs.py` `event_stream` | 进程停 → 流式断 |
| 任务领取**已原子** | `collab._claim_one`（SELECT queued + UPDATE running + commit） | ✅ 多进程消费地基已具备 |
| 并发计数在内存 | `collab._running` 集合 | ✋ 需外置到 DB 才能多进程 |
| pid 注册表在内存,带创建时间指纹 | `executor/runner._RUN_PIDS` | ✋ 跨进程 kill 需重建;指纹可跨进程重算 |
| `task_runs.pid` 字段已存在 | DB schema | ✅ pid 可落库,跨进程可见 |

**核心矛盾**：Agent 执行寿命 = API 进程寿命。**解法：把执行层剥离成独立 Worker 进程。**

## 目标架构

```
          用户浏览器
              │
         ┌────▼────┐   Nginx 反代(:8100)  连接不断
         │  Nginx   │   upstream → API 实例(蓝/绿)
         └────┬────┘
              │
       ┌──────▼───────┐   塞任务/查状态/写 kill 标记   ┌──────────────┐
       │  API 进程     │ ─────────────────────────────→ │   SQLite      │
       │ (频繁重启)     │ ←── 尾随读 run_logs 转 SSE ───── │  (WAL 模式)    │
       └──────────────┘                               └──────▲───────┘
                                                             │ 写 run_logs/落状态/读 kill·交棒标记/读写 agent_sessions
                                                     ┌───────┴────────┐
                                                     │  Worker 进程     │ (稳定,少重启)
                                                     │ _loop + CLI 子进程│
                                                     └────────────────┘
```

- **API 进程**：HTTP、SSE、路由、业务逻辑——用户频繁改的这里。**不再跑 Agent**,只塞队列 + 尾随 run_logs 转发流式 + 写 kill 标记。无状态(状态全在 DB/Worker)→ 可任意重启、可蓝绿。
- **Worker 进程**：`reclaim_orphan_runs` + `_loop` 并发池 + 孤儿巡检 + 起 CLI 子进程。代码稳定,较少重启;要重启(改执行层)时走「温和重启」:先 defer 等在跑 run 自然收尾(能等则零中断);超时才杀在跑 CLI、把 run 标「待续」重入队 → 新 Worker 领取后依 `agent_sessions.session_id` **resume 续跑**。此时 CLI 秒级中断,上下文靠 session 保留,不需从头重跑。

## 关键设计决策

### 决策 1：流式改为「SSE 尾随 run_logs + 断点续传」（不做「文件唯一真相源+同步器」）

**背景修正**：早期为「托孤」设想过「CLI 输出直写文件、同步器回填 run_logs」——目的是让 CLI 输出脱离 Worker 进程存活,供托孤后新 Worker 续跟。**改走静默+resume 路线后,重启会中断 CLI,CLI 无需脱离进程独立存活,这套重活整段砍掉。**

**保留现状 + 只补 SSE 续传**：
- CLI stdout 维持经 Worker daemon 线程 → `runner._log()` 写 `run_logs`（现状不动,`_has_jian_deliverable`/`_has_trailing_stdout`/孤儿巡检 last_ts/转写详情全部零改动）。
- 唯一改造:**API 的 SSE 端点改为尾随 `run_logs`**——记已推的最大 `run_logs.id`,轮询/推送新增行到前端,直到 run 终态。这样 **API 重启导致 SSE 断连后,前端带「已收最大 log id」重连即可从断点续推,不重不丢**(尾随天然支持断点续传),满足「改 API 用户流式不断」。
- WAL(决策见下)让「Worker 写 run_logs + API 读 run_logs」不互锁。

**SSE 尾随的边界**：
- run 排队未起 → SSE 发「排队中」占位,Worker 领取起 CLI 后开始有日志。
- run 已终态(用户中途进入) → 一次性回放全量 + 收尾态,不需实时。
- API 重启导致 SSE 断 → 前端带「已收最大 log id」重连续传,不重不丢。
- **Worker 温和重启超时后 run 被中断→resume 续跑** → 续跑是**新的一次执行**(新 run),前端 SSE 会看到该 task 的执行链路衔接到新 run(与现有「一个 task 多条 run」的展示一致);被中断的旧 run 落终态(`superseded`)。

### 决策 2：调度状态外置——`_running` 计数改查 DB

- 现状 `_running` 是内存 set,存「本进程正在跑的 run_queue id」,用于并发上限判断。
- 外置：并发计数改为 `SELECT COUNT(*) FROM run_queue WHERE status='running'`(或 task_runs 同义)。
- `_claim_one` 已原子(UPDATE status='running'),多 worker 竞争领取天然安全,不重复领取。
- **与 concurrency 的 2.1 同一件事**：此项一次做,两个 change 都满足。

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

**目标**：改 `collab`/`executor` 等执行层代码需重启 Worker 时,在跑的 Agent **允许秒级中断,但上下文不丢、自动续跑,不需人工从头重跑**。

**为何弃托孤**：早期方案想让 CLI 脱离 Worker 独立存活(detached)+ Worker 重启后原子认领续跟(worker_id/心跳)+ 收尾重建(结束标记)+ reclaim 语义反转。评估后确认更优路线是**不做进程托孤**,而是「重启前中断 → 重启后用持久化 `session_id` 让 CLI resume 续」。一旦有了 [agent-session-resume] 的 resume 能力,进程级托孤的复杂度(跨进程认领、心跳、双跑防护、收尾解耦、Windows detached 控制台坑)就**不值得**——中断+resume 对多 Agent 平台已足够健壮,且代码量小一个数量级。

**温和重启机制（依赖 [agent-session-resume] 提供 session_id）**：
- **前置**：[agent-session-resume] 已为每个在跑 run 落了 CLI `session_id`（claude 从 stream-json 抓、codex 从 app-server threadId 抓，且流中途 pin 落库防崩溃丢指针），claude 与 codex 均已接入 resume（均必选）。
- **第 0 步 · 温和重启：先 defer 等空闲窗口**：收到重启意图后,若当前 `activeTasks > 0`（有在跑 run）,**优先不中断,等一个空闲窗口**（轮询 `activeTasks`、总计等待上限 **5 分钟**，参数化可调）——能等到所有在跑 run 自然收尾就零中断重启。仅当超过等待上限仍有在跑 run,才进入下面的「中断 + resume」硬路径。经验上多数重启可落在空闲窗口,根本不触发中断。等待期间**停止领新活**,避免边等边来新活永远等不完。
- **交棒流程（DB 标记,Windows 无优雅 SIGTERM,与 kill 标记同机制）**——仅在 defer 超时仍有在跑 run 时触发：
  1. 写「交棒」标记 → 旧 Worker 轮询到 → **停止领新活**。
  2. 旧 Worker 对每个在跑 run:**杀其 CLI 子进程树**（`taskkill /F /T` + pid 指纹防误杀）、把 run 标为 **「待续」(supersede) 并落终态**（旧 run → `superseded`）、按其 `(conversation, agent)` **重新入队一条续跑 run**（携带「resume 该 session」意图 + `superseded_from`）→ 旧 Worker 退出。
  3. 起新 Worker（新代码）→ 从队列领取续跑 run → 依 `agent_sessions.session_id` resume 启动 CLI → Agent 从上次上下文续跑。
- **续跑喂什么 prompt**：续跑 run **重发原始任务 prompt**（重新 `build_cli_prompt`）+ resume 指针,**不喂空、不造「继续」指令**。靠 resume 恢复记忆 + prompt 约束（「聚焦本轮、只做一次」）防重复。resume 确认未落地时,prompt 前置「上轮会话未能恢复,这是新会话,请如实告知用户」披露。此语义与 agent-session-resume 的常规 resume 统一（见其 design「续跑时的 prompt 语义」）。
- **副作用重复防护（不做服务端 exactly-once）**：被中断的 run 可能已产生副作用（建卡/comment/改状态,已落库）。续跑靠 **session 记忆 + prompt 约束**（「你之前可能已提交过,先检查再动手」「只做一次,即便非零退出也不重试」）防重复,**不引入服务端精确幂等键**。关键写操作（如建卡）可加轻量自然去重,但不追求 exactly-once。
- **续跑不吃防死循环配额（失败原因白名单区分）**：续跑 run 是**系统触发**（非 Agent 自发 @），标记为系统恢复类,**豁免** `MAX_MENTION_CHAIN` 空转链计数、不占 `MAX_RUNS_PER_TASK` 配额,避免频繁重启啃配额甚至误熔断。
- **无 session 可依的 run 的兜底**：首次执行尚无 session_id、或 poisoned 失败已丢 session 的 run → 无法 resume → 落 `failed` 重排队(现状兜底,等于从头重跑该次分派)。codex 已接入 resume,不再是兜底主因。
- 中断到续跑之间秒级;上下文靠 CLI session 保留,不靠进程存活。

**`reclaim_orphan_runs` 维持现状语义 + 一处增强**：
- 现状：「启动时所有残留 running 判孤儿、落终态」——**保留**（重启会真的中断 CLI,running 确实都死了,现状假设成立,无需「探活认领」的反转）。
- 增强：reclaim 落终态某 running run 时,**若该 run 有可用 `session_id`（agent-session-resume 已存）→ 除落终态外,再入队一条续跑 run**（携带 resume 意图),使异常重启(非交棒的硬崩溃)也能 resume 续。无 session → 现状落终态/兜底不变。
- 好处:不引入 worker_id/心跳/双跑防护/收尾重建等全部托孤复杂度;续跑天然复用现有「队列领取 + 起 CLI + 收尾」全链路,收尾幂等由现有 `_finalize_if_running` 保证。

**防重复续跑**：续跑 run 入队时打标（如 `superseded_from=<旧 run_id>`),避免交棒杀 + reclaim 兜底对同一 run 各入队一次导致双续。以「旧 run 是否已生成续跑 run」为幂等键。

## 落地技术细节

**T0 — 温和重启 defer 窗口（M2.5 前置）**：重启意图到达后先停领新活、轮询 `activeTasks`；全部在跑 run 自然收尾即零中断重启。等待上限 **5 分钟**（参数化可调）内未清零才转入交棒硬路径。参数化配置,避免长任务把重启无限拖住。

**T1 — SSE 尾随 run_logs + 断点续传（M1 唯一改造）**：SSE 端点从「直连 CLI stdout 边读边推」改为「尾随 `run_logs`」——查 `run_logs WHERE run_id=? AND id > <前端已收最大 id> ORDER BY id`,轮询(~200ms)推新增行。前端重连带「已收最大 log id」即从断点续,不重不丢。CLI 写 run_logs 的路径(Worker 线程 → `_log()`)**不动**。（「seq/id 单调 + DB 持久化 + 按位置增量回灌」的续传模式,单机单 API 不需要跨节点消息中继。）

**T2 — 交棒时的中断与重入队（M2.5 核心）**：交棒标记轮询到后,旧 Worker 对每个在跑 run:① `taskkill /F /T` 杀 CLI 进程树(pid 指纹防误杀);② 旧 run 落终态(`superseded`,**不触发自动流转**——不误判子任务 done/父任务 reviewing);③ 依 `agent_sessions` 有无可用 session_id 决定——有则入队「续跑 run」(标 `superseded_from=旧run_id` + resume 意图 + 系统恢复标记豁免配额)、无则入队普通重跑(从头)。新 Worker 领取续跑 run,重发原 prompt + 交给 [agent-session-resume] 的 resume 路径起 CLI。

**T3 — reclaim 增强（异常重启也能续）**：`reclaim_orphan_runs` 维持「running 判死落终态」;增强:落终态时若该 run 有 `session_id` → 追加入队一条续跑 run(同样打 `superseded_from` 幂等标)。覆盖非交棒的硬崩溃场景。

**T4 — 防双续幂等**：交棒杀 + reclaim 兜底可能对同一旧 run 各触发一次续跑入队。以「旧 run 是否已有 `superseded_from=该run` 的子 run」为幂等键,已有则不再入队,保证一个中断 run 至多一条续跑。

**T5 — kill 标记 vs 交棒标记（两回事，都走 DB 轮询）**：
- **kill 标记**(run 级)：用户主动终止某 run → 杀 CLI + 落终态,**不续跑**(用户就是要停)。
- **交棒标记**(Worker 级)：重启执行层 → 杀所有在跑 CLI + **续跑**。
- 二者语义相反(kill 不续、交棒续),实现上以标记来源区分:交棒触发的中断带 resume 意图,kill 触发的不带。

**T6 — 收尾幂等（复用现状，不需收尾重建）**：续跑是「新 run 走完整现有收尾链路」,旧 run 已由交棒/reclaim 落终态。收尾幂等靠现有 `_finalize_if_running`(`WHERE status='running'` 条件更新)——无需为托孤设计「任意 Worker 基于日志重建收尾」那套。

| 里程碑 | 交付 | 达成的用户价值 | 验证 |
|--------|------|----------------|------|
| **M1** 流式与并发地基 | WAL+busy_timeout;SSE 尾随 run_logs + 断点续传 | API 重启后前端流式可续(不重不丢);WAL 让读写不互锁 | 新增「SSE 尾随续传」探针:断连带 last-id 重连从断点续、不重不丢、run 终态回放全量 |
| **M2** 执行层剥离 | worker.py;API 不 start_loop;`_running`→DB;kill→DB 标记;pid 落库 | **重启 API 时 Agent 不中断** | 新增「多进程消费 + 跨进程 kill」探针:两进程不重复领取、API 写标记 Worker 杀成功、停 API 保 Worker 后 run 仍推进 |
| **M2.5** 温和重启 + resume 续跑(核心) | defer 等空闲窗口;超时才交棒;旧 Worker 杀在跑 CLI + run 标 `superseded` + 入队续跑(重发原 prompt+豁免配额);reclaim 增强追加续跑;防双续幂等 | **重启 Worker(改执行层)时:能等则零中断;等不到则秒级中断后 resume 续跑,上下文不丢** | 新增「温和重启续跑」探针:①有在跑 run 时先 defer、run 收尾后零中断重启 ②defer 超时转交棒、旧 run 落 superseded、续跑带 resume 意图+系统恢复标记 ③新 Worker 重发原 prompt 以 resume 续 ④防双续幂等 ⑤无 session 走从头重跑兜底 ⑥续跑不吃 mention-chain/runs-per-task 配额 |
| **M3** 连接平滑 | Nginx 反代 + 蓝绿脚本 | **改代码重启 API 用户连接不断** | 手动验证:压测期间蓝绿切换,进行中的 SSE 与请求不报错 |

- **顺序**：M1 → M2 → M2.5 → M3。M2 依赖 M1(WAL + SSE 续传);**M2.5 依赖 [agent-session-resume] 先出 resume 能力** + 本 change M2(独立 Worker + 交棒机制);M3 依赖 M2(API 无状态才可蓝绿),与 M2.5 相对独立可并行。
- **达标线**：M1+M2 达成「改 API 平滑」;**M2.5 达成「改执行层重启也不丢上下文(resume 续跑)」**;M3 达成「用户连接不断」。
- **回退**：每期独立,若 M2.5 遇阻(如 agent-session-resume 未就绪)可停在 M2(改 API 已平滑、改执行层暂退回「重启从头重跑」),不影响已达成的能力。

## 与 [platform-concurrency-scaling] 的协调（避免重复/冲突）

| 事项 | 本 change (graceful-restart) | concurrency-scaling | 约定 |
|------|------------------------------|---------------------|------|
| WAL + busy_timeout | M1 | 阶段 0.1 | **同一件事,谁先做另一个即满足** |
| 调度状态外置(`_running`/pid→DB) | M2 | 阶段 2.1 | **由本 change M2 实现,concurrency 2.1 复用** |
| 多 worker 无状态消费 | M2 达成「可多进程消费」地基(先跑单 worker) | 阶段 2.2 | 本 change 出地基,concurrency 2.2 在其上做「多 worker 并发规模与公平」 |
| `_claim_one` 原子领取 | 复用(已具备) | 复用(已具备) | 无需改 |
| 同 slug 串行 / 公平调度 / Postgres | 不涉及 | concurrency 阶段 0/1 负责 | 本 change 不碰,避免动机混淆 |

**分工原则**：本 change 只解决「解耦 + 平滑重启」(单 API + 单 Worker);「多 worker 并发规模、项目公平、Postgres」归 concurrency。两者共享「WAL + 状态外置」地基,一次实现、双方受益。

## 非目标（本 change 明确不做）

- 不做 Postgres 迁移(归 concurrency 阶段 1)。
- 不做多 worker 并发规模与项目公平调度(归 concurrency 阶段 2.2/1.3)。本 change 出「多进程可消费」地基,但只跑单 Worker。
- 不做代码级热替换(`importlib.reload` 对有状态长运行服务不可靠,已排除)。
- **不做 CLI 进程托孤**(detached 独立存活、worker_id 认领、收尾重建、reclaim 语义反转)——已被静默+resume 路线取代,复杂度不值(见决策 5)。
- **不实现 resume 本身**——resume(session_id 抓取/存储/`--resume`/增量回灌)归独立 change [agent-session-resume];本 change 只负责「重启时中断 + 重入队触发续跑」。
- 不追求逐字符零延迟流式(SSE 尾随近实时已满足体验)。
- 不改 Agent 执行模型/不压缩硬墙钟;用 resume 续跑解决长任务重启不丢上下文,而非把任务拆短或让 CLI 托孤长活。

