# agent-skills

## Purpose

提供项目全局的 Skill 库（能力指令文本，存于 `skills/<slug>.md`），用户可导入或新建；每个 Agent 可勾选启用若干 Skill，运行时注入到 Agent 的能力上下文。Skill 的选择按 Agent 身份（slug）跨项目共享。

## Requirements

### Requirement: Skill 库管理

系统 SHALL 扫描 `skills_dir` 下的 `.md` 文件入库，并支持浏览、搜索、查看、新建、重新扫描。

#### Scenario: 导入并扫描
- **WHEN** 用户往 `skills_dir` 放入一个含 frontmatter 的 `.md` 并触发扫描
- **THEN** 系统登记该 Skill（slug 唯一、记录 name/description/body）

#### Scenario: 平台内新建
- **WHEN** 用户在界面新建 Skill
- **THEN** 系统在 `skills/<slug>.md` 落盘并入库

### Requirement: Agent 启用 Skill

用户 SHALL 能为某 Agent 勾选启用若干 Skill；该选择按 Agent 身份共享。

#### Scenario: 勾选并保存
- **WHEN** 用户为某 Agent 勾选若干 Skill 并保存
- **THEN** 系统记录该 Agent（按 slug）启用的 Skill 集合

#### Scenario: 跨项目共享
- **WHEN** 同一 Agent（同 slug）被引入另一个项目
- **THEN** 其启用的 Skill 集合与原项目一致

### Requirement: 路径安全

系统 SHALL 把 Skill 文件路径限定在 `skills_dir` 内。

#### Scenario: 拒绝穿越
- **WHEN** skill slug 含 `..` 或绝对路径
- **THEN** 系统拒绝读写
