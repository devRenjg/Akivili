## Why

两个真实痛点：

1. **Agent 自己把任务标「完成」，人还没验收就流转了。** 此前 Agent（含 Leader）`jian status done` 会直接把任务置为 `done`，触发经验反思与「已解决数」计数——但工作到底做没做好、要不要返工，人还没看过一眼。"执行结束"被当成了"任务验收通过"，二者本该分开。
2. **执行过程是黑盒，看不到 Agent 到底跑了什么命令。** 详情页右侧的执行日志只把工具调用压成一句「调用工具：Bash」，实际命令、参数、输出全丢了。用户无法审计 Agent 在本机执行了什么，出问题也难定位。

## What Changes

- **人工验收闭环（Human-in-the-loop）**：新增「验证中（reviewing）」为一等状态，插在「进行中」与「已完成」之间。
  - Agent 端 `jian status done` 一律**降级为 reviewing**（含 Leader），并在活动线记录说明——Agent 永远无法把任务标为真正完成。
  - 新增 `progress.on_execution_complete`：子任务执行成功自动置 `done`；某父任务的**全部子任务** done 后，父任务自动进入 `reviewing` 等待验收；无子任务的独立任务执行完成直接进 `reviewing`。
  - **经验反思 / 已解决数计数只在人工验收时触发**——即管理员手动把父任务/独立任务拖入「已完成」，此时对父任务连同其子任务一起反思。自动流程绝不触发反思。
  - `blocking_subtasks` 判定改为「是否还有 queued/running 的 run」而非「status != done」，执行完的子任务不再阻塞父任务验收。
- **执行日志详情（结构化 transcript 设计）**：把每次 run 的完整过程结构化留存、可视化还原。
  - 执行器保留每次工具调用的**工具名 + 完整入参（含 Bash 实际命令）+ 完整输出**；`run_logs` 表新增 `tool` / `tool_input` / `tool_output` 三列。
  - 新增 `GET /runs/{id}/transcript`：返回结构化事件序列（tool/tool_result/thinking/text/error），服务端统一脱敏（密钥/token/连接串）。
  - 详情页每次 run 旁新增「日志详情」按钮，打开全屏弹窗：彩色 timeline 进度条 + 元数据 chips（模型/时长/工具数）+ 工具类型过滤 + 排序 + 复制全部；逐条事件可展开看完整 input/output。
  - **实时可见**：SSE 流也带上 `tool_input`/`tool_output`，执行中气泡的工具行可点击展开看命令详情。
- **看板与子任务体验**：
  - 看板新增独立「验证中」列（浅蓝样式），位于「进行中」与「已完成」之间。
  - 子任务以**嵌套小卡片**呈现在父卡片下（状态点 + 优先级点 + 负责人头像 + 标题），点击进详情。
  - 子任务支持描述 + 优先级；强制两级（子任务下不能再建子任务）。
  - 看板 `list_tasks` 返回每个父任务的 `subtasks` 数组（含最新 run 状态）。
- **细节打磨**：活动时间线以昵称/角色名（而非英文 slug）呈现，兼容历史 slug 记录；项目概览移除冗余的项目信息卡。

## Capabilities

### Modified Capabilities
- `task-board`：新增「验证中」为一等看板列；状态流转明确 进行中 → 验证中 → 已完成。
- `task-system`：子任务嵌套卡片展示、带描述+优先级、强制两级；执行日志区新增「日志详情」入口。
- `agent-execution`：执行日志升级为结构化 transcript（工具名/完整命令/输出），可视化还原 + 服务端脱敏 + 实时流式携带命令详情。
- `agent-collaboration`：Agent（含 Leader）无法自行完成任务，`done` 请求降级为 `reviewing`；子任务执行成功自动完成、父任务自动进验证中；反思只在人工验收触发。

## Impact

- 后端：`progress.py` 新增 `on_execution_complete`/`_set_reviewing`/`_set_done`/`_has_pending_run`；`routes/agent_cli.py` 的 `status` 降级 done→reviewing、`_display_name` 昵称化；`routes/tasks.py` 子任务描述/优先级/两级校验 + 看板返回 subtasks；`collab.py` run 成功后触发自动流程；`activity.py` 作者按 slug+name 双匹配；`executor/base.py` `ExecEvent` 加工具字段、`claude_code.py`/`codex.py` 解析保留命令与输出、`runner.py` 结构化落库；`routes/runs.py` 新增 `/transcript` + SSE 带工具字段；新增 `backend/redact.py` 服务端脱敏。
- 数据：`run_logs` 表新增 `tool` / `tool_input` / `tool_output` 三列（轻量迁移，向后兼容旧行）。
- 前端：新增 `components/RunTranscriptDialog.vue` + `utils/redact.js`；`TaskDetail.vue` 接入日志详情按钮 + 实时工具行展开；`Workspace.vue` 验证中列 + 子任务嵌套卡片；`ProjectDetail.vue` 移除信息卡；`api/index.js` 加 `transcript`。
- 验证：前端 `vite build` 通过；后端各模块语法校验通过；`run_logs` 迁移在既有库跑通；claude 解析器单测确认 text+Bash命令(完整input)+tool_result 正确拆分；`/transcript` 端到端含脱敏验证通过。
