# Design — per-agent CLI session + 增量回灌

> 目标：把「每次执行全量回灌历史」换成「每个 (task, agent) 维护一条 CLI session、再执行时 resume 续接 + 只喂增量」。省 token、上下文连贯，并为 [platform-graceful-restart] M2.5「温和重启+resume」提供 resume 地基。**claude 与 codex 均为必选**（codex 是重度使用的 backend）。本文不含代码改动。

## 设计哲学

核心原则：**「别过度接管，能下推给 CLI/prompt 的就下推」**——平台只存 `session_id` 指针 + workdir，对话上下文由 CLI 原生 session 恢复；不追求副作用 exactly-once，靠 prompt 约束 + session 记忆 + 任务级去重。本方案全面遵循这套策略，仅在一处有意偏离（见决策 3 末）。

## 现状（实地核查）

| 事实 | 位置 | 影响 |
|------|------|------|
| 每次执行独立建 CLI 会话，无 session 复用 | `runner.py` L3 注释「无 CLI session；靠记忆+历史回灌恢复」 | 丢失 CLI 原生连续记忆 |
| 执行前全量取历史 | `runner.py` `SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id` | 长任务 token 高 |
| 历史双限裁剪 | `_clip_history`(`_HISTORY_MAX_MSGS` + `_HISTORY_MAX_CHARS`) | 到上限裁早期上下文 → 丢信息 |
| 历史拼进 prompt 文本 | `base.py::build_cli_prompt`(【用户】/【队友】行) | CLI 每次「重读历史」而非续接 |
| claude session_id 被丢弃 | `claude_code.py::_parse_line`（system/result 行只当「执行完成」） | 有 session_id 却没存 |
| codex 为一次性 `codex exec --json -m - -` | `codex.py` | 无 thread 复用，resume 需改集成模式 |
| claude 支持 `--resume <sid>`；codex 支持 app-server `thread/resume` | CLI 原生能力 | 我们都没用 |

**核心**：Agent 的连续记忆 = 每次拼进 prompt 的历史文本，而非 CLI 自己的 session。→ **解法：per-(task, agent) session + resume + 增量回灌。**

## 关键设计决策

### 决策 1：粒度 = per-(conversation_id, agent_slug)，且同 (task, agent) 串行

- 粒度取 **(conversation_id, agent_slug)**——同一 task 里每个成员各持独立 session，互不干扰。
- 新表 `agent_sessions(id, conversation_id, agent_slug, session_id, committed_msg_id, provider_id, backend, workdir, updated_at)`，唯一键 `(conversation_id, agent_slug)`。`committed_msg_id` = 上次**成功**执行确认喂到的水位（见决策 3）。本次执行的快照终点 `planned_through_msg_id` 落在 `task_runs` 行（每 run 一行、天然是历史）。
- **同 (task, agent) 串行 + 折叠（Review P0-3 修正：不是 index、不是丢弃）**：目标 = 同一成员在同一 task 上**至多一个 queued/running run**，重复触发**折叠**进下一轮（合并触发意图，不丢），而非并行起第二条、也不是当前代码的直接 `return None` 丢弃。**注意**：`run_queue` **无 conversation_id 列**，无法直接建 `(conversation_id, agent_slug)` partial unique index（原 spec 写法建不出来）;改用「折叠模型」实现（见决策 1b）。这样既防重复并行做工，又让「增量边界」有确定的推进时机。

### 决策 1b：重复触发折叠模型（Review P0-3，取代「查重丢弃」与「伪 index」）

现状 `collab.py:392` 对同 task/agent 已有 queued/running 时直接 `return None`——触发被静默丢。改为持久化折叠：

- **目标 run 为 queued（未 claim）**：把新触发的消息水位/触发来源**合并**进该 queued 行（更新其 planned 上界），不新建行。
- **目标 run 为 running**：在其 `run_queue` 行持久化 `rerun_requested=1` + `pending_through_message_id`（合并到最大）+ 触发来源集合。
- **该 running run 收尾事务内**：检查 pending intent，若有则**至多创建一个** successor run（带合并后的水位），然后清 pending。
- **区分 enqueue plan 与 delivery receipt**：记录「计划喂到哪」与「实际随 claim 投递了哪些」，避免新旧 Worker 协议不一致时误判消息已送达（简化实现 = 存消息高水位 + 必要触发 ID，不必像多节点那样存完整投递集）。
- **不降低全局并发**：全局并发上限（当前 8）约束的是「不同 (task, agent) 同时跑几个」，同 (task, agent) 串行与之正交——不同成员、不同任务照旧并行。

### 决策 2：session_id 抓取与生命周期（三件套 + poisoned 丢弃）

- **每轮覆盖存最新**：resume 后 CLI 可能返回新的 session_id（这正是需要落地判定兜底的原因）。每次执行收尾都以本次输出的最新 session_id 覆盖存，**不是只存首次**。
- **空值保护（COALESCE）**：更新用 `SET session_id = COALESCE(?, session_id)`——本次没抓到 id 时不清空旧指针。
- **流中途抢先落库（pin）**：流里第一次看到 session_id 就落库一次，防执行中途崩溃丢指针（这条对 [platform-graceful-restart] 的 resume 续跑至关重要）。
- **resume 落地判定（Review P1-6 修正：mismatch 只在失败时判）**：
  - `失败 && emitted_id != requested_id`（或 stderr "no conversation found"）→ 判 resume **未落地**，返回空触发 fresh 重建。
  - `成功 && emitted_id != requested_id` → **正常**（resume 后 CLI 可能 fork 新 session id）→ **接受并覆盖保存** emitted_id，不判失败。
  - 原方案「emitted≠requested 即判未落地」**过严**，会误杀成功续跑，已修正。
- **poisoned 失败丢 session**：某些失败类型（迭代上限 `iteration_limit`、模型 400 `api_invalid_request`、codex 语义静默 `codex_semantic_inactivity`）会污染会话状态，resume 它们只会重放坏状态 → 这类失败**主动丢弃 prior session**，下次从头重建。

### 决策 3：增量回灌——2 字段水位（committed + planned，at-least-once 安全）

**不变量**：崩溃时**宁可重复喂，绝不能漏喂**（at-least-once）。用 2 个字段实现（对齐用户拍板，不用三段）：

- **`agent_sessions.committed_msg_id`**：上次**成功**执行确认喂到的水位——**只有 run 成功收尾才推进**。
- **`task_runs.planned_through_msg_id`**：本次构建 prompt 那刻的快照终点 = 该 conversation 当时的 `MAX(messages.id)`。落在 task_runs（每 run 一行、天然是历史快照），不污染 session 主状态。
- **起点不单存**：本次增量起点永远 = 上次的 `committed_msg_id`，查出来即可。
- **增量 = `messages WHERE conversation_id=? AND id > committed_msg_id AND id <= planned_through_msg_id AND 非本 agent 自产`**（参数化）：
  - 别人（含人工）在上次成功水位之后、本次快照之前说的话 → 喂给本 Agent。
  - 本 Agent 自己的历史发言由 CLI session 记忆承载，**不重喂**。
- **为何 at-least-once 安全**：
  - A 成功 → `committed_msg_id` 推进到 A 的 `planned_through`;下次从此起点，不重不漏。
  - A **崩溃/被中断**（committed 未推进）→ 续跑仍从**同一 committed 起点**取增量 → **重复喂但不漏**（满足不变量）。
  - A 执行期间 B 写的消息 id > A 的 planned_through → 不在 A 本次增量内，但下次触发 A 时（committed 仍在 B 之前）被正确纳入，**不漏**。
- **崩溃不漏的关键**：**只有成功才提交 committed**;prompt 构建后、CLI 接收前崩溃时 committed 不得提前推进（否则漏）。
- 增量仍过 `_clip_history` 防单次海量。首次执行（无 session）回灌全量（现状行为）。
- **等效方案对比**：另一种防漏思路是维护「投递集」（记录已喂给该 agent 哪些 message）+ 排队期消息「折叠」进本轮；我们用「快照水位 + 排除自产」达到等效防漏，省一张投递集表。
- **一处有意取舍**：也可让 Agent 用 CLI 命令**自己回读**业务历史、平台几乎不喂。我们**保留平台侧增量喂**（不建 Agent 自读命令），因为：① 我们的 `jian` CLI 未暴露全量 message 列表读取；② 增量在快照机制下很小；③ 改动面小于「造一套 Agent 自读历史的命令 + 改 prompt 教 Agent 用」。留待讨论期复核。

### 决策 4：降级链（健壮性核心，保证不劣于现状）

任何一步不确定都回退到「全量回灌 + 新建会话」：

| 情形 | 处置 |
|------|------|
| 首次执行(无 `agent_sessions` 行) | 开新 session（不带 resume）+ **全量回灌**；成功后存 session_id + committed_msg_id |
| resume 未落地（**失败** && "no conversation found" 或 emitted id≠请求 id；成功返回新 id 不算） | 清失效 session_id → 本次降级全量 + 新建 → 更新 session_id |
| **poisoned 失败**（iteration_limit / api 400 / codex 语义静默） | **丢弃 prior session** → 下次从头重建（不 resume 坏状态） |
| `provider_id`/`backend` 变更 | 弃旧 session、开新 + 全量 |
| `workdir` 变更 | 开新 + 全量 |
| 跨机器（未来多机） | runtime 不匹配不 resume（resume 要求 runtime 一致）|

### 决策 5：backend——claude（flag 式）与 codex（app-server 式）均必选

用户重度使用 codex，故 codex resume 为**必选项**，与 claude 并行推进（不再后置）。两者共用 `agent_sessions` 表与降级链，但集成模式差异巨大（codex 的 app-server 集成代码量约为 claude flag 方案的数倍）：

- **claude（改动小）**：现状 `-p ... stream-json` + prompt 走 stdin。resume = 追加命令行 flag `--resume <session_id>`。session_id 从 stream-json 的 system/result 行提取。
- **codex（改动大，集成模式变更）**：现状 `codex exec --json -m - -`（一次性）。resume 需改为 **每次执行 attempt 启动一个 `codex app-server --listen stdio://` 进程 + JSON-RPC**（Review P1-7 明确：**每 run 一个 app-server 进程、run 结束即关**，**非** Worker 全局共享一个——全局共享会显著增加并发隔离/kill/协议路由/重启复杂度；跨 run 靠持久化 threadId `thread/resume`）：
  1. 启动 app-server，握手（`HandshakeTimeout`）。
  2. `thread/resume`（带 `threadId`/`cwd`/`model`/`developerInstructions`）恢复线程；可恢复协议错误（unknown thread/schema 漂移）回退 `thread/start` 开新线程；传输/进程错误 fail-fast（不回退，app-server 已不可用）。
  3. `turn/start` 发本轮实际 prompt（= 本轮指令 + 增量历史）。
  4. drain 流，捕获 `threadId` 作为 codex 的 session_id 等价物存 `agent_sessions`。
- **runner 分流**：依 `agent_sessions.backend` 决定走 claude flag 路径还是 codex app-server 路径；两者产出的 session_id/thread_id 统一存 `session_id` 列。

## 续跑时的 prompt 语义（供 graceful-restart M2.5 复用）

被重启打断后的 resume 续跑（[platform-graceful-restart] M2.5）**重发原始任务 prompt**（重新 `build_cli_prompt`）+ resume 指针，**不喂空、不造「继续」指令**。靠 resume 恢复记忆 + prompt 约束（「聚焦本轮、只做一次」）防重复。**resume 确认未落地时**，prompt 前置一段「上轮会话未能恢复，这是新会话，请如实告知用户」的连续性披露，不静默从头再来。

## 落地技术细节

- **T1 建表**：`agent_sessions`，唯一键 `(conversation_id, agent_slug)`；`database.py` 建表 + 迁移（存量无行，首次执行自然走全量分支）。加同 (task,agent) pending 唯一约束（决策 1 串行）。
- **T2 claude 抓取**：`claude_code.py::_parse_line` 提 session_id；`ExecEvent` 增字段承载；`runner.py` 流中途 pin + 收尾覆盖存（COALESCE）。
- **T3 增量取历史**：`runner.py` 执行前查 `agent_sessions`，决定「全量首建 / 增量 + resume」；增量 SQL 参数化 `id > committed_msg_id AND id <= planned_through_msg_id AND author != 本agent`；`planned_through_msg_id`（落 task_runs）= prompt-build 时 `MAX(messages.id)`；`committed_msg_id`（session）只在成功收尾推进。
- **T4 claude resume**：命中 session 时 `args += ["--resume", sid]`；`resolveSessionID` 落地判定；未落地 → 清 sid + 本次降级全量。
- **T5 codex app-server**：`codex.py` 从 `exec` 一次性改为 `app-server --listen stdio://` + `thread/resume`(回退 `thread/start`) + `turn/start`；握手超时、进程组隔离、`threadId` 捕获。**改动最大的一处**。
- **T6 poisoned 分类**：定义 poisoned 失败集（iteration_limit / api 400 / codex 语义静默）；命中 → 丢 session、不 resume。
- **T7 串行约束**：同 (conversation, agent) 至多一个 pending/running run；重复触发折叠进下一轮（复用现有自触发守卫思路）。

## 里程碑

| 里程碑 | 交付 | 价值 | 验证 |
|--------|------|------|------|
| **S1** claude session 建立 | 建表 + 折叠模型 + 抓 session_id（pin + COALESCE 覆盖）；首次全量 | session 被正确建立/存储/可查 | 探针:首次执行后 `agent_sessions` 有正确 session_id + committed_msg_id；同 (task,agent) 不并行起两条（折叠不丢） |
| **S2** claude resume + 增量回灌 | `--resume` + committed/planned 两阶段水位增量（排除自产）；mismatch 落地判定（失败才判） | **省 token + 上下文连贯 + 并发不漏话 + 崩溃不漏（at-least-once）** | 探针:二次执行带 resume、prompt 只含增量、并发场景 B 在 A 跑期间的发言下次不漏、prompt 构建后崩溃 committed 不提前推进；token 对比下降 |
| **S3** 降级链 + poisoned 丢 session | 首次/resume 失败/provider/workdir 回退全量；poisoned 丢 session | **不劣于现状 + 不重放坏状态** | 探针:模拟 "no conversation found"/换 provider/poisoned 失败，均正确回退/丢弃、不报错 |
| **S4** codex app-server 集成（必选） | `codex exec`→`app-server` + `thread/resume`（回退 `thread/start`）+ `turn/start`；threadId 存库 | **codex 同享 resume（重度使用必选）** | 探针:codex 二次执行 thread/resume 续接、协议错误回退 thread/start、降级链生效 |

- 顺序：S1 → S2 → S3（claude 线）；S4（codex 线）可与 claude 线并行，但依赖 S1 的表结构与降级链框架。
- 回退：任一阶段遇阻可停在「全量回灌」(现状)，已达成阶段不受影响。

## 非目标

- 不改多 Agent 协同/@mention 调度模型（session 是执行层复用，不动调度决策）。
- 不做跨 task 的 session 共享（session 严格绑 conversation_id）。
- 不做「Agent 自读历史命令」（决策 3 已说明有意偏离，保留平台增量喂）。
- 不追求副作用 exactly-once（靠 prompt 约束 + session 记忆 + 任务级去重，不做服务端精确幂等）。
- 不追求「零全量」——降级链保底永远可全量，健壮性优先。
