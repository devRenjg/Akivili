## Why

实际使用中，CLI Agent（Claude / Codex）的会话正文里混进了大量**命令过程碎语**——例如「`jian` 命令是通过 `jian.bat` 调用的，用 `py -3.12` 直接跑 jian.py 即可」「设 `PYTHONUTF8=1`」「连通正常（roster 已取到数据，仅打印时 GBK 编码崩）」「结尾那句是终端中文编码显示问题，实际已发送成功」。这些是 Agent「边干边碎念」的执行过程，放在正文里干扰阅读，用户希望正文只保留正常的问答与结论。

**根因**：`runner.execute_dispatch` 把后端吐出的**流式 stdout 全文**（`final_text`）无条件落成一条 assistant 会话消息展示。但 CLI Agent 的**真实交付**走的是 `jian comment` / `jian subtask`（已单独落库、干净）——于是同一 Agent 的产出被记两遍：一遍干净结论、一遍夹带命令细节的过程碎念。

线上数据印证（任务 53/55，克里珀项目）：每条噪声消息的长度与对应 run 的 stdout 落库长度**逐条精确相等**，而干净交付（团队介绍 #136/#161、卡芙卡自我介绍 #144）均来自 `jian comment`。

`_persist_memory` 早已确立正确判别原则——「jian comment 发言＝真实产出 ＞ stdout 兜底」；但**展示落库**这条路径没遵守同一原则。

## What Changes

- **CLI 后端（claude-cli / codex-cli）的流式 stdout 不再落成会话正文消息**。stdout 仍全量进 `run_logs`（日志详情可逐条排查），只是不进会话 Thread。Agent 的真实交付经 `jian comment` / `jian subtask` 落库、正常展示。
- **API 后端不变**：它没有 jian CLI 通道，stdout `final_text` **就是** Agent 的唯一产出，仍照常落成 assistant 会话消息展示。
- **收工写记忆的 stdout 兜底不受影响**：`_RUN_CTX[run_id]["stream_text"]` 仍被填充，`_persist_memory` 仍在「无 jian comment 发言」时回退到 stdout。
- **CLI run 未走 jian 交付时打醒目标记（不拿 stdout 错误兜底）**：目标是让 `jian comment` 100% 出现，而**非**用可能错误的 stdout 结论兜底（那没有意义）。若某 CLI run 成功结束却没有任何 jian 平台动作（`jian comment` 落本会话消息、或 `jian subtask`/`jian status` 记本人活动），落一条 `⚠️ …未通过 jian comment/subtask 提交交付…` 系统活动，便于发现并追查（完整 stdout 仍在 run_logs / 日志详情里）。

设计取舍：若某个 CLI run 里 Agent 只在 stdout 说了结论、忘了走 `jian`，该 run 在会话正文里没有痕迹，但会有上述醒目标记 + 活动时间线 task_started/completed + run_logs 完整可查。系统提示已强制要求真实交付必须走 `jian`。

## Capabilities

### Modified Capabilities
- `agent-execution`：明确「会话正文来源」——CLI 后端的流式 stdout 仅入 run_logs、不作会话消息；真实交付经 jian comment；API 后端 stdout 即产出、照常落库展示。CLI run 未产出 jian 交付时打醒目标记而非拿 stdout 兜底。

## Impact

- 后端：`backend/executor/runner.py::execute_dispatch`——① final_text 落库处按 `provider.type` 分流（CLI 不落会话消息、API 落）；② 新增 `_has_jian_deliverable`（查本轮 jian comment 消息 / subtask/status 活动），CLI 成功但无交付则 `log_activity` 打警告标记。`_persist_memory` / `finalize_run` / collab @mention 解析（读自身 collected，不依赖会话消息）均不受影响。
- 前端：无改动（标记走既有 `commented` 活动渲染路径）。
- 数据：无迁移。历史噪声消息（线上克里珀项目 task53/55/56/57/58/59 的 9 条 stdout-mirror）已按精确 msg id 清理（删前备份 `jianagency.db.bak_before_stdout_noise_cleanup_20260707`；每条删前校验同会话同 agent 另有 jian 交付，正文不空）。
- 验证：`TestReport/run_stdout_display_probe.py` 8/8（CLI stdout 不落正文但进日志 / 无 jian 打标记 / jian 交付保留且不打标记 / API stdout 照落且不打标记）；QA 套件 28/30、并发探针、子任务探针与改动前基线**逐项一致**（既有 2 项协同排序失败为测试 harness 漂移、与本改动无关）。
