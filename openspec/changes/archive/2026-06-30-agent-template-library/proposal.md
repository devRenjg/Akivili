## Why

JianAgency 的 Agent 来自 Agent 库目录（agency-agents-zh，314 个中文 Agent 人格定义，含新造的「项目负责人」与「测试专员」）。这些目前只是磁盘上的 `.md` 文件，系统并不认识它们。要让 Agent 真正"进系统"——被浏览、被选用、未来被导入到项目——第一步是把这些模版扫描解析、登记进库，并提供一个可浏览/搜索/查看详情的库界面。

本阶段聚焦**模版库层**（与项目无关、独立可用）。"导入到具体项目 + 项目内改造/自建"依赖项目实体，留到 P3（项目）落地后衔接。

## What Changes

- 后端新增 `agents.py`：扫描 Agent 模版根目录，解析 frontmatter（name/description/emoji/color）+ 正文，登记/更新到 `agent_templates` 表；排除非角色目录（examples / integrations / strategy）
- 后端新增 `routes/agents.py`：模版库列表（支持按分类、关键词过滤）、模版详情、手动触发重新扫描
- 配置新增 `agent_library_dir`（默认 Agent 库目录），可在设置中查看
- 前端新增 `Agents.vue`：Agent 库浏览页——按分类分组的卡片网格、搜索框、点击查看人格详情
- 导航新增「Agent 库」入口

## Capabilities

### New Capabilities
- `agent-library`: 系统扫描并登记 Agent 模版库（来自本地 Agent 库目录），用户可在库界面按分类/关键词浏览、搜索、查看每个 Agent 的人格详情；可手动触发重新扫描以同步磁盘变更。

## Impact

- 后端：新增 `agents.py` / `routes/agents.py`；`config.py` 增 `agent_library_dir`；复用 P1 的 `agent_templates` 表
- 前端：新增 `Agents.vue` 与路由、API 封装、导航入口
- 数据：`agent_templates` 表填充（幂等 upsert，按 slug 唯一）
- 安全：仅读取本地 `.md` 文件（解析 frontmatter，不执行）；扫描目录限定在配置的库根目录内，防路径穿越
