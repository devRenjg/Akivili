# agent-collaboration

## ADDED Requirements

### Requirement: Agent 不可自行完成任务（人工验收）

Agent（含 Team Leader）SHALL NOT 把任务标为真正「已完成」。Agent 通过平台命令请求 `done` 时，系统 SHALL 降级为「验证中（reviewing）」并在活动线记录说明，最终验收由人工执行。

#### Scenario: Agent 请求完成被降级
- **WHEN** 负责人或成员通过平台命令把任务状态改为 `done`
- **THEN** 系统把状态置为 `reviewing`（而非 `done`），并在活动时间线记录「任务需人工验收」，Agent 无法越过验收

#### Scenario: 反思只在人工验收触发
- **WHEN** 任务经自动流程进入 `reviewing`
- **THEN** 系统不触发经验反思与「已解决数」计数；只有管理员手动把任务拖入「已完成」时才触发（对父任务连同子任务一起反思）

### Requirement: 执行完成自动流转

系统 SHALL 在一次非 Leader 执行成功结束后推进任务生命周期：子任务自动置 `done`；某父任务全部子任务 `done` 后父任务自动进入 `reviewing`；无子任务的独立任务执行完成直接进入 `reviewing`。

#### Scenario: 子任务执行完成
- **WHEN** 某子任务的一次执行成功结束且无待处理运行
- **THEN** 系统把该子任务置为 `done`（不触发反思）

#### Scenario: 父任务待验收
- **WHEN** 某父任务的全部子任务均已 `done`
- **THEN** 系统把父任务自动推进到 `reviewing`，等待人工验收

#### Scenario: 子任务不阻塞判定
- **WHEN** 判断某任务是否仍在推进
- **THEN** 以其子任务是否还有 queued/running 的运行为准，已执行完的子任务不阻塞父任务验收
