## MODIFIED Requirements

### Requirement: 并发池调度

系统 SHALL 用并发池执行协同中的各次 Agent 运行，允许多个 Agent 同时执行，同时最多 `MAX_CONCURRENCY` 个。

（原「队列串行调度」需求废弃：不再一次只跑一个。）

#### Scenario: 并行执行
- **WHEN** 队列中有多个待执行运行且空闲并发槽 > 0
- **THEN** 后台循环把空闲槽填满并发执行，最多同时运行 `MAX_CONCURRENCY` 个；某成员执行慢不阻塞其他成员

#### Scenario: 达到并发上限
- **WHEN** 正在运行的 Agent 数已达 `MAX_CONCURRENCY`
- **THEN** 后台循环不再领取新运行，直到有运行完成释放并发槽

### Requirement: 卡死超时兜底

系统 SHALL 为单个 Agent 执行设置超时，超时即终止其子进程并释放并发槽，不得拖垮队列或留下僵尸进程。

#### Scenario: 执行超时被终止
- **WHEN** 某 Agent 运行时长超过 `RUN_TIMEOUT_SEC`
- **THEN** 系统终止其执行（kill 子进程）、在活动记录写入执行失败说明、并释放该并发槽，队列继续处理其他运行
