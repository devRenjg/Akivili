# agent-runtime-config

## Purpose

每个 Agent 默认接入一个大模型，从已配置的供应商中选择。接入模型按 Agent 身份（slug）跨项目共享，作为 P4 运行时调用执行器的输入。

## Requirements

### Requirement: 选择接入模型

用户 SHALL 能为某 Agent 从已配置的供应商中选择一个作为默认接入模型。

#### Scenario: 选择并保存
- **WHEN** 用户为某 Agent 选择一个供应商
- **THEN** 系统记录该 Agent（按 slug）的接入模型

#### Scenario: 无可用供应商
- **WHEN** 尚未配置任何供应商
- **THEN** 界面提示用户先去设置页配置

### Requirement: 跨项目共享

接入模型 SHALL 按 Agent 身份共享。

#### Scenario: 跨项目一致
- **WHEN** 同一 Agent（同 slug）被引入另一个项目
- **THEN** 其接入模型与原项目一致

### Requirement: 独立性

不同 Agent 的接入模型 SHALL 互相独立。

#### Scenario: 各自独立
- **WHEN** 修改某 Agent 的接入模型
- **THEN** 其他 Agent 的接入模型不受影响
