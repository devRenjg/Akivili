## Why

当前后端是「单进程」架构：HTTP/SSE、业务路由、多 Agent 协同调度（`_loop` 并发池）、CLI 子进程,全部绑在同一个 Python 进程里。因此**每次改代码/优化都要重启整个进程**,而重启会：

1. **中断正在跑的 Agent**——CLI 子进程是本进程用 `subprocess.Popen` 起的,进程一停,`_process_one` 协程消失、收尾逻辑跑不到,正在执行的 run 被判死(靠 `reclaim_orphan_runs` + 孤儿巡检补成 `killed`)。任务被迫重跑。
2. **断开用户连接**——用户看任务详情/看流式输出走的是本进程的 SSE 长连接,进程停即断,前端短暂报错。

用户诉求(已确认)：**改代码就重启,很频繁**,且要求**用户访问不断 + Agent 任务不中断**——即「无感知的平滑重启/热更新」。

**根因**：Agent 执行的寿命 = 后端进程寿命。要做到平滑重启,必须打破这个绑定——把「执行层」从「API 层」剥离,使 API 可以随意重启而 Agent 继续跑。

**路线修正**：早期方案设想「CLI 托孤」——让 CLI 子进程脱离 Worker 独立存活、Worker 重启后原子认领续跟(detached/文件唯一源/收尾重建/worker_id 认领/reclaim 反转/交棒标记)。评估后确认更优路线是**不做进程托孤,而走「温和重启 + 逻辑 session resume」**——重启时优先等在跑执行自然收尾（能等则零中断），等不到才中断、重启后靠持久化的 CLI `session_id` 把 run 重新入队、resume 续跑,上下文不丢。一旦具备 resume 能力（见独立 change [agent-session-resume]），**托孤的进程级复杂度就不值得了**:温和重启→（超时）中断→resume 续接对多 Agent 平台已足够健壮,且实现量小一个数量级。故本 change 的 M2.5 改走「温和重启+resume」,砍掉全部托孤机制;M3 的 Nginx 蓝绿保留。

## What Changes

> **经技术负责人 Review 修订（2026-07-16，判定 Request changes）**：核心 = 先把方案从「resume 功能列表」升级为「持久化执行协议」——先定义 durable execution 状态机 + 并发不变量，再实现。分期改为**阶段 0-6（Worker 剥离优先、resume 后置）**。**本 change 暂不改任何代码。**

修正原方案 3 处事实错误：① `_claim_one` **非原子**（UPDATE 无 CAS，多 Worker 会双领）——原写「已原子/地基已具备」错误;② 人工 @ 主受理人**在 API 请求内同步执行**（`routes/runs.py`），M2 未覆盖此主路径;③ `run_queue` **无 conversation_id 列**，原 spec 写的 partial index 建不出来，且「折叠」当前是丢弃。

- **阶段 0 — 补执行协议规格**：定义状态机 `accepted→queued→claimed→running→{succeeded/failed/killed/superseded(+recovery child)}` + 7 条不变量（原子持有、单 running+单 pending intent、CAS 终态、supersede+child 同事务、旧 generation fencing、有界恢复、事务边界）。见 design 决策 0。
- **阶段 1 — DB 协议地基**：`WAL + 每连接 busy_timeout + 安全在线备份 + 索引`；**所有触发统一入队**；**两段式 dispatch**（POST 幂等入队返回 execution_id + GET 独立 SSE 订阅）；**原子 claim（CAS 单语句）**；重复触发合并（不丢）；`task_run↔run_queue` claim 时即建不可变关联。
- **阶段 2 — 执行层剥离**：独立 **Worker 进程**；API 不执行 Agent；Worker **supervisor/heartbeat/readiness**；**generation/lease/单实例**；CLI **进程 containment**（Windows Job Object / POSIX 进程组，防崩溃后孤儿 CLI）；kill 带 request/ack。**→ 重启 API 不断 Agent。**
- **阶段 3 — Claude resume**、**阶段 4 — Codex app-server**：归 [agent-session-resume]（省 token + 上下文连贯 + 为续跑提供 session）。
- **阶段 5 — 交棒 + 有界恢复**：restart generation + claim barrier；**defer 5 分钟**等自然收尾（能等则零中断）；超时 kill + **CAS supersede + recovery child 同事务**；**bounded recovery（次数上限/退避/dead-letter）**；stale generation fencing。**→ 改执行层温和重启、resume 续跑，副作用语义明确为 at-least-once。**
- **阶段 6 — 连接平滑**：**Nginx** 蓝绿；SSE **主动轮换 + Last-Event-ID** 自动重连（给旧 API 确定排空上限）；expand/contract 部署。验收口径 = 「自动重连、状态与日志不重不漏」，非绝对「同一连接不断」。

## Capabilities

### New Capabilities
- `platform-graceful-restart`: 平台的持久化执行协议 + 平滑重启能力——durable execution 状态机 + 并发不变量、原子 claim、两段式 dispatch、API/执行层解耦、进程 containment、Worker generation/fencing、温和重启（defer 优先）+ 超时 resume 续跑（有界恢复）、SSE 续传、反向代理蓝绿。综合达成:**改 API 层用户自动无损重连且 Agent 不中断;改执行层温和重启（能等则零中断，超时 resume 续跑不丢上下文，副作用 at-least-once）;无双执行、无触发丢失。** resume 能力由独立 change [agent-session-resume] 提供。

## Impact

- **规划态,暂不改代码。** 落实时预计涉及：`database.py`(WAL/busy_timeout/索引;`run_queue` 加列 idempotency_key/claim_owner/claim_generation/claimed_at/lease_until/recovery_chain_id/recovery_attempt/superseded_from/rerun_requested/pending_through_message_id;`task_runs` 加列 run_queue_id UNIQUE NOT NULL/worker_generation/pid_create_time;新增 `worker_state` 单行表)、`routes/runs.py`(两段式 dispatch:POST 幂等入队+GET SSE 订阅、SSE 契约含 Last-Event-ID/superseded 跳转、kill request/ack)、`collab.py`(原子 claim CAS、`_running`→DB、`_loop` 挪 worker、触发合并不丢、reclaim 增强、交棒 generation/ack、fencing 校验)、`executor/*.py`(进程 containment:Job Object/start_new_session、交棒杀 CLI)、新增 `worker.py` 入口 + supervisor、新增 `deploy/nginx.conf` + 蓝绿脚本、`jian` 写接口加 generation fencing 校验。**不涉及** detached 独立存活、文件唯一真相源、worker_id 认领、收尾重建（已砍）。
- **强依赖 [agent-session-resume]**：M2.5 的「resume 续跑」依赖该 change 先提供 per-agent `session_id` 存储与 `--resume` 能力。二者关系:agent-session-resume 是独立优化(省 token/上下文连贯),同时是本 change M2.5 的地基;本 change 只负责「重启时怎么中断、怎么重入队触发 resume」,不负责 resume 本身的实现。
- **与 [platform-concurrency-scaling] 强协调（关键）**：该 change 的阶段 2（2.1 调度状态外置 / 2.2 多 worker 无状态消费）与本 change 的 M2 是**同一块地基**。约定：**由本 change 的 M2 实现「调度状态外置 + worker 无状态化」,concurrency 的 2.1/2.2 复用该成果、不重复实现。** 本 change 聚焦「单 API + 单 Worker 的解耦与平滑重启」,concurrency 聚焦「多 worker 并发规模与公平」,二者共享外置地基、动机与验收各自独立。
  - 顺序建议：本 change M1(WAL)与 concurrency 阶段 0.1(WAL)是同一件事,谁先做另一个即已满足。
  - `_claim_one` **当前非原子**（SELECT + 无 CAS 的 UPDATE），本 change 阶段 1 补 CAS 后多进程消费地基才具备;concurrency 复用该成果（Review 修正原「已原子」误判）。
- **关联能力**：[agent-execution](执行/kill/孤儿兜底)、[agent-collaboration](调度/队列)、[task-system](run_logs/SSE)。
- **关联记忆**：`backend-restart-single-instance`(重启前须杀净 8100 监听、reload 双实例坑)——M3 的蓝绿正是这条坑的正式解法;落地后该手动流程被 Nginx reload 取代。
- **平台事实**：Windows 上 CLI 子进程用 `taskkill /F /T` 杀进程树;pid 带「创建时间指纹」防复用误杀——阶段 2 跨进程 kill 须沿用该指纹机制并在跨进程场景重新验证;并新增 Job Object containment 使 Worker 崩溃时 OS 自动清理。
