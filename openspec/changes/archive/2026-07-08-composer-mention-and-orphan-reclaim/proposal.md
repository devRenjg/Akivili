## Why

1. **人工在对话框里想按需引入多位成员协作，但原交互只能从下拉选一个人**：追加指令区是一个「@谁」单选下拉 + 输入框，一次只能派给一位成员，无法在一条指令里 @ 多位、也不能在多轮会话里灵活引入不同成员。

2. **任务已完成，详情页右侧却持续显示「执行中」**：`reclaim_orphan_runs` 之前只回收 `run_queue`，但详情页右侧「执行记录」列表读的是 `task_runs`。多轮会话每轮 `dispatch` 建一条 `task_runs`，生成器被取消 / 断连 / 重启时收尾路径跑不到 → `task_runs` 卡 `running` 成孤儿，即使任务已流转「验证中」、外部卡片正常，右侧仍显「执行中」。

## What Changes

- **对话框输入区 @mention**：移除「@谁」下拉，改为在指令文本里输入 `@` 触发团队成员浮层补全（键盘上下选择、Enter/Tab 选中、Esc 关闭）。候选来自当前项目团队。
- **一条人工指令可 @ 多位成员**：`dispatch` 端点在执行主受理人的同时，解析人工指令里额外 `@` 的成员，复用 `parse_and_enqueue_mentions` 各入队一个 run，由协同后台循环串行执行。主受理人作为 `author_slug` 传入，避免被重复入队。
- **孤儿回收补齐 `task_runs` 层**：启动时的 `reclaim_orphan_runs` 同时回收两层——`run_queue`→`failed`、`task_runs`→`killed`（补 `ended_at`），并清理内存注册表残留。
- **对话框纵深放大**：详情页加宽（1280→1440）、输入框加高（3→6 行）。
- **表格样式强化**（纯样式）：任务详情 Markdown 表格改为圆角外框 / 深色表头 / 斑马纹 / 数字右对齐 / 可横向滚动。

## Capabilities

### Modified Capabilities
- `agent-collaboration`：@mention 触发扩展到「人工在对话框输入的指令」，且一条指令可 @ 多位成员分别入队；新增启动时孤儿运行回收（覆盖 run_queue 与 task_runs 两层）。

## Impact

- 前端：`views/TaskDetail.vue`（@mention 浮层、send 解析多人、放大布局）、`components/MarkdownView.vue`（表格样式，纯前端）。build 后生效。
- 后端：`routes/runs.py`（dispatch 解析人工指令 @ 的成员并入队 + 确保协同循环在跑）、`collab.py`（`reclaim_orphan_runs` 覆盖 task_runs）。改 runs/collab 需重启。
- 数据：无迁移。启动回收把历史孤儿 `task_runs` 落 `killed`。
- 验证：`run_orphan_reclaim_probe` 12/12、QA 31/31、concurrency 7/7；前端 `parseMentions` 纯逻辑单测 7/7、build 通过。composer 为管理员可见，交互需登录后人工实测。
