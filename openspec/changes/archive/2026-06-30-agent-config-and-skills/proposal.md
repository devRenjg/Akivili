## Why

项目里被安排进来的 Agent 要真正干活，需要两样配置：**接入哪个大模型**、**带哪些能力（Skills）**。同时按用户设定，模型、记忆、Skills 都绑定在「Agent 身份」上——同一个 Agent 无论被引入哪个项目，这三样都互通共享，只有人格（persona）允许各项目独立改造。

本阶段（P3.5）搭好「配置层」：让每个 Agent 配齐它的模型与技能，并提供一个项目全局的 Skill 库（`C:\Code\JianAgency\skills`，用户可自行导入 `.md`）。运行时真正把模型+记忆+Skills 注入执行器让 Agent 跑起来，是 P4 的事。

## What Changes

- 新增 `agent_profiles` 表（按 slug 一行，存接入的 provider_id）；新增 `skills` 表（Skill 库）；新增 `agent_skills` 关联表（agent_slug ↔ skill_slug）——后两者均按 slug，实现跨项目互通
- 后端新增 `skills.py`（扫描 `skills/` 目录解析入库，仿 agents.py）+ `routes/skills.py`（库列表/详情/重扫/新建/编辑）
- 后端新增 `routes/agent_config.py`：读写某 slug 的接入模型与启用的 Skills
- `config.py` 增 `skills_dir`；startup 建目录 + README + 空库自动扫描
- 前端新增「Skills」导航 Tab 与 `Skills.vue`（库浏览/搜索/查看/新建/重扫）
- 前端项目详情的 Agent 卡片：增「接入模型」下拉（从已配供应商选）+「Skills」勾选弹窗 + 卡片显示模型与技能数；标注模型/记忆/Skills 跨项目共享

## Capabilities

### New Capabilities
- `agent-skills`: 项目全局的 Skill 库（能力指令文本，存于 `skills/<slug>.md`），用户可导入/新建；每个 Agent 可勾选启用若干 Skill，选择按 Agent 身份（slug）跨项目共享。
- `agent-runtime-config`: 每个 Agent 可从已配置的大模型供应商中选择默认接入模型，按 Agent 身份（slug）跨项目共享。

## Impact

- 后端：新增 `skills.py` / `routes/skills.py` / `routes/agent_config.py`；`config.py` 增 `skills_dir`；database 增 3 张表
- 前端：新增 `Skills.vue` 与导航；改 `ProjectDetail.vue` 增模型与 Skills 配置
- 数据：新增 `agent_profiles` / `skills` / `agent_skills`；`project_agents.provider_id` 退为展示用，实际以 agent_profiles 为准
- 安全：skills 目录读写限定在 `skills_dir` 内（同 memory 的白名单 slug 防穿越）；仅读取 `.md`，不执行
