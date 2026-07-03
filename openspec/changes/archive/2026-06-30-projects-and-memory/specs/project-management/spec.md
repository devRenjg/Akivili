# project-management

## Purpose

提供"项目"这一核心容器：用户围绕项目组织工作，每个项目绑定一个本地文件夹作为 Agent 的工作边界，并从模版库选用 Agent 组成团队。主页总览所有项目与状态。

## Requirements

### Requirement: 项目 CRUD

用户 SHALL 能创建、查看、更新、删除项目。项目含标题、本地文件夹路径、描述、状态。

#### Scenario: 创建项目并绑定本地文件夹
- **WHEN** 用户提交项目标题与一个已存在的本地文件夹路径
- **THEN** 系统创建项目并记录绑定路径

#### Scenario: 拒绝无效路径
- **WHEN** 用户提交的本地文件夹路径不存在或为空
- **THEN** 系统返回 400 并说明原因，不创建项目

#### Scenario: 删除项目级联清理
- **WHEN** 用户删除项目
- **THEN** 系统同时清理该项目下的 Agent 实例

### Requirement: 项目 Agent 团队

用户 SHALL 能从模版库为项目导入 Agent，也能自建，并可改造或移除。

#### Scenario: 从模版导入
- **WHEN** 用户选择一个模版导入到项目
- **THEN** 系统在该项目下创建一个 Agent 实例（复制模版人格为可编辑的 persona）

#### Scenario: 改造项目内 Agent
- **WHEN** 用户修改某项目内 Agent 的 persona 或指定供应商
- **THEN** 系统保存改动，且不影响原模版与其他项目

### Requirement: 主页总览

系统 SHALL 在主页以卡片形式展示所有项目及其状态与 Agent 数量。

#### Scenario: 查看项目总览
- **WHEN** 用户进入主页
- **THEN** 系统列出每个项目的标题、状态、绑定路径、Agent 团队规模
