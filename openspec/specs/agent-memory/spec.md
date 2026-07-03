# agent-memory

## Purpose

每个 Agent 拥有一份持久记忆，存于 `memory/<agent-slug>.md`（跨项目共用，每个 md 文件即一个 Agent 的记忆）。Agent 默认开工先读取自己的记忆恢复上下文（含做事方式与要领），任务中把思考、结论、进度、信息写回。本能力提供记忆的存储与读写基础设施；执行时的自动读写闭环在 Agent 执行能力中实现。

## Requirements

### Requirement: 记忆读写

系统 SHALL 提供按 Agent slug 读取、覆盖写入、追加其记忆文件的能力。

#### Scenario: 读取尚不存在的记忆
- **WHEN** 某 Agent 还没有记忆文件时被读取
- **THEN** 系统返回空内容而非报错

#### Scenario: 写入并持久化
- **WHEN** 写入某 Agent 的记忆
- **THEN** 系统在 `memory/<slug>.md` 落盘（必要时创建目录），后续读取可取回

#### Scenario: 追加记忆
- **WHEN** 向某 Agent 记忆追加一段内容
- **THEN** 系统在原内容末尾追加，不覆盖既有记忆

### Requirement: 路径安全

系统 SHALL 把记忆文件路径限定在配置的 memory 目录内。

#### Scenario: 拒绝路径穿越
- **WHEN** slug 含 `..` 或绝对路径等穿越意图
- **THEN** 系统拒绝该操作，不读写 memory 目录之外的文件

### Requirement: 界面查看与编辑

用户 SHALL 能在界面查看并编辑某 Agent 的记忆。

#### Scenario: 查看与编辑
- **WHEN** 用户打开某 Agent 的记忆
- **THEN** 系统展示其记忆内容，用户可编辑并保存
