## Why

到 P3.5 为止，项目里的 Agent 配齐了模型、记忆、Skills、人格——但它们还不能真正干活。P4 让平台「活起来」：提供一个工作区看板，把工作拆成任务，@ 分派给团队成员（Agent），Agent 真实执行（调本地 CLI / API，在项目目录内改文件、跑命令），全程流式可见、可暂停 / kill、可查日志。

工作围绕项目展开 → 项目下创建任务 → 任务有负责人、有状态流转（规划中→进行中→验证中→已完成，随时可废弃归档）。任务卡片点进去就是一个对话终端（Thread），在里面 @ 负责人布置与追问，Agent 在此流式回复并执行。

本期聚焦**单个 Agent 的真实执行闭环**；Agent 之间的自动协同（Leader 拆解后自动 @ 下属）留待 P6。

## What Changes

- 新增 `tasks`（看板任务）/ `task_runs`（每次执行）/ `run_logs`（执行日志）三表；复用 `conversations`/`messages` 作为任务对话 Thread
- 后端新增执行引擎 `executor/`：`base`（抽象+事件）、`claude_code`（claude -p stream-json）、`codex`（codex exec --json）、`api_llm`（httpx 流式）、`runner`（按 agent_profiles.provider_id 选后端）
- 执行闭环：开工读记忆+会话历史 → 组装系统提示（人格+记忆+Skills+任务上下文）→ 真实执行（放开权限，--add-dir 锁项目目录，列表传参防注入，to_thread 卸载阻塞）→ 收工写记忆
- 后端新增 `routes/tasks.py`（CRUD+状态流转+归档+看板分组）、`routes/runs.py`（@分派 SSE 流式、kill、日志查询、对话历史）
- 前端新增 `Workspace.vue`（5 列看板）+ `TaskThread.vue`（任务内对话终端：@选择器、流式输出、运行中标识、暂停/kill、日志面板）；项目内加「工作区」入口

## Capabilities

### New Capabilities
- `task-board`: 项目下的看板式任务管理。任务有标题/描述/负责人，状态在 规划中→进行中→验证中→已完成 间流转，可随时废弃归档；看板按状态分列展示。
- `agent-execution`: @ 分派任务给团队成员后，该 Agent 按其接入模型真实执行（CLI 在项目目录内改文件跑命令 / API 纯对话），流式输出全过程，执行前读记忆与会话历史恢复上下文、执行后写回记忆；执行可暂停 / kill，状态与日志可监控查询。

## Impact

- 后端：新增 `executor/`（5 文件）、`routes/tasks.py`、`routes/runs.py`；database 增 3 表（含 _migrate 兼容旧库）
- 前端：新增 `Workspace.vue`、`TaskThread.vue` 与路由、API、项目内入口
- 数据：tasks/task_runs/run_logs 开始写入；复用 conversations/messages
- 安全：放开 CLI 权限（用户已确认）但 --add-dir 锁定项目目录；子进程列表传参绝不 shell 拼接；记录 PID 以便 kill；SSE 检测断开及时终止子进程，避免空转
