# agent-execution

## Purpose

@ 分派任务给团队成员后，该 Agent 按其接入模型真实执行，流式输出全过程，执行前读记忆与会话历史恢复上下文、执行后写回记忆；执行可暂停 / kill，状态与日志可监控。本期聚焦单个 Agent 执行，Agent 间协同留待后续。

## Requirements

### Requirement: @ 分派与真实执行

用户 SHALL 能在任务对话中 @ 负责人下达指令，触发该 Agent 真实执行。

#### Scenario: 分派执行
- **WHEN** 用户在任务 Thread 中 @ 某成员并下达指令
- **THEN** 系统按该 Agent 的接入模型启动执行（CLI 在项目目录内可改文件/跑命令，API 为纯对话），流式返回过程

#### Scenario: 上下文恢复
- **WHEN** Agent 开始执行
- **THEN** 系统先读取其记忆（含工作区约束与 Skills 说明）与该任务的会话历史，组装进上下文

#### Scenario: 收工写记忆
- **WHEN** 一次执行结束
- **THEN** 系统把关键结论写回该 Agent 记忆

### Requirement: 执行控制

用户 SHALL 能终止（kill）正在运行的执行。

#### Scenario: Kill 运行
- **WHEN** 用户对运行中的执行点击 Kill
- **THEN** 系统终止其子进程，执行状态置为 killed

### Requirement: 状态与日志监控

系统 SHALL 记录每次执行的状态与日志，供查询。

#### Scenario: 查看日志
- **WHEN** 用户查看某次执行
- **THEN** 系统返回其状态与日志记录

### Requirement: 执行安全

系统 SHALL 安全地调用执行器。

#### Scenario: 防注入与目录限定
- **WHEN** 启动 CLI 子进程
- **THEN** 一律列表传参（不 shell 拼接），工作目录 / --add-dir 限定在该项目的本地路径
