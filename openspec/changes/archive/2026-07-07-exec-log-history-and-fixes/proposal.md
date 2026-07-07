## Why

v0.13.0 落地「人工验收闭环 + 日志详情」后，实际使用暴露出一批问题与体验缺口：

1. **执行日志区不好用**：多个运行实例各自独立成卡、并行展开，历史越堆越乱，看不清"这次运行到底干了啥"。更好的做法是——历史折叠、每行命令缩略 + 相对时间 + 图标 hover 看 Agent。
2. **日志详情有信息疏漏**：codex 后端把命令与输出挂在同一事件上，但详情弹窗只渲染入参、丢了运行结果；Claude 的工具结果行只显示「结果」而非「Bash」（拿不到工具名）。
3. **正文格式全失**：Agent 产出满是 `**粗体**`、编号列表、链接，却按纯文本展示，难读。
4. **人工验收闭环有卡死 bug**：子任务被误降级为「验证中」、父任务因竞态永远收不了尾、重构时丢了"唤醒负责人汇总"——导致"子任务全完成却无汇报"。
5. **多行发言被截断**：Agent 用 `jian comment "$(cat 多行文件)"` / pwsh here-string 发长内容，经 Windows `.bat` 的 `%*` 只剩第一行，完整产出丢失。

## What Changes

- **执行日志区重做为历史运行折叠列表**：进行中运行常显；历史运行折叠在「显示历史运行（N）」开关后、展开列出全部；每行 = 状态图标（hover 显示 Agent 名+状态+可点终止/重跑）+ 命令缩略版（撑满截断）+ 相对时间（刚刚/N分钟前/N小时前/N天前），行 hover 右侧换「日志详情」入口。`/tasks/{id}/runs` 每条附 `summary`（首条工具命令缩略、脱敏）。
- **日志详情增强**：工具事件展开同时显示「命令/参数 + 运行结果」（补 codex 输出）；Claude `tool_result` 按 `tool_use_id` 跨行回填工具名（标签显示 Bash）；每条右侧显示执行北京时间（去序号）；顶部显示「供应商名·模型」；筛选去空名、助手发言项命名「发言」；助手发言行内显示绿色「发言」标签。
- **Markdown 渲染**：新增 `MarkdownView`（marked GFM + DOMPurify 消毒），任务描述与消息气泡按 Markdown 渲染，支持标题/粗体/列表/表格/代码块/图片/可点击链接（含裸链接自动识别）。
- **人工验收闭环修复**：
  - 子任务 `jian status done` 不降级（子任务无「验证中」概念，直接 done）；子任务被标 `reviewing` 归一为 done，避免卡死。
  - `_has_pending_run` 增 `exclude_run_id`：排除"当前正在收尾、队列行尚未标 done 的 run"，修复最后一个子任务把自己算作 pending 导致父任务永不收尾的竞态。
  - 重构后补回「全部子任务完成 → 父任务进验证中 + 唤醒负责人做统一汇总汇报」。
  - 子任务执行/重跑时父任务处按有效状态显示「进行中」。
- **`jian comment` 支持 `--body-file`/`--stdin`**：多行长发言从文件/stdin 读，绕开命令行截断；系统提示要求长内容必须用 `--body-file`。
- **交互细节**：项目卡片/概览展示仓库链接（不暴露本地工作区路径）；执行状态改 Element Plus 图标；任务详情返回按钮改「返回」（子任务回父任务）；用户消息/活动按用户名显示同名头像；记忆数据仅管理员可见；后端默认热加载。

## Capabilities

### Modified Capabilities
- `agent-execution`：执行日志区改历史折叠列表（命令缩略+相对时间+图标hover）；日志详情补全工具命令+运行结果、工具名回填、执行时间、供应商标签；`jian comment` 支持 --body-file 修多行截断。
- `task-system`：任务描述与消息支持 Markdown 富文本渲染；返回按钮子任务回父任务。
- `agent-collaboration`：修复子任务 done 降级/卡死、父任务收尾竞态，补回负责人汇总汇报环节。

## Impact

- 后端：`routes/runs.py`（runs summary + transcript 命令/结果/工具名/时间/供应商）、`executor/claude_code.py`（tool_use_id→名映射）、`progress.py`（_has_pending_run exclude_run_id + _advance_and_summarize_parent）、`collab.py` / `routes/agent_cli.py`（收尾调用传 exclude_run_id、子任务 done/reviewing 归一）、`cli/jian.py`（comment --body-file/--stdin）、`routes/memory.py`（GET 加 require_admin）、`routes/projects.py`+`projects.py`+`database.py`（git_url）、`main.py`（默认 reload）。
- 前端：新增 `MarkdownView.vue` / `RunRow.vue` / `utils/redact.js`；`TaskDetail.vue`（历史列表、Markdown、返回、状态图标、用户头像、子任务有效状态）；`RunTranscriptDialog.vue`（命令+结果、工具名、时间、供应商、筛选、发言标签）；`AgentAvatar.vue`（加载失败回退）；`Dashboard/ProjectSpace.vue`（仓库链接）。
- 数据：`projects` 加 `git_url`、`messages` 加 `author_name`、`run_logs` 加 `tool/tool_input/tool_output`（均轻量迁移，向后兼容）。
- 验证：前端 vite build 通过；后端各模块 import/语法通过；线上任务 53 负责人已补做汇总汇报、任务 54/56/60 被截断内容已修复。
