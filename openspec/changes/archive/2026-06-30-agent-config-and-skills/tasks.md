## 1. 数据模型

- [x] 1.1 `database.py` 增 `agent_profiles`（slug PK, provider_id, updated_at）
- [x] 1.2 增 `skills`（id, slug UNIQUE, name, description, source_path, body, imported_at）
- [x] 1.3 增 `agent_skills`（agent_slug, skill_slug, PK 复合）

## 2. 后端：Skill 库

- [x] 2.1 `config.py` 增 `skills_dir`，默认 项目内 `skills/`
- [x] 2.2 `skills.py`：扫描目录下 `.md`，解析 frontmatter(name/description)+body，幂等 upsert（仿 agents.py）
- [x] 2.3 `routes/skills.py`：GET 列表（关键词过滤）、GET 详情、POST 重扫、POST 新建/PUT 编辑（写 `<slug>.md`，防穿越）
- [x] 2.4 startup 建 skills_dir + README + 空库自动扫描；main.py 注册

## 3. 后端：Agent 档案（模型 + Skills）

- [x] 3.1 `routes/agent_config.py`：GET 某 slug 档案（provider_id + 已选 skill slugs）
- [x] 3.2 PUT 接入模型（写 agent_profiles，upsert by slug）
- [x] 3.3 PUT 启用的 Skills（重写 agent_skills 该 slug 的关联）
- [x] 3.4 main.py 注册

## 4. 前端：Skills 库页

- [x] 4.1 `Skills.vue`：搜索 + 卡片网格 + 详情抽屉 + 新建 + 重新扫描（仿 Agents.vue）
- [x] 4.2 导航增「Skills」Tab；router + api/skillsApi

## 5. 前端：项目内 Agent 配置

- [x] 5.1 Agent 卡片增「接入模型」下拉（从 settings 供应商列表选，空则提示去设置页配）
- [x] 5.2 Agent 卡片增「Skills」按钮 → 勾选弹窗（全局 skill 库多选）
- [x] 5.3 卡片显示当前模型徽标 + 已选 Skills 数
- [x] 5.4 标注「模型/记忆/Skills 按 Agent 身份跨项目共享」
- [x] 5.5 api/index.js 增 skillsApi、agentConfigApi

## 6. 验证

- [x] 6.1 skills 目录放一个 .md，重扫后库中可见
- [x] 6.2 给某 Agent 选模型 + 勾 Skills，保存后重开保持
- [x] 6.3 在另一个项目引入同一 Agent，模型与 Skills 自动一致（跨项目互通）
- [x] 6.4 不同 Agent 的模型/Skills 互相独立
- [x] 6.5 新建 skill 落盘到 skills/<slug>.md；路径穿越被拒
