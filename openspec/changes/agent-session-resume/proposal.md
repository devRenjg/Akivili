## Why

现状：每次 Agent 执行（一次 @ 分派）都**独立建 CLI 会话**,靠把**整个 task 会话历史全量回灌**成一段大 prompt 喂给 CLI 来恢复上下文（`runner.py` + `build_cli_prompt`,`_clip_history` 双限裁剪）。三个真实痛点：

1. **Token 浪费**：每次执行把历史整段重喂,长对话（如 task140 数十轮）token 成本高、且 `_clip_history` 到上限就裁剪,**丢早期上下文**。
2. **上下文不连贯**：CLI 每次「重新读一遍历史文本」,不如它自己维护的连续会话记忆(claude/codex 的 session)。
3. **失去 CLI 原生续接能力**：claude/codex 本身支持 `--resume <session_id>`（用 session_id + resume 让 Agent 从上次上下文续跑），我们完全没用。

**成熟实践参照**：业界成熟的多 Agent 平台普遍采用「每个 (task × agent) 维护一条 session，下轮任务用 DB 里存的 `session_id + workdir` 让 CLI `--resume` 续」。这既省 token 又让上下文连贯，还顺带成为「任务被打断后可续」的地基。

**本 change 独立价值**：接入 per-agent session + resume 是**独立于平滑重启的纯优化**（省 token、上下文连贯）。它同时为 [platform-graceful-restart] 的「静默重启 + resume 续」路线提供地基（见该 change M2.5）。

## What Changes

> 规划态,claude 与 codex 均必选（claude 改动小、codex 需 app-server 集成，两线并行）。**本 change 暂不改代码。**

- **per-(conversation, agent) session + 串行折叠**：每个 `(conversation_id, agent_slug)` 维护一条 CLI session。新增轻表 `agent_sessions(conversation_id, agent_slug, session_id, committed_msg_id, provider_id, backend, workdir, updated_at, session_version, current_task_run_id)`,唯一键 `(conversation_id, agent_slug)`。同 (conversation, agent) **至多一个 active run（queued/claimed/running）**，重复触发**折叠**进下一轮（合并触发意图，**不丢**）——现状代码是 `return None` 丢弃（Review P0-3 修正）;DB 级唯一性用 `(conversation_id, agent_slug)` partial unique index `WHERE status IN ('queued','claimed','running')` 保证（第五轮 P1-5 方案 B：与 session owner 键同粒度，先给 run_queue 补 conversation_id 列;应用层查重有 TOCTOU 竞态，不足以防并发 POST，Review P0-B），配合折叠模型（queued 原子合并水位 / claimed·running 记 pending intent、收尾据 pending 建至多一个 successor）。
- **session_id 抓取**：claude 从 `system`/`result` 行提取 `session_id`（现被忽略）;codex 从 app-server 的 `threadId` 提取。均写回 `agent_sessions`。
- **增量回灌（2 字段水位，at-least-once 安全）**：`agent_sessions.committed_msg_id`（成功才推进）+ `task_runs.planned_through_msg_id`（本次 prompt 快照终点）。增量 = `messages WHERE id > committed_msg_id AND id <= planned_through_msg_id AND 非本 agent 自产`。崩溃时 committed 未推进→续跑从同一起点重取→**重复但不漏**（满足不变量）。本 Agent 自己历史由 session 记忆承载不重喂。
- **session_id 生命周期（三件套）**：每轮覆盖存最新（resume 后 id 可能变）+ COALESCE 空值保护（没抓到不清旧指针）+ 流中途 pin 抢先落库（防崩溃丢指针）+ resume 落地判定（**失败** && "no conversation found"/emitted id≠请求 id → 判未落地重建;**成功**返回新 id → 接受覆盖存，Review P1-6 修正原过严判定）。
- **降级链（健壮性）**：首次执行(无 session)→ 全量回灌(现状);resume 未落地/跨 provider/跨 workdir → 回退全量 + 新建;**poisoned 失败（迭代上限/api 400/codex 语义静默）→ 主动丢弃 prior session**（不 resume 坏状态）。**任何降级都不劣于现状。**
- **增量水位修正并发漏话**：不用「完成时 MAX(id)」（并发下会把执行期间别人的发言算进水位而漏话），改用「prompt-build 快照水位 + 排除自产」——A 执行期间 B 写的消息下次触发 A 时被正确纳入。
- **backend：claude 与 codex 均必选**（用户重度使用 codex）。claude 用 `-p --resume <sid>`（改动小）;codex 需从一次性 `codex exec` 改为 **每次执行 attempt 起一个 `codex app-server --listen stdio://` 进程 + JSON-RPC `thread/resume`**（回退 `thread/start`,run 结束即关，**非** Worker 全局共享一个 app-server）——集成模式变更，改动大但必做。

## Capabilities

### New Capabilities
- `agent-session-resume`: Agent 执行的 CLI 会话复用能力——每个 (conversation, agent) 维护一条 CLI session,再次执行时 resume 续接 + 只喂增量上下文,替代每次全量回灌;含首次/失败/provider 变更的降级链,保证不劣于现状。**所有可执行 task 必须拥有非 NULL conversation**;`conversation_id` 为空的 task（历史/系统 task 与迁移隔离的 quarantined task）**一律不可执行、不进 claim/dispatch**（第十二轮 P0-A 落为三层硬门：dispatch 拒绝入队 / scheduler 扫描排除 / claim CAS `AND conversation_id IS NOT NULL`;两组互补 active 索引里的 NULL 组仅作存量脏数据并发防御、非可执行兜底;存量 NULL 在途行 activate 前统一隔离迁移），人工恢复时先创建独立 conversation + 可审计消息切分后迁移（第八轮 P1-F + 第十一轮 P1-C：`messages` 无 task_id、`conversation_id NOT NULL`，NULL task 无法合法读写「本 task 消息」，故不设可执行的 NULL run 口径;`agent_sessions` 键无 task_id、SQLite UNIQUE 对 NULL 不提供 task 级 session 唯一性）。

## Impact

- **规划态,暂不改代码。** 落实时预计涉及：`executor/base.py`(`build_cli_prompt` 支持增量模式)、`executor/claude_code.py`(`--resume` 参数 + session_id 提取 + mismatch 判定)、`executor/codex.py`(app-server + thread/resume，每 run 一进程)、`executor/runner.py`(查/写 `agent_sessions`、committed/planned 两阶段水位、增量取历史、折叠模型)、`database.py`(建 `agent_sessions` 表——唯一键 `(conversation_id, agent_slug)`、含 `session_version`/`current_task_run_id` owner 字段 + `task_runs.planned_through_msg_id` 列 + 折叠所需 `run_queue.conversation_id` 及两组互补 active 唯一索引)。
- **关联能力**：[agent-collaboration](多 Agent 协同/会话)、[agent-execution](执行)、[platform-graceful-restart](其阶段 3/4 = 本 change;其阶段 5「温和重启+resume 续跑」依赖本 change 出的 resume 地基与流中途 pin)。
- **不破坏多 Agent 协同**：每个 Agent 独立 session,互不干扰;别人的发言经「增量回灌」补给,语义正确。
- **模型适配**：业界常见 session 模型是「1 issue × 1 agent = 1 session」,我们是「多 Agent 在一个 conversation 里 @ 来 @ 去」,故需 per-(conversation, agent) 粒度 + 增量回灌来适配,而非直接套用单会话。
