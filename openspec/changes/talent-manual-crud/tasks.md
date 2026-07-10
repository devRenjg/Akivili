# Tasks

## 1. 数据模型
- [x] 1.1 `agent_templates` 增加 `origin TEXT DEFAULT 'scan'` 列（SCHEMA + `_migrate`）

## 2. 后端接口
- [x] 2.1 `POST /agents/templates`（`require_admin`）手动创建人才：slug=`manual-<slug(name)>-<uuid8>`，`origin='manual'`，昵称唯一校验
- [x] 2.2 创建时写 `agent_profiles`（ON CONFLICT）与 `agent_skills`（INSERT OR IGNORE，按 slug 绑定）
- [x] 2.3 创建后调用 `sync_agent_memory` 落人格记忆
- [x] 2.4 `PUT /agents/templates/{id}/division` 改人才分类（''=其他）
- [x] 2.5 `PUT /agents/divisions/rename` 批量重命名分类
- [x] 2.6 `DELETE /agents/divisions/{name}` 删除分类（该分类人才 `division=''`）
- [x] 2.7 `GET /agents/templates` 返回 `origin`；重扫按 slug 幂等，不触碰 `manual-*`
- [x] 2.8 人才详情返回已集成 Skills（名称/描述，按 slug JOIN skills）
- [x] 2.9 列表排序改为 `project_count DESC, solved_tasks DESC, division, name`

## 3. 前端
- [x] 3.1 `CreateTalentDialog.vue`：分类（filterable+allow-create）、Skills 多选、图标、供应商、人格正文
- [x] 3.2 `Agents.vue`：管理员「新增人才」入口 + 分类重命名/删除 + 详情抽屉改分类 + 集成 Skills 展示
- [x] 3.3 `api/index.js`：`agentsApi.create/setDivision/renameDivision/deleteDivision`
- [x] 3.4 `ProjectDetail.vue`：移除「从库导入」「自建 Agent」入口，团队列表只显示通用人才（`visibleTeam`）
- [x] 3.5 非管理员只读：人才库全部变更入口 `isAdmin` 门控

## 4. 验证
- [x] 4.1 后端回归（QA suite / skill downloadable probe）全绿
- [x] 4.2 接口自测：detail 返回 skills、列表排序、砂金归「哔哩哔哩人才」
- [ ] 4.3 管理员登录后手动验收：创建人才、改/重命名/删除分类、绑定 Skill 展示、项目区无导入/自建入口
