## Why

P0-P2 已经有了配置地基和 Agent 模版库。但平台还没有"项目"这个核心容器——用户的工作是围绕项目展开的：每个项目自起标题、绑定一个本地文件夹（Agent 的工作边界），并从模版库选用若干 Agent 组成团队。同时，主页需要把"有哪些项目、各自状态、配了哪些 Agent"直观呈现出来，这就是用户进入平台的第一落点。

此外，要落实"Agent 拥有持久记忆"的设定：每个 Agent 在 `C:\Code\JianAgency\memory\<agent-slug>.md` 维护一份跨项目共用的记忆，记录它的做事方式、要领、以及任务中的思考与进展。本阶段先把记忆的**基础设施**搭好（目录、约定、读写接口、UI 查看/编辑）；Agent 执行时"开工先读记忆、收工写记忆"的自动闭环在 P4（Agent 对话/执行）落地。

## What Changes

- 后端新增 `projects.py` + `routes/projects.py`：项目 CRUD、绑定/校验本地文件夹、项目状态
- 后端新增 `routes/project_agents.py`：把模版导入为项目内 Agent 实例（写 `project_agents`，可改造 persona、指定 provider）、列表、自建、改造、移除
- 后端新增 `memory.py` + `routes/memory.py`：读取/写入/追加某 Agent 的记忆文件（`memory/<slug>.md`），路径限定在 memory 目录内防穿越
- 前端新增 `Dashboard.vue`（重写）：项目状态卡片网格 + Agents 总览；新增项目入口
- 前端新增 `ProjectDetail.vue`：项目详情——基本信息、绑定文件夹、该项目的 Agent 团队（从库导入/自建/改造/移除）、查看每个 Agent 的记忆
- 配置新增 `memory_dir`（默认 `C:\Code\JianAgency\memory`）；启动时确保目录存在

## Capabilities

### New Capabilities
- `project-management`: 用户创建/管理项目（自起标题、绑定本地文件夹、描述、状态），从模版库为项目选用 Agent 组成团队，可在项目内改造或自建 Agent；主页以卡片总览所有项目与状态。
- `agent-memory`: 每个 Agent 在 `memory/<slug>.md` 拥有一份持久记忆（跨项目共用），系统提供读取/写入/追加接口，用户可在界面查看与编辑；为 P4 的"开工读记忆、收工写记忆"自动闭环提供基础。

## Impact

- 后端：新增 `projects.py` / `memory.py` / `routes/projects.py` / `routes/project_agents.py` / `routes/memory.py`；`config.py` 增 `memory_dir`；复用 P1 的 `projects` / `project_agents` 表
- 前端：重写 `Dashboard.vue`，新增 `ProjectDetail.vue` 与路由、API、导航
- 数据：`projects` / `project_agents` 表开始写入
- 安全：本地文件夹路径与 memory 文件路径均做存在性校验与边界限定（memory 读写限定在 `memory_dir` 内，防路径穿越）；创建项目时校验 local_path 存在
