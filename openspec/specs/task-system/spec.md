# task-system

## Purpose

看板式任务体系：任务有优先级、可拆子任务、有细粒度状态与活动时间线，详情为两栏布局（主内容 + 属性侧栏），执行日志区呈现每次 Agent 运行。

## Requirements

### Requirement: 细粒度状态与优先级

任务 SHALL 支持状态 待办/规划中/进行中/验证中/已完成/阻塞/归档，以及优先级 紧急/高/中/低/无。

#### Scenario: 改状态记入活动
- **WHEN** 任务状态变更
- **THEN** 系统保存新状态，并在活动时间线记录一条 status_changed（含 from/to）

#### Scenario: 改优先级
- **WHEN** 用户设置任务优先级
- **THEN** 系统保存并在时间线记录 priority_changed

### Requirement: 活动时间线

系统 SHALL 记录任务全生命周期的活动，供时间线展示。

#### Scenario: 生命周期事件入线
- **WHEN** 任务被创建、指派、状态/优先级变更、Agent 执行开始/完成/失败
- **THEN** 各生成一条活动记录，按时间与对话消息合并展示

### Requirement: 子任务

任务 SHALL 可拆分子任务并展示完成进度。

#### Scenario: 子任务进度
- **WHEN** 任务有若干子任务
- **THEN** 展示 done/total 进度；子任务可独立改状态

### Requirement: 两栏详情与执行日志

任务详情 SHALL 为两栏布局：主内容（描述+子任务+时间线+输入）与属性侧栏（状态/优先级/负责人/时间/执行日志）。

#### Scenario: 执行日志区
- **WHEN** 用户查看任务详情
- **THEN** 属性侧栏列出每次 Agent 运行（触发/状态/耗时），可展开查看该次执行的详细日志
