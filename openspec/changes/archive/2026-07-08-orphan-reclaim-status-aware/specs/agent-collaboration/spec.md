# agent-collaboration (delta)

## MODIFIED Requirements

### Requirement: 启动时孤儿运行回收

系统 SHALL 在启动时（协同后台循环开始领取新运行之前）把残留的「运行中」记录回收为终态，覆盖 `run_queue` 与 `task_runs` 两层数据源，使任务不再被误显示为「执行中」。回收 SHALL 尊重任务的真实终态：已成功收尾任务（`done`/`reviewing`）的运行孤儿视为「进程已正常结束、只是未落库」，落成 `succeeded`，不得误标为终止，以免污染卡片完成显示与成员已完成任务计数。

执行状态由本进程内存态驱动（`run_queue` 靠内存集合 + 处理协程；`task_runs` 靠执行生成器收尾或主动 finalize）。进程重启 / 生成器被取消 / 连接断开时收尾路径跑不到，会留下 `status='running'` 的孤儿：`run_queue` 孤儿使进度判定误报「执行中」，`task_runs` 孤儿使详情页执行记录列表显示「执行中」。

#### Scenario: 重启后回收两层孤儿（状态感知）
- **WHEN** 系统启动，发现 `run_queue` 或 `task_runs` 中存在 `status='running'` 的残留记录
- **THEN** 系统把 `run_queue` 的 running 落成 `failed`；`task_runs` 的 running 按其任务状态区分——任务已 `done`/`reviewing` 的落成 `succeeded`、其余落成 `killed`（并补 `ended_at`），清理进程注册表残留，使相关任务不再显示为执行中

#### Scenario: 已完成任务的孤儿不记误导活动
- **WHEN** 被回收的孤儿所属任务已处于 `done`/`reviewing`（任务实际已成功）
- **THEN** 系统不为该任务记录「回收失败」类活动（该任务是成功的，其 run 只是未落库），仅对未收尾任务记录回收活动

#### Scenario: 卡片完成显示不被单条 run 污染
- **WHEN** 某任务已 `done`/`reviewing`，但其最新一条 run 因回收或其他原因为 `killed`/`failed`
- **THEN** 工作区卡片以任务终态为准显示「执行完成」，不显示「已终止」/「失败」

#### Scenario: 只回收孤儿、不误伤有效记录
- **WHEN** 回收执行时存在非 running 的记录（如 `queued`/`done`/`failed`/`succeeded`）
- **THEN** 这些记录保持不变，仅 `running` 状态被回收

#### Scenario: 幂等
- **WHEN** 回收执行连续运行两次
- **THEN** 第二次没有可回收的孤儿，不产生任何变更
