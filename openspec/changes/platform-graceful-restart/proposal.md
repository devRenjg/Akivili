## Why

当前后端是「单进程」架构：HTTP/SSE、业务路由、多 Agent 协同调度（`_loop` 并发池）、CLI 子进程,全部绑在同一个 Python 进程里。因此**每次改代码/优化都要重启整个进程**,而重启会：

1. **中断正在跑的 Agent**——CLI 子进程是本进程用 `subprocess.Popen` 起的,进程一停,`_process_one` 协程消失、收尾逻辑跑不到,正在执行的 run 被判死(靠 `reclaim_orphan_runs` + 孤儿巡检补成 `killed`)。任务被迫重跑。
2. **断开用户连接**——用户看任务详情/看流式输出走的是本进程的 SSE 长连接,进程停即断,前端短暂报错。

用户诉求(已确认)：**改代码就重启,很频繁**,且要求**用户访问不断 + Agent 任务不中断**——即「无感知的平滑重启/热更新」。

**根因**：Agent 执行的寿命 = 后端进程寿命。要做到平滑重启,必须打破这个绑定——把「执行层」从「API 层」剥离,使 API 可以随意重启而 Agent 继续跑。

**路线修正**：早期方案设想「CLI 托孤」——让 CLI 子进程脱离 Worker 独立存活、Worker 重启后原子认领续跟(detached/文件唯一源/收尾重建/worker_id 认领/reclaim 反转/交棒标记)。评估后确认更优路线是**不做进程托孤,而走「温和重启 + 逻辑 session resume」**——重启时优先等在跑执行自然收尾（能等则零中断），等不到才中断、重启后靠持久化的 CLI `session_id` 把 run 重新入队、resume 续跑,上下文不丢。一旦具备 resume 能力（见独立 change [agent-session-resume]），**托孤的进程级复杂度就不值得了**:温和重启→（超时）中断→resume 续接对多 Agent 平台已足够健壮,且实现量小一个数量级。故本 change 的 M2.5 改走「温和重启+resume」,砍掉全部托孤机制;M3 的 Nginx 蓝绿保留。

## What Changes

> 规划态,分期落实(M1→M2→M2.5→M3),每期独立走探针验证、独立提交。**目标:改 API 层代码平滑无感;改执行层代码重启 Worker 时,在跑 Agent 被静默中断后自动 resume 续跑(不丢上下文、不需人工重跑)。本 change 暂不改任何代码。**

- **M1（流式与并发地基）**：SQLite 开 `WAL + busy_timeout`；SSE 端点改为**尾随 `run_logs`**（记已推最大 id,断连重连带位置续传,不重不丢),使 API 重启后前端流式可续。CLI 输出维持经 Worker 写 `run_logs`（不做「文件唯一真相源+同步器」那套重活——静默+resume 路线不需要 CLI 脱离进程存活）。
- **M2（执行层剥离）**：新建独立 **Worker 进程**消费队列、起 CLI、跑 Agent；**API 不再 `start_loop()`**,只负责 HTTP/API + 塞队列 + 尾随转 SSE。内存态外置：`_running` 计数查 DB、kill 改「API 写 DB 标记 → Worker 轮询自杀子进程树」。**做完这步,重启 API 时 Agent 不中断。**
- **M2.5（温和重启 + resume 续跑 = 执行层平滑核心，取代托孤）**：改执行层需重启 Worker 时,**先 defer 等空闲窗口**（有在跑 run 就先不中断、停领新活、等一个上限窗口，默认 5 分钟，多数重启可等到全部自然收尾→零中断）。仅当超时仍有在跑 run 才转硬路径：旧 Worker 收 DB 交棒标记 → 杀在跑 CLI 子进程树、run 标 `superseded`（不触发自动流转）+ 按 `(conversation,agent)` 入队一条续跑 run（`superseded_from` + resume 意图 + 系统恢复标记豁免死循环配额）→ 退出;新 Worker(新代码)领取续跑 run，**重发原始任务 prompt** + 依 [agent-session-resume] 的 `session_id` resume 续跑——CLI 秒级中断,但**上下文不丢、无需从头重跑**。续跑靠 session 记忆 + prompt 约束防副作用重复（不做服务端 exactly-once）。首次执行无 session / poisoned 已丢 session 的 run 落 failed 重排队(现状兜底)。**依赖 [agent-session-resume]（claude+codex 均已必选接入）。**
- **M3（连接平滑）**：引入 **Nginx** 反向代理 + 蓝绿:改代码后起新 API 实例 → 健康检查 → `nginx -s reload` 平滑切 upstream → 旧 API 退。API 无状态,切换零成本、连接不断。

## Capabilities

### New Capabilities
- `platform-graceful-restart`: 平台的平滑重启/热更新能力——API 层与执行层(Worker)解耦、跨进程 kill、SSE 尾随续传、Worker 静默重启后依 session `--resume` 续跑在跑 run、反向代理蓝绿切换。综合达成:**改 API 层用户连接不断且 Agent 不中断;改执行层 Agent 被秒级静默中断后自动 resume 续跑、上下文不丢。** resume 能力由独立 change [agent-session-resume] 提供。

## Impact

- **规划态,暂不改代码。** 落实时预计涉及：`database.py`(WAL/busy_timeout)、`routes/runs.py`(SSE 尾随 run_logs + 断点续传、kill 写 DB 标记)、`collab.py`(`_running`→DB、`_loop` 挪 worker、`reclaim_orphan_runs` 对「待续」run 走重入队 resume、交棒/kill 标记轮询)、`executor/*.py`(交棒时杀在跑 CLI、run 标「待续」)、`task_runs` 加列(kill 标记/交棒标记/「待续」标记)、新增 `worker.py` 入口、新增 `deploy/nginx.conf` + 蓝绿/交棒脚本。**不涉及** detached 启动、文件唯一真相源、同步器、worker_id 认领、收尾重建（静默+resume 路线已砍掉这些）。
- **强依赖 [agent-session-resume]**：M2.5 的「resume 续跑」依赖该 change 先提供 per-agent `session_id` 存储与 `--resume` 能力。二者关系:agent-session-resume 是独立优化(省 token/上下文连贯),同时是本 change M2.5 的地基;本 change 只负责「重启时怎么中断、怎么重入队触发 resume」,不负责 resume 本身的实现。
- **与 [platform-concurrency-scaling] 强协调（关键）**：该 change 的阶段 2（2.1 调度状态外置 / 2.2 多 worker 无状态消费）与本 change 的 M2 是**同一块地基**。约定：**由本 change 的 M2 实现「调度状态外置 + worker 无状态化」,concurrency 的 2.1/2.2 复用该成果、不重复实现。** 本 change 聚焦「单 API + 单 Worker 的解耦与平滑重启」,concurrency 聚焦「多 worker 并发规模与公平」,二者共享外置地基、动机与验收各自独立。
  - 顺序建议：本 change M1(WAL)与 concurrency 阶段 0.1(WAL)是同一件事,谁先做另一个即已满足。
  - `_claim_one` 已是原子领取(SELECT queued + UPDATE running + commit),多进程消费的地基已具备,两 change 都受益。
- **关联能力**：[agent-execution](执行/kill/孤儿兜底)、[agent-collaboration](调度/队列)、[task-system](run_logs/SSE)。
- **关联记忆**：`backend-restart-single-instance`(重启前须杀净 8100 监听、reload 双实例坑)——M3 的蓝绿正是这条坑的正式解法;落地后该手动流程被 Nginx reload 取代。
- **平台事实**：Windows 上 CLI 子进程用 `taskkill /F /T` 杀进程树;pid 带「创建时间指纹」防复用误杀——M2 跨进程 kill 须沿用该指纹机制并在跨进程场景重新验证。
