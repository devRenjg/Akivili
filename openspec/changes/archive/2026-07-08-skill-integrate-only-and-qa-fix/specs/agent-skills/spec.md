# agent-skills

## MODIFIED Requirements

### Requirement: Skill 库管理

系统 SHALL 扫描 `skills_dir` 下的 Skill 入库，并支持浏览、搜索、查看、新建、重新扫描。Skill 有两种形态：单文件型（`<slug>.md`）与目录型能力包（`<slug>/SKILL.md`，可含 `scripts`/`references` 等配套子目录，登记为 `is_dir`）。

#### Scenario: 导入并扫描单文件型
- **WHEN** 用户往 `skills_dir` 放入一个含 frontmatter 的 `<slug>.md` 并触发扫描
- **THEN** 系统登记该 Skill（slug 唯一、记录 name/description/body）

#### Scenario: 扫描目录型能力包
- **WHEN** `skills_dir` 下存在 `<slug>/SKILL.md`（含 frontmatter），触发扫描
- **THEN** 系统登记该 Skill 为目录型（`is_dir`），source_path 指向该目录、正文取自 `SKILL.md`

#### Scenario: 平台内新建
- **WHEN** 用户在界面新建 Skill
- **THEN** 系统在 `skills/<slug>.md` 落盘并入库

## ADDED Requirements

### Requirement: Skill 下载与「仅集成」访问控制

Skill 的 frontmatter `downloadable` 字段 SHALL 控制其可下载性：值为 `false`/`no`/`0`/`off` 时视为「仅供 Agent 集成、不对外提供下载」（登记为不可下载），缺省或其它值视为可下载。系统 SHALL 在服务端强制该控制，不依赖前端隐藏。

#### Scenario: 可下载的 Skill 打包下载
- **WHEN** 用户对一个可下载的 Skill 请求下载
- **THEN** 目录型能力包打包为 zip（含 `SKILL.md` 与配套 `scripts`/`references`）、单文件型下 `.md`，并记录下载日志

#### Scenario: 仅集成的 Skill 拒绝下载
- **WHEN** 用户（或直接调用下载接口）对一个 `downloadable=false` 的 Skill 请求下载
- **THEN** 系统返回 403 并说明「仅供 Agent 集成使用」，不产出任何文件

#### Scenario: 前端标识仅集成
- **WHEN** 用户在 Skills 页浏览一个 `downloadable=false` 的 Skill
- **THEN** 界面隐藏下载按钮并显示「仅集成」标识；目录型 Skill 额外标为「能力包」

#### Scenario: 仅集成的 Skill 仍可被 Agent 启用
- **WHEN** 某 Agent 勾选启用一个 `downloadable=false` 的 Skill
- **THEN** 运行时其 `SKILL.md` 正文照常注入该 Agent 的能力上下文（「仅集成」只限制下载、不限制集成使用）
