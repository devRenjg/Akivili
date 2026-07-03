# agent-library

## Purpose

把本地 `C:\Code\Agents` 的 Agent 人格模版扫描登记进系统，让用户能在库界面浏览、搜索、查看每个 Agent 的人格详情，作为后续在项目中选用 Agent 的来源。

## Requirements

### Requirement: 模版扫描与登记

系统 SHALL 扫描配置的 Agent 库根目录下的 `.md` 文件，解析其 frontmatter 与正文，登记到 `agent_templates` 表。

#### Scenario: 首次扫描
- **WHEN** 库为空且系统启动或用户触发扫描
- **THEN** 系统递归读取库目录，将每个含合法 frontmatter 的 `.md` 登记为一条模版（slug 唯一、记录 division/name/description/emoji/color/body）

#### Scenario: 排除非角色目录
- **WHEN** 扫描遇到 examples / integrations / strategy / assets / scripts 等非角色目录
- **THEN** 系统跳过这些目录，不登记为 Agent

#### Scenario: 幂等重扫
- **WHEN** 用户再次触发扫描
- **THEN** 已存在的 slug 被更新而非重复插入，返回新增/更新/跳过计数

### Requirement: 库浏览与搜索

用户 SHALL 能浏览、按分类筛选、按关键词搜索模版，并查看单个模版的人格详情。

#### Scenario: 按分类与关键词过滤
- **WHEN** 用户指定 division 或输入关键词
- **THEN** 系统返回匹配的模版列表（匹配 name 或 description）

#### Scenario: 查看人格详情
- **WHEN** 用户打开某个模版详情
- **THEN** 系统返回该模版的完整描述与人格正文 body

### Requirement: 解析健壮性

系统 SHALL 在个别文件解析失败时不中断整体扫描。

#### Scenario: 跳过非法文件
- **WHEN** 某 `.md` 缺少 frontmatter 或解析出错
- **THEN** 系统跳过该文件并计入 skipped，继续扫描其余文件
