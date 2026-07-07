# agent-collaboration

## MODIFIED Requirements

### Requirement: 执行完成自动流转

系统 SHALL 在一次非 Leader 执行成功结束后推进任务生命周期：子任务自动置 `done`；某父任务全部子任务 `done` 后父任务自动进入 `reviewing` **并唤醒负责人做统一汇总汇报**；判断"是否还有待跑运行"时 SHALL 排除当前刚收尾、其队列行尚未标 done 的运行本身。

#### Scenario: 子任务执行完成
- **WHEN** 某子任务的一次执行成功结束
- **THEN** 系统把该子任务置为 `done`（不触发反思）

#### Scenario: 父任务收尾不被自身竞态阻塞
- **WHEN** 父任务的最后一个子任务刚执行完、触发收尾检查，而该子任务自己的队列行尚未被标记 done
- **THEN** 系统在"是否还有待跑运行"判断中排除该运行本身，正确识别为"全部完成"，把父任务推进到 `reviewing`

#### Scenario: 唤醒负责人汇总
- **WHEN** 父任务因全部子任务完成而进入 `reviewing`
- **THEN** 系统唤醒负责人，注入各子任务成果清单，令其用 `jian comment` 写一份统一汇总汇报（长内容用 --body-file），不再派活；该汇总只触发一次

### Requirement: Agent 不可自行完成任务（人工验收）

Agent（含 Team Leader）SHALL NOT 把**顶层任务**标为「已完成」；对顶层任务的 `done` 请求降级为 `reviewing`。子任务无「验证中」概念——子任务的 `done` 正常放行，`reviewing` 归一为 `done`。

#### Scenario: 顶层任务 done 降级
- **WHEN** Agent 对顶层任务请求 `done`
- **THEN** 降级为 `reviewing`，记录活动，等待人工验收

#### Scenario: 子任务不卡在验证中
- **WHEN** Agent 对子任务请求 `done` 或 `reviewing`
- **THEN** 一律落为 `done`（子任务无验证中概念），并触发父任务收尾检查，不会卡死
