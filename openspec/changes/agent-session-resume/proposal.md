## Why

现状：每次 Agent 执行（一次 @ 分派）都**独立建 CLI 会话**,靠把**整个 task 会话历史全量回灌**成一段大 prompt 喂给 CLI 来恢复上下文（`runner.py` + `build_cli_prompt`,`_clip_history` 双限裁剪）。三个真实痛点：

1. **Token 浪费**：每次执行把历史整段重喂,长对话（如 task140 数十轮）token 成本高、且 `_clip_history` 到上限就裁剪,**丢早期上下文**。
2. **上下文不连贯**：CLI 每次「重新读一遍历史文本」,不如它自己维护的连续会话记忆(claude/codex 的 session)。
3. **失去 CLI 原生续接能力**：claude/codex 本身支持 `--resume <session_id>`（用 session_id + resume 让 Agent 从上次上下文续跑），我们完全没用。

**成熟实践参照**：业界成熟的多 Agent 平台普遍采用「每个 (task × agent) 维护一条 session，下轮任务用 DB 里存的 `session_id + workdir` 让 CLI `--resume` 续」。这既省 token 又让上下文连贯，还顺带成为「任务被打断后可续」的地基。

**本 change 独立价值**：接入 per-agent session + resume 是**独立于平滑重启的纯优化**（省 token、上下文连贯）。它同时为 [platform-graceful-restart] 的「静默重启 + resume 续」路线提供地基（见该 change M2.5）。

## What Changes

> 规划态,claude 与 codex 均必选（claude 改动小、codex 需 app-server 集成，两线并行）。**本 change 暂不改代码。**

- **per-(task, agent) session + 串行**：每个 `(conversation_id, agent_slug)` 维护一条 CLI session。新增轻表 `agent_sessions(conversation_id, agent_slug, session_id, snapshot_msg_id, provider_id, backend, workdir, updated_at)`,唯一键 `(conversation_id, agent_slug)`。同 (task, agent) **至多一个 queued/running run**（partial unique index），重复触发折叠进下一轮——既防重复并行做工，又给增量边界确定的推进点。
- **session_id 抓取**：claude 从 `system`/`result` 行提取 `session_id`（现被忽略）;codex 从 app-server 的 `threadId` 提取。均写回 `agent_sessions`。
- **增量回灌（快照水位 + 排除自产）**：执行前查该 (task, agent) 的 `session_id` + `snapshot_msg_id`;增量 = `messages WHERE conversation_id=? AND id > snapshot_msg_id AND 非本 agent 自产`（别人/人工在上一快照后说的话；本 Agent 自己的历史由 CLI session 记忆承载不重喂）。prompt 只喂增量,CLI 带 resume。`snapshot_msg_id` = 构建本次 prompt 那刻的 `MAX(messages.id)`。
- **session_id 生命周期（三件套）**：每轮覆盖存最新（resume 后 id 可能变）+ COALESCE 空值保护（没抓到不清旧指针）+ 流中途 pin 抢先落库（防崩溃丢指针）+ resume 落地判定（"no conversation found"/emitted id≠请求 id → 判未落地重建）。
- **降级链（健壮性）**：首次执行(无 session)→ 全量回灌(现状);resume 未落地/跨 provider/跨 workdir → 回退全量 + 新建;**poisoned 失败（迭代上限/api 400/codex 语义静默）→ 主动丢弃 prior session**（不 resume 坏状态）。**任何降级都不劣于现状。**
- **增量水位修正并发漏话**：不用「完成时 MAX(id)」（并发下会把执行期间别人的发言算进水位而漏话），改用「prompt-build 快照水位 + 排除自产」——A 执行期间 B 写的消息下次触发 A 时被正确纳入。
- **backend：claude 与 codex 均必选**（用户重度使用 codex）。claude 用 `-p --resume <sid>`（改动小）;codex 需从一次性 `codex exec` 改为 **`codex app-server --listen stdio://` + JSON-RPC `thread/resume`**（回退 `thread/start`）——集成模式变更，改动大但必做。

## Capabilities

### New Capabilities
- `agent-session-resume`: Agent 执行的 CLI 会话复用能力——每个 (task, agent) 维护一条 CLI session,再次执行时 resume 续接 + 只喂增量上下文,替代每次全量回灌;含首次/失败/provider 变更的降级链,保证不劣于现状。

## Impact

- **规划态,暂不改代码。** 落实时预计涉及：`executor/base.py`(`build_cli_prompt` 支持增量模式)、`executor/claude_code.py`(`--resume` 参数 + session_id 提取)、`executor/runner.py`(查/写 `agent_sessions`、增量取历史)、`database.py`(建 `agent_sessions` 表)。
- **关联能力**：[agent-collaboration](多 Agent 协同/会话)、[agent-execution](执行)、[platform-graceful-restart](其 M2.5「静默+resume」依赖本 change 出的 resume 地基)。
- **不破坏多 Agent 协同**：每个 Agent 独立 session,互不干扰;别人的发言经「增量回灌」补给,语义正确。
- **模型适配**：业界常见 session 模型是「1 issue × 1 agent = 1 session」,我们是「多 Agent 在一个 conversation 里 @ 来 @ 去」,故需 per-(task, agent) 粒度 + 增量回灌来适配,而非直接套用单会话。
