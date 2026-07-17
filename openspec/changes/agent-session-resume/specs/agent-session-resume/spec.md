# agent-session-resume (delta)

## ADDED Requirements

### Requirement: per-(task, agent) CLI 会话复用与串行

系统 SHALL 为每个 `(conversation_id, agent_slug)` 维护一条 CLI session,持久化于 `agent_sessions` 表（唯一键 `(conversation_id, agent_slug)`）。同一 Agent 在同一 task 里再次被触发执行时,系统 SHALL 复用该 session（CLI resume）,而非每次新建会话。session 记录 SHALL 含 `session_id`、`committed_msg_id`（上次成功执行确认的增量水位）、`provider_id`、`backend`、`workdir`;本次执行的快照终点 `planned_through_msg_id` SHALL 落在 `task_runs` 行。粒度 SHALL 为 (conversation, agent)——同一 task 内每个成员各持独立 session,互不干扰。系统 SHALL 保证同一 `(task, agent)` 至多一个 active run（active = queued/claimed/running）,重复触发 SHALL **折叠**进下一轮（合并触发意图，不丢弃），而非并行起第二条。DB 级唯一性 SHALL 用 `partial unique index ON run_queue(task_id, agent_slug) WHERE status IN ('queued','claimed','running')` 保证（应用层查重有 TOCTOU 竞态，不足以防两个并发 POST 各插一条）;折叠 SHALL 用「queued 原子合并水位 / claimed·running 记 pending intent、收尾据 pending 建至多一个 successor」实现。全局并发上限约束的是不同 (task, agent) 的并行度,与此串行正交。

#### Scenario: 首次执行建立 session
- **WHEN** 某 Agent 在某 task 首次被触发执行（`agent_sessions` 无该 (conversation, agent) 行）
- **THEN** 系统开新 CLI 会话（不带 resume）、执行成功后把 CLI 返回的 `session_id`、`provider_id`、`backend`、`workdir` 落 `agent_sessions`，并把本次成功喂到的水位作为 `committed_msg_id` 落库

#### Scenario: 再次执行复用 session
- **WHEN** 同一 (conversation, agent) 再次被触发,且已有 session 且 backend/provider/workdir 未变
- **THEN** 系统以 resume 启动 CLI,复用上次会话上下文,不新建会话

#### Scenario: 同 (task, agent) 串行
- **WHEN** 某 (task, agent) 已有 active run（queued/claimed/running），此时该成员在同一 task 被再次触发
- **THEN** partial unique index 挡下并发插入的第二条 run，触发被折叠进下一轮（queued 合并水位 / claimed·running 记 pending intent，收尾据 pending 建至多一个 successor），保证增量边界有确定推进点

#### Scenario: 会话粒度隔离
- **WHEN** 同一 task 内两个不同成员各自被触发执行
- **THEN** 两者各用自己 (conversation, agent) 的 session,互不串上下文,且不同成员照旧可并行

### Requirement: session_id 抓取与生命周期

系统 SHALL 从 CLI 输出中提取会话标识并按下述策略维护其生命周期。claude backend SHALL 从 `-p --output-format stream-json` 的 `system`(init)/`result` 行提取 `session_id`;codex backend SHALL 从 app-server 的 `threadId` 提取。生命周期 SHALL 满足：① **流中途首次见到 id 即抢先落库（pin）**,防执行中途崩溃丢指针;② **收尾以本次最新 id 覆盖存**（resume 后 id 可能变,非只存首次）;③ 更新用 **COALESCE 空值保护**（本次没抓到 id 时不清空旧指针）;④ **pin 与覆盖 SHALL 带 CAS 条件（Review P1-3）**：`agent_sessions` SHALL 含 `session_version` 与 `current_task_run_id` 字段，pin/覆盖用 `WHERE current_task_run_id=? AND session_version=?`（写入者仍是当前活跃 attempt/版本才更新），成功后 `session_version` 自增、`current_task_run_id` 指向本 attempt;仅当写入者仍是当前活跃 attempt 时才更新，防上一轮迟到的流事件覆盖下一轮已建立的新 session pointer。

#### Scenario: 流中途 pin 落库
- **WHEN** CLI 流中第一次出现 session_id,该 run 尚未收尾
- **THEN** 系统立即把该 session_id 落库一次,使执行中途崩溃时仍有可用 resume 指针（供平滑重启续跑）

#### Scenario: 每轮覆盖存最新
- **WHEN** 一次 resume 执行成功,CLI 返回的 session_id 与上次不同
- **THEN** 系统以本次最新 session_id 覆盖存储,不保留过期的旧 id

#### Scenario: 空值不清指针
- **WHEN** 某次执行未能从输出中抓到 session_id
- **THEN** 系统用 `COALESCE(?, session_id)` 更新,保留上一次的有效指针,不清空

#### Scenario: 迟到流事件不覆盖新 session
- **WHEN** 上一轮 run 的流事件迟到到达，此时该 (task,agent) 已由新一轮 run（新 task_run_id/generation）建立了新的 session pointer
- **THEN** CAS 条件（task_run_id/generation 不匹配）拒绝迟到写入，新 session pointer 不被旧事件覆盖

### Requirement: resume 落地判定与失效降级（不劣于现状）

系统 SHALL 判定 resume 是否真正落地,并在 session 不可用的任何情形下回退到「全量回灌 + 新建会话」,保证行为不劣于改造前。落地判定 SHALL 仅在**执行失败**时把 mismatch 判为未落地：`失败 && (CLI 报会话不存在如 "no conversation found" 或 输出 session_id 与请求不一致)` → 判 resume 未落地。**执行成功但输出 session_id 与请求不一致 SHALL 视为正常**（resume 后 CLI 可能 fork 新 session id），系统 SHALL 接受并覆盖保存新 id，SHALL NOT 判为失败。降级情形 SHALL 覆盖：resume 未落地、provider/backend 变更、workdir 变更、（未来多机）runtime 不匹配。降级 SHALL 对用户无感（自动重开会话、执行正常完成）。

#### Scenario: 失败且 mismatch 才判未落地并回退
- **WHEN** 以 resume 启动 CLI，执行**失败**且 CLI 报会话不存在（或输出 session_id 与请求不一致）
- **THEN** 系统判定 resume 未落地、清除失效 session_id、本次降级为全量回灌 + 新建会话,执行正常完成,新 session_id 落库供后续复用

#### Scenario: 成功返回新 session_id 则接受覆盖存
- **WHEN** 以 resume 启动 CLI，执行**成功**但输出的 session_id 与请求的不一致（CLI fork 了新会话）
- **THEN** 系统视为正常，接受并以新 session_id 覆盖保存，SHALL NOT 判为失败或降级

#### Scenario: provider/workdir 变更弃旧会话
- **WHEN** 某 (conversation, agent) 已有 session,但当前执行的 `provider_id`/`backend`/`workdir` 与存储值不一致
- **THEN** 系统弃用旧 session、以新配置开新会话 + 全量回灌,不误用不兼容的 session

### Requirement: poisoned 失败主动丢弃会话

系统 SHALL 识别会污染会话状态的失败类型（poisoned），命中时**主动丢弃 prior session**,使下次执行从头重建而非 resume 坏状态。poisoned 失败集 SHALL 至少包含：迭代上限耗尽（`iteration_limit`）、模型请求非法（`api_invalid_request` / 400 类）、codex 语义静默超时（`codex_semantic_inactivity`）。非 poisoned 的基础设施型失败（如被平滑重启中断）SHALL 保留 session 供 resume 续跑。

#### Scenario: poisoned 失败丢 session
- **WHEN** 某次执行以 poisoned 原因失败（迭代上限 / 模型 400 / codex 语义静默）
- **THEN** 系统丢弃该 (conversation, agent) 的 prior session,下次执行从头重建,不 resume 已污染的会话状态

#### Scenario: 基础设施失败保留 session
- **WHEN** 某执行因基础设施原因（如平滑重启中断）失败,非 poisoned
- **THEN** 系统保留 session_id,供后续 resume 续跑

### Requirement: 增量上下文回灌（快照水位 + 排除自产）

系统 SHALL 在复用 session 执行时只回灌「增量上下文」,而非全量历史。增量水位 SHALL 用 **2 字段两阶段**：`agent_sessions.committed_msg_id`（上次**成功**执行确认的水位，**只有 run 成功收尾才推进**）+ `task_runs.planned_through_msg_id`（本次构建 prompt 那刻的快照终点 = 当时 `MAX(messages.id)`）。系统 **SHALL NOT** 用「执行完成时的 MAX(id)」做单一水位（并发下会把执行期间别人新写的消息算进而漏话）。增量 = `messages WHERE conversation_id=? AND id > committed_msg_id AND id <= planned_through_msg_id AND 作者非本 agent`（参数化）：别人/人工在上次成功水位之后、本次快照之前说的话喂给本 Agent,本 Agent 自己的历史发言由 CLI session 记忆承载、不重喂。**不变量 = at-least-once**：崩溃/中断时 committed 未推进 → 续跑从同一起点重取 → 重复但不漏;prompt 构建后、CLI 接收前崩溃时 committed **SHALL NOT** 提前推进。**单次海量增量 SHALL 用连续前缀分批，SHALL NOT 用「保新丢旧」裁剪**：现状历史裁剪按保留最新、丢弃较早裁剪，若套在增量上会丢中段较早消息而 committed 仍推进到 planned_through → 中段永久漏喂，违反 at-least-once。增量 SHALL 从 committed 之后最旧一条起、按连续前缀截取本轮可喂量，`committed_msg_id` 收尾时 **SHALL 只推进到本轮实际连续喂达的最后一条 id（committed_batch_end），SHALL NOT 直接推进到 planned_through**;剩余尾部留到下一轮从新 committed 起点续喂。超上限 SHALL 在连续前缀处截断、分多轮喂，**SHALL NOT 跳段**。「连续前缀」SHALL 指所有 eligible message（排除本 agent 自产后）的连续扫描区间，**不要求原始消息 ID 无空洞**（自产消息造成的 ID 空洞不算跳段）;单条消息本身超字符预算时 SHALL 至少完整投递该条，避免永远无法推进。**剩余尾部 SHALL 自动续批**：本轮 run 成功且 `committed_batch_end < planned_through_msg_id` 时，系统 SHALL 在**同一事务**内（连同 committed 推进、session pointer 更新）创建一个 `history_backlog` successor run 继续消费尾部（携带 `trigger=history_backlog`/`history_batch_no`/`history_batch_end`/`history_backlog_from_execution_id`），SHALL NOT 依赖新的用户 @ 才继续。backlog successor SHALL 不计 mention-chain、受独立最大批次数/token 预算限制、复用同一 CLI session;所有批次消费完才清除 backlog 标记;达批次上限仍未消费完 SHALL 进人工提示而非无限自动跑。首次执行（无 session）SHALL 回灌全量历史（现状行为，同样连续前缀分批 + 自动续批、超上限分轮不丢段不空转）。

#### Scenario: 复用执行只喂增量
- **WHEN** 某 (conversation, agent) 带 session resume 执行,其间别人新增了若干条 messages
- **THEN** 系统只把 `id > committed_msg_id AND id <= planned_through_msg_id` 且非本 agent 自产的新增 messages 拼进 prompt,不重复喂更早历史,也不重喂本 Agent 自己的旧发言

#### Scenario: 并发不漏话
- **WHEN** Agent A 执行期间,Agent B 在同一 conversation 写了发言（其 id 大于 A 本次的 planned_through_msg_id）
- **THEN** B 的发言不在 A 本次增量内，但因 A 的 committed 只推进到本次成功水位（仍在 B 之前）,B 的发言在下次触发 A 时被正确纳入增量,不漏

#### Scenario: 崩溃不漏（at-least-once）
- **WHEN** A 的 prompt 构建后、run 未成功收尾即崩溃/被中断（committed_msg_id 未推进）
- **THEN** 续跑从同一 committed_msg_id 起点重取增量，已喂的消息重复喂但不漏，满足 at-least-once

#### Scenario: 增量为空
- **WHEN** 自上次快照后无他人新 messages（增量为空）
- **THEN** prompt 仅含本轮指令,不含历史片段

#### Scenario: 海量增量连续前缀分批不跳段
- **WHEN** 增量消息条数/字符超单轮上限
- **THEN** 系统从 committed 之后最旧一条起按连续前缀截取本轮可喂量、在上限处截断（committed_batch_end），committed_msg_id 只推进到 committed_batch_end 而非 planned_through，剩余尾部下一轮从新起点续喂，中间不跳过任何应喂消息

#### Scenario: 无新触发也自动续批消费尾部
- **WHEN** 海量增量需分 3 批消费，且期间没有新的用户 @ 触发
- **THEN** 本轮成功后系统在同事务内自动创建 `history_backlog` successor 继续下一批，逐轮推进 committed，直至全部批次消费完才清 backlog；不依赖新触发、不空转在半消费状态

#### Scenario: 单条超大消息不卡死
- **WHEN** 某单条消息本身超过字符预算
- **THEN** 系统至少完整投递该条（必要时该轮只喂这一条），committed 得以越过它继续推进，不永久卡住

#### Scenario: 达批次上限转人工
- **WHEN** backlog 自动续批达到独立设定的最大批次数/预算仍未消费完
- **THEN** 系统停止自动续批、转人工提示，不无限自动跑下去

#### Scenario: 首次执行全量回灌
- **WHEN** 某 (conversation, agent) 首次执行（无 session）
- **THEN** 系统回灌全量历史（与改造前一致），不因缺 session 而丢上下文

### Requirement: backend 分流（claude 与 codex 均必选）

系统 SHALL 支持 claude 与 codex 两个 backend 的 session 复用,均为必选（用户重度使用 codex）。claude backend SHALL 用 `-p --resume <session_id>` flag。codex backend SHALL **每次执行 attempt 启动一个** `codex app-server --listen stdio://` 进程（run 结束即关，**非** Worker 全局共享一个 app-server）+ JSON-RPC `thread/resume`（不可恢复时回退 `thread/start`,传输/进程错误 fail-fast）+ `turn/start`,以 `threadId` 作为其 session 标识。codex `thread/resume` 前系统 SHALL 检查 `CODEX_HOME` 下对应 rollout/thread 记录存在性与 workdir 一致性——记录不存在或 workdir 不一致时 SHALL 降级 `thread/start` 全量、不 resume 到不存在/错配的线程。runner SHALL 依 `agent_sessions.backend` 分流到对应实现;两 backend 共用同一 `agent_sessions` 表、降级链与 poisoned 分类。

#### Scenario: claude 走 flag resume
- **WHEN** 执行 backend 为 claude 且命中可用 session
- **THEN** 走 `--resume <session_id>` + 增量回灌

#### Scenario: codex 走 app-server thread resume
- **WHEN** 执行 backend 为 codex 且命中可用 session（threadId）
- **THEN** 系统先校验 `CODEX_HOME` rollout 存在且 workdir 一致，再经 app-server `thread/resume` 恢复线程续接;线程不可恢复（unknown thread/schema 漂移）时回退 `thread/start` 开新线程并如实标记为新会话

#### Scenario: codex rollout 不存在则降级
- **WHEN** codex 命中 session（threadId）但 `CODEX_HOME` 下对应 rollout 记录不存在或 workdir 不一致
- **THEN** 系统不 resume 到该线程，降级 `thread/start` 全量新建，如实标记为新会话

#### Scenario: codex 传输错误 fail-fast
- **WHEN** codex app-server 出现传输/进程级错误（非协议可恢复错误）
- **THEN** 系统 fail-fast 不回退（app-server 已不可应答）,该 run 按失败处置,下次重建
