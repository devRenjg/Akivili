## Why

任务管理已跑通基础闭环，但状态流转与信息组织偏薄：只有 5 个状态、纯对话弹窗、没有活动轨迹、不能拆子任务、没有优先级。汲取成熟看板产品的能力与界面布局思路（配色沿用 Akivili 星穹风），把任务详情升级成两栏结构，补齐活动时间线、更细状态、优先级、子任务、执行日志区，让每个任务的生命周期一目了然。

## What Changes

- tasks 表加 `priority`、`parent_task_id`；状态枚举加 `backlog`、`blocked`（_migrate 兼容旧库）
- 新增 `activities` 表：任务活动时间线（创建/状态变更/优先级变更/指派/执行开始·完成·失败）
- 后端：tasks CRUD 支持优先级与子任务；关键动作埋点写 activity；runner 执行前后写 activity；新增 activities / subtasks / priority 接口；列表带子任务进度与优先级
- 前端：TaskThread 重构为**两栏大弹窗**（中间：标题+描述+子任务区+活动/对话时间线+输入框；右侧属性栏：状态/优先级/负责人/时间/执行日志区）
- 前端：看板卡片加优先级标识 + 子任务进度环；状态列适配

## Capabilities

### New Capabilities
- `task-system`: 看板式任务体系——任务有优先级、可拆子任务（带进度）、有细粒度状态（待办/规划/进行中/验证中/已完成/阻塞/归档）、有活动时间线记录全生命周期、有执行日志区呈现每次 Agent 运行；任务详情为两栏布局（主内容 + 属性侧栏）。

## Impact

- 后端：tasks 加列 + 新增 activities 表；tasks.py / runner.py 埋点；新增 activities/subtasks/priority 接口
- 前端：重构 TaskThread 为两栏；Workspace 卡片增强
- 数据：activities 开始写入；tasks.priority/parent_task_id 启用
- 兼容：旧任务默认 priority=none、无父、状态不变；_migrate 平滑升级
