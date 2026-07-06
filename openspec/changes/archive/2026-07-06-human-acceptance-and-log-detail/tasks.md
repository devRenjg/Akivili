## 1. 人工验收闭环

- [x] 1.1 `progress.on_execution_complete`：子任务成功→done、父任务全子完成→reviewing、独立任务完成→reviewing
- [x] 1.2 `_set_reviewing` / `_set_done` 幂等 + 记系统活动；`_has_pending_run` 查 run_queue
- [x] 1.3 `blocking_subtasks` 改按「是否有 queued/running run」判定
- [x] 1.4 `routes/agent_cli.py::status`：Agent（含 Leader）`done` 降级为 `reviewing`，活动线记录说明
- [x] 1.5 移除 Agent 驱动 done 时的反思/进阶副作用，反思只留在人工验收路径
- [x] 1.6 `collab.py`：非 Leader run 成功后调 `on_execution_complete`，不触发反思

## 2. 执行日志详情（transcript）

- [x] 2.1 `executor/base.py`：`ExecEvent` 加 `tool`/`tool_input`/`tool_output`，事件类型加 `thinking`/`tool_result`
- [x] 2.2 `claude_code.py`：stream-json 一行拆多事件，`tool_use` 保留完整 input、`user` 消息取 `tool_result`
- [x] 2.3 `codex.py`：`command_execution` 保留完整命令与输出
- [x] 2.4 `database.py`：`run_logs` 加 `tool`/`tool_input`/`tool_output` 三列 + 迁移
- [x] 2.5 `runner.py`：结构化落库（tool_input 存 JSON）
- [x] 2.6 新增 `backend/redact.py` 服务端脱敏；`routes/runs.py` 新增 `GET /runs/{id}/transcript`（解析 + 脱敏）
- [x] 2.7 SSE `dispatch` payload 带 `tool`/`tool_input`/`tool_output`（脱敏后）
- [x] 2.8 前端 `RunTranscriptDialog.vue`：timeline 进度条/元数据/过滤/排序/复制/逐条展开；`utils/redact.js` 兜底脱敏
- [x] 2.9 `TaskDetail.vue`：run 旁「日志详情」按钮 + 执行中工具行可展开

## 3. 看板与子任务

- [x] 3.1 `Workspace.vue`：新增「验证中」一等列（浅蓝样式）
- [x] 3.2 `Workspace.vue`：子任务嵌套小卡片（状态/优先级/头像/标题，点击进详情）
- [x] 3.3 `routes/tasks.py`：`list_tasks` 返回每个父任务的 `subtasks`（含最新 run 状态）
- [x] 3.4 `routes/tasks.py`：`SubtaskRequest` 加 description/priority；禁止在子任务下建子任务

## 4. 打磨

- [x] 4.1 `activity.py`：作者按 slug + name 双匹配，历史 slug 记录也能显示昵称/头像
- [x] 4.2 `agent_cli.py`：`_display_name` 把 slug 解析为「昵称（角色名）」
- [x] 4.3 `ProjectDetail.vue`：移除冗余项目信息卡

## 5. 验证与文档

- [x] 5.1 前端 `vite build` 通过；后端各模块语法校验通过
- [x] 5.2 `run_logs` 迁移在既有库跑通；claude 解析器单测；`/transcript` 端到端含脱敏
- [x] 5.3 更新 README（能力概览/执行引擎/功能列表/版本 v0.13.0）
- [x] 5.4 更新 specs：`task-board`/`task-system`/`agent-execution`/`agent-collaboration`
- [x] 5.5 归档本 change
