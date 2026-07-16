# agent-session-resume (delta)

## ADDED Requirements

### Requirement: per-(task, agent) CLI 会话复用与串行

系统 SHALL 为每个 `(conversation_id, agent_slug)` 维护一条 CLI session,持久化于 `agent_sessions` 表（唯一键 `(conversation_id, agent_slug)`）。同一 Agent 在同一 task 里再次被触发执行时,系统 SHALL 复用该 session（CLI resume）,而非每次新建会话。session 记录 SHALL 含 `session_id`、`snapshot_msg_id`（增量快照水位）、`provider_id`、`backend`、`workdir`。粒度 SHALL 为 (conversation, agent)——同一 task 内每个成员各持独立 session,互不干扰。系统 SHALL 保证同一 `(conversation, agent)` 至多一个 queued/running run（串行）,重复触发折叠进下一轮,而非并行起第二条;全局并发上限约束的是不同 (task, agent) 的并行度,与此串行正交。

#### Scenario: 首次执行建立 session
- **WHEN** 某 Agent 在某 task 首次被触发执行（`agent_sessions` 无该 (conversation, agent) 行）
- **THEN** 系统开新 CLI 会话（不带 resume）、执行成功后把 CLI 返回的 `session_id`、`provider_id`、`backend`、`workdir`、构建 prompt 时的 `MAX(messages.id)` 作为 `snapshot_msg_id` 落库

#### Scenario: 再次执行复用 session
- **WHEN** 同一 (conversation, agent) 再次被触发,且已有 session 且 backend/provider/workdir 未变
- **THEN** 系统以 resume 启动 CLI,复用上次会话上下文,不新建会话

#### Scenario: 同 (task, agent) 串行
- **WHEN** 某 (conversation, agent) 已有 queued/running run,此时该成员在同一 task 被再次触发
- **THEN** 系统不并行起第二条 run,把新触发折叠进下一轮,保证增量边界有确定推进点

#### Scenario: 会话粒度隔离
- **WHEN** 同一 task 内两个不同成员各自被触发执行
- **THEN** 两者各用自己 (conversation, agent) 的 session,互不串上下文,且不同成员照旧可并行

### Requirement: session_id 抓取与生命周期

系统 SHALL 从 CLI 输出中提取会话标识并按下述策略维护其生命周期。claude backend SHALL 从 `-p --output-format stream-json` 的 `system`(init)/`result` 行提取 `session_id`;codex backend SHALL 从 app-server 的 `threadId` 提取。生命周期 SHALL 满足：① **流中途首次见到 id 即抢先落库（pin）**,防执行中途崩溃丢指针;② **收尾以本次最新 id 覆盖存**（resume 后 id 可能变,非只存首次）;③ 更新用 **COALESCE 空值保护**（本次没抓到 id 时不清空旧指针）。

#### Scenario: 流中途 pin 落库
- **WHEN** CLI 流中第一次出现 session_id,该 run 尚未收尾
- **THEN** 系统立即把该 session_id 落库一次,使执行中途崩溃时仍有可用 resume 指针（供平滑重启续跑）

#### Scenario: 每轮覆盖存最新
- **WHEN** 一次 resume 执行成功,CLI 返回的 session_id 与上次不同
- **THEN** 系统以本次最新 session_id 覆盖存储,不保留过期的旧 id

#### Scenario: 空值不清指针
- **WHEN** 某次执行未能从输出中抓到 session_id
- **THEN** 系统用 `COALESCE(?, session_id)` 更新,保留上一次的有效指针,不清空

### Requirement: resume 落地判定与失效降级（不劣于现状）

系统 SHALL 判定 resume 是否真正落地,并在 session 不可用的任何情形下回退到「全量回灌 + 新建会话」,保证行为不劣于改造前。判定 SHALL 覆盖：请求了 resume 但 CLI 报会话不存在（如 "no conversation found"）、或输出的 session_id 与请求的不一致 → 判 resume 未落地。降级情形 SHALL 覆盖：resume 未落地、provider/backend 变更、workdir 变更、（未来多机）runtime 不匹配。降级 SHALL 对用户无感（自动重开会话、执行正常完成）。

#### Scenario: resume 未落地自动回退
- **WHEN** 以 resume 启动 CLI,CLI 报会话不存在,或输出 session_id 与请求的不一致
- **THEN** 系统判定 resume 未落地、清除失效 session_id、本次降级为全量回灌 + 新建会话,执行正常完成,新 session_id 落库供后续复用

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

系统 SHALL 在复用 session 执行时只回灌「增量上下文」,而非全量历史。增量水位 SHALL 用 **prompt-build 快照水位** `snapshot_msg_id`（= 构建本次 prompt 那刻该 conversation 的 `MAX(messages.id)`）,而 **SHALL NOT** 用「执行完成时的 MAX(id)」——后者在并发下会把执行期间别人新写的消息算进水位而导致漏话。增量 = `messages WHERE conversation_id=? AND id > snapshot_msg_id AND 作者非本 agent`（参数化 `id > ?`）：别人/人工在上一快照后说的话喂给本 Agent,本 Agent 自己的历史发言由 CLI session 记忆承载、不重喂。增量仍 SHALL 过历史裁剪防单次海量。首次执行（无 session）SHALL 回灌全量历史（现状行为）。

#### Scenario: 复用执行只喂增量
- **WHEN** 某 (conversation, agent) 带 session resume 执行,其间别人新增了若干条 messages
- **THEN** 系统只把 `id > snapshot_msg_id` 且非本 agent 自产的新增 messages 拼进 prompt,不重复喂更早历史,也不重喂本 Agent 自己的旧发言

#### Scenario: 并发不漏话
- **WHEN** Agent A 执行期间,Agent B 在同一 conversation 写了发言（其 id 大于 A 本次的 snapshot_msg_id）
- **THEN** A 本次水位只推进到 A 自己的快照,B 的发言在下次触发 A 时被正确纳入增量,不漏

#### Scenario: 增量为空
- **WHEN** 自上次快照后无他人新 messages（增量为空）
- **THEN** prompt 仅含本轮指令,不含历史片段

#### Scenario: 首次执行全量回灌
- **WHEN** 某 (conversation, agent) 首次执行（无 session）
- **THEN** 系统回灌全量历史（与改造前一致），不因缺 session 而丢上下文

### Requirement: backend 分流（claude 与 codex 均必选）

系统 SHALL 支持 claude 与 codex 两个 backend 的 session 复用,均为必选（用户重度使用 codex）。claude backend SHALL 用 `-p --resume <session_id>` flag。codex backend SHALL 改用 `codex app-server --listen stdio://` 长驻进程 + JSON-RPC `thread/resume`（不可恢复时回退 `thread/start`,传输/进程错误 fail-fast）+ `turn/start`,以 `threadId` 作为其 session 标识。runner SHALL 依 `agent_sessions.backend` 分流到对应实现;两 backend 共用同一 `agent_sessions` 表、降级链与 poisoned 分类。

#### Scenario: claude 走 flag resume
- **WHEN** 执行 backend 为 claude 且命中可用 session
- **THEN** 走 `--resume <session_id>` + 增量回灌

#### Scenario: codex 走 app-server thread resume
- **WHEN** 执行 backend 为 codex 且命中可用 session（threadId）
- **THEN** 系统经 app-server `thread/resume` 恢复线程续接;线程不可恢复（unknown thread/schema 漂移）时回退 `thread/start` 开新线程并如实标记为新会话

#### Scenario: codex 传输错误 fail-fast
- **WHEN** codex app-server 出现传输/进程级错误（非协议可恢复错误）
- **THEN** 系统 fail-fast 不回退（app-server 已不可应答）,该 run 按失败处置,下次重建
