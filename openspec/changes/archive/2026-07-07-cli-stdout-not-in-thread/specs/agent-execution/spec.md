# agent-execution

## MODIFIED Requirements

### Requirement: @ 分派与真实执行

用户 SHALL 能在任务对话中 @ 负责人下达指令，触发该 Agent 真实执行；会话正文 SHALL 只呈现 Agent 的真实交付与结论，不混入执行过程碎语。

#### Scenario: 分派执行
- **WHEN** 用户在任务 Thread 中 @ 某成员并下达指令
- **THEN** 系统按该 Agent 的接入模型启动执行（CLI 在项目目录内可改文件/跑命令，API 为纯对话），流式返回过程

#### Scenario: 上下文恢复
- **WHEN** Agent 开始执行
- **THEN** 系统先读取其记忆（含工作区约束与 Skills 说明）与该任务的会话历史，组装进上下文

#### Scenario: CLI 交付经 jian、过程仅入日志
- **WHEN** CLI 后端（claude/codex）的 Agent 执行完毕，流式 stdout 里含执行过程碎语（如 jian 命令用法、环境变量、终端编码提示等）
- **THEN** 该 stdout 全量记入 run_logs（供日志详情排查），但**不**落成会话正文消息；Agent 的真实交付经 `jian comment` / `jian subtask` 单独落库并在正文展示

#### Scenario: CLI 未产出 jian 交付时打标记而非兜底
- **WHEN** CLI 后端的 Agent 本轮成功结束，却没有任何 jian 平台动作（未 `jian comment` 落本会话消息、也未 `jian subtask`/`jian status` 记本人活动）
- **THEN** 系统落一条醒目的系统活动标记（`⚠️ …未通过 jian comment/subtask 提交交付…`）便于发现追查，而**不**把流式 stdout 当作结论兜底展示（stdout 仍完整保留在 run_logs / 日志详情里）

#### Scenario: API 交付即 stdout
- **WHEN** API 后端的 Agent 执行完毕
- **THEN** 其 stdout 最终文本即该 Agent 的产出，落成 assistant 会话消息在正文展示（API 无 jian CLI 通道，不打上述标记）

#### Scenario: 收工写记忆
- **WHEN** 一次执行结束
- **THEN** 系统把关键结论写回该 Agent 记忆；取值优先 Agent 经 jian comment 落库的发言，无则回退流式 stdout 兜底
