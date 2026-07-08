# 孤儿运行回收：状态感知，不误伤已成功任务

## Why

启动孤儿回收把所有 `running` 的 `task_runs` 一刀切落 `killed`。但「进程正常结束、只是 run 没落库」型孤儿（如任务最终成功产出的那次 run，因流式连接断开/生成器被取消而没落终态）也被误标 `killed`，污染了**已成功任务**的真实终态：

- 工作区卡片 `run_status` 取最新 run = killed → 已完成任务显「■已终止」而非「✓执行完成」；
- 团队成员「已完成任务数」（`solved_tasks`，要求 `run=succeeded AND task=done`）少计（实测数据工程师显 3、应为 4）。

## What Changes

- 孤儿回收改为**状态感知**：`task_runs` 的 running 孤儿，若其任务已 `done/reviewing` 则落 `succeeded`（任务正是靠它成功的），否则落 `killed`。
- 已完成任务的孤儿不再记误导性「回收失败」活动。
- 前端 `Workspace.vue` 卡片执行状态以任务终态优先（done/reviewing → 显「执行完成」），作为不依赖 run 明细的双保险。

## Impact

- Specs: `agent-collaboration`（修改「启动时孤儿运行回收」需求）
- Code: `backend/collab.py`、`frontend/src/views/Workspace.vue`
- Tests: `TestReport/run_orphan_reclaim_probe.py` 13/13
