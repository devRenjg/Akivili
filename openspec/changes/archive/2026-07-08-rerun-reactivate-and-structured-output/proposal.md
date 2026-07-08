## Why

任务详情页两个体验问题：

1. **重跑子任务时父任务状态显示滞后**：去子任务卡片重新触发执行后，父任务处仍显示旧的「已完成」，要等几秒（3 秒轮询 progress 聚合出「有 running 子 run」）才翻成「进行中」。有明显的滞后窗口。
2. **会话正文没有主次层次**：任务详情的会话消息虽按 Markdown 渲染，但 Agent 的汇报/交付**用 `━━━` 装饰线 + emoji 当章节标题**、只零星加粗，几乎不用 `##` 标题——渲染出来是扁平正文，读者抓不到重点。问题在生成侧（Agent 不产结构）与渲染侧（层次对比可更强）两头。

## What Changes

- **重跑即时回写 + 前端乐观更新**：
  - 后端 `auto_dispatch` 重跑时，若目标任务已 `done`/`reviewing`，立即把它——及其父任务（若也 `done`/`reviewing`）——回写 `in_progress`，不等轮询聚合（`_reactivate_on_redispatch`）。仅对已收尾任务生效，首次执行（backlog/in_progress）不误伤。
  - 前端 `rerunTask` 乐观更新：重跑瞬间本地即时把该子任务与当前父任务视图置「进行中」，再由轮询校正。滞后窗口消除。
- **会话结构化输出（生成侧）**：`jian CLI` 使用说明 + 负责人收尾 prompt 增排版要求——发言/汇报/交付用 Markdown 结构（`##`/`###` 小标题、`**粗体**` 标关键项、`-` 列表），**不要用 `━━━` 装饰线当标题**（那样渲染无层次）。
- **渲染层次增强（渲染侧）**：`MarkdownView` 强化标题字号/字重/颜色差 + h1/h2 底部细分隔线、粗体作为字段名标签更深、列表留白与 marker 弱化。让结构化内容一眼看出主次。

## Capabilities

### Modified Capabilities
- `agent-collaboration`：重跑已收尾任务时即时回写父/子任务为进行中，消除状态显示滞后。
- `agent-execution`：jian CLI 发言/交付要求 Markdown 结构化排版（有层次），不用装饰线假装标题。
- `task-system`：富文本渲染强化标题层次与主次对比。

## Impact

- 后端：`routes/runs.py`（`_reactivate_on_redispatch` + auto_dispatch 调用）、`executor/runner.py`（`JIAN_CLI_USAGE` 加排版要求）、`progress.py`（收尾 prompt 加 Markdown 结构要求）。
- 前端：`views/TaskDetail.vue`（`rerunTask` 乐观更新 + `optimisticReactivate`）、`components/MarkdownView.vue`（标题/粗体/列表样式增强）。build 后生效。
- 数据：无迁移。老消息（含 `━━━` 装饰线）照旧渲染，新消息更结构化。
- 验证：新增 `TestReport/run_reactivate_probe.py` 5/5（子任务重跑回写父+子、独立任务回写自身、首次执行不误伤）；前端 build 通过；回归 timeout+QA 12/12、subtask 6/6、concurrency 7/7、reflect 6/6、QA 28/30 与基线一致。
