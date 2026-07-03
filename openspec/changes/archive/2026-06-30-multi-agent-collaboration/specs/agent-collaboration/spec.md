# agent-collaboration

## Purpose

多 Agent 事件驱动协同：任务可由 Team Leader 统筹，读任务、按成员技能委派、成员执行后回报、Leader 汇总收尾。调度由 LLM 依据注入的协作协议与团队花名册决策；@mention 是 Agent 间唯一通信原语；任务队列串行调度并有多层防死循环。

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
- **THEN** 系统为该成员入队一个执行任务并串行执行

### Requirement: 队列串行调度

系统 SHALL 用队列串行执行协同中的各次 Agent 运行。

#### Scenario: 串行
- **WHEN** 队列中有多个待执行运行
- **THEN** 后台循环一次执行一个，完成后取下一个

### Requirement: 防死循环

系统 SHALL 通过多层机制防止 Agent 互相 @ 无限循环。

#### Scenario: 自触发守卫
- **WHEN** Leader 刚以 Leader 身份发言
- **THEN** 不因自身发言再次唤醒自己

#### Scenario: 深度上限
- **WHEN** 单个任务的协同累计运行数达到上限
- **THEN** 系统停止继续入队并在活动记录说明
