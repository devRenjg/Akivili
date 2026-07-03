# agent-collaboration

## Purpose

多 Agent 事件驱动协同：任务可由 Team Leader 统筹，读任务、按成员技能委派、成员执行后回报、Leader 汇总收尾。调度由 LLM 依据注入的协作协议与团队花名册决策；@mention 是 Agent 间唯一通信原语；执行由并发池调度（多成员可并行），单 Agent 卡死有超时兜底，并有多层防死循环。

## Requirements

### Requirement: Leader 统筹调度

系统 SHALL 在 Leader 执行时注入协作协议与团队花名册，使其协调而非亲自执行。

#### Scenario: Leader 委派
- **WHEN** 启动某任务的协同（唤醒 Team Leader）
- **THEN** Leader 读任务后按成员技能 @mention 委派给合适成员，并停手等待

#### Scenario: 花名册
- **WHEN** Leader 被激活
- **THEN** 其上下文包含每个成员的名字、角色、技能与 @ 语法

### Requirement: @mention 触发

Agent 发言里的 @成员 SHALL 触发该成员执行。

#### Scenario: 委派触发执行
- **WHEN** 某 Agent 发言中 @ 了一位团队成员
- **THEN** 系统为该成员入队一个执行任务，由并发池调度执行

### Requirement: 并发池调度

系统 SHALL 用并发池执行协同中的各次 Agent 运行，允许多个 Agent 同时执行，同时最多 `MAX_CONCURRENCY` 个。

#### Scenario: 并行执行
- **WHEN** 队列中有多个待执行运行且空闲并发槽 > 0
- **THEN** 后台循环把空闲槽填满并发执行，最多同时运行 `MAX_CONCURRENCY` 个；某成员执行慢不阻塞其他成员

#### Scenario: 达到并发上限
- **WHEN** 正在运行的 Agent 数已达 `MAX_CONCURRENCY`
- **THEN** 后台循环不再领取新运行，直到有运行完成释放并发槽

### Requirement: 卡死超时兜底

系统 SHALL 为单个 Agent 执行设置超时，超时即终止其**整棵子进程树**、落库执行终态并释放并发槽，不得拖垮队列或留下僵尸进程与孤儿运行记录。超时阈值 SHALL 支持按角色（slug）配置，默认值适配普通角色，取数等长耗时角色可单独放宽。

#### Scenario: 执行超时被终止
- **WHEN** 某 Agent 运行时长超过其角色对应的超时阈值（默认 30 分钟；数据类角色 60 分钟）
- **THEN** 系统终止其执行（kill 整棵进程树，非仅父进程）、在活动记录写入执行失败说明（以角色名/昵称呈现，而非内部 slug）、并释放该并发槽，队列继续处理其他运行

#### Scenario: 超时不留孤儿运行记录
- **WHEN** 执行因超时被取消（其执行生成器的正常收尾代码来不及运行）
- **THEN** 系统主动把该运行的 `task_runs` 状态落成终态（不停留在 `running`），并清理进程注册表

### Requirement: 防死循环

系统 SHALL 通过多层机制防止 Agent 互相 @ 无限循环。

#### Scenario: 自触发守卫
- **WHEN** Leader 刚以 Leader 身份发言
- **THEN** 不因自身发言再次唤醒自己

#### Scenario: 深度上限
- **WHEN** 单个任务的协同累计运行数达到上限
- **THEN** 系统停止继续入队并在活动记录说明
