# agent-collaboration

## MODIFIED Requirements

### Requirement: @mention 触发

@成员 SHALL 触发该成员执行 —— 无论 @ 来自 Agent 的发言、还是人工在任务对话框输入的指令。一条指令里 @ 多位成员时，SHALL 为每一位被 @ 的成员分别触发执行。

#### Scenario: Agent 委派触发执行
- **WHEN** 某 Agent 发言中 @ 了一位团队成员
- **THEN** 系统为该成员入队一个执行任务，由并发池调度执行

#### Scenario: 人工指令 @ 多位成员
- **WHEN** 管理员在任务对话框的指令里 @ 了多位团队成员
- **THEN** 系统让第一位被 @ 的成员作为主受理人即时执行，并为其余被 @ 的成员各入队一个执行任务，由协同后台循环调度执行；主受理人不被重复入队

#### Scenario: @ 候选来自当前项目团队
- **WHEN** 用户在对话框输入 `@` 触发成员补全
- **THEN** 候选列表为当前项目团队成员（按昵称/角色名匹配）；@ 不属于本项目团队的名字不触发任何执行

## ADDED Requirements

### Requirement: 启动时孤儿运行回收

系统 SHALL 在启动时（协同后台循环开始领取新运行之前）把残留的「运行中」记录回收为终态，覆盖 `run_queue` 与 `task_runs` 两层数据源，使任务不再被误显示为「执行中」。

执行状态由本进程内存态驱动（`run_queue` 靠内存集合 + 处理协程；`task_runs` 靠执行生成器收尾或主动 finalize）。进程重启 / 生成器被取消 / 连接断开时收尾路径跑不到，会留下 `status='running'` 的孤儿：`run_queue` 孤儿使进度判定误报「执行中」，`task_runs` 孤儿使详情页执行记录列表显示「执行中」。

#### Scenario: 重启后回收两层孤儿
- **WHEN** 系统启动，发现 `run_queue` 或 `task_runs` 中存在 `status='running'` 的残留记录
- **THEN** 系统把 `run_queue` 的 running 落成 `failed`、`task_runs` 的 running 落成 `killed`（并补 `ended_at`），清理进程注册表残留，使相关任务不再显示为执行中

#### Scenario: 只回收孤儿、不误伤有效记录
- **WHEN** 回收执行时存在非 running 的记录（如 `queued`/`done`/`failed`/`succeeded`）
- **THEN** 这些记录保持不变，仅 `running` 状态被回收

#### Scenario: 幂等
- **WHEN** 回收执行连续运行两次
- **THEN** 第二次没有可回收的孤儿，不产生任何变更
