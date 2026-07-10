# project-management

## MODIFIED Requirements

### Requirement: 项目 Agent 团队

项目团队 SHALL 通过「从人才库邀请人才加入」单一入口组建；管理员在人才库选定人才邀请进项目，并可改造项目内实例或将其移除。项目区不再提供项目内「从库导入」「自建 Agent」入口，也不展示历史自建 Agent（无 `template_id` 的实例）。

#### Scenario: 从人才库邀请加入
- **WHEN** 管理员在人才库为某人才选择一个可加入的项目并邀请加入
- **THEN** 系统在该项目下创建一个 Agent 实例（复制人才人格为可编辑的 persona，slug 沿用人才身份以跨项目共享记忆）

#### Scenario: 项目区只展示通用人才
- **WHEN** 用户查看某项目的团队
- **THEN** 界面只展示来自人才库的通用人才（有 `template_id` 的实例），历史自建 Agent 不再显示；且团队区不提供项目内导入/自建按钮

#### Scenario: 改造项目内 Agent
- **WHEN** 用户修改某项目内 Agent 的 persona 或指定供应商
- **THEN** 系统保存改动，且不影响原人才模版与其他项目
