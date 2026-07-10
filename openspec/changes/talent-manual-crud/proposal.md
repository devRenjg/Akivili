# 管理员手动新增人才 + 分类管理

## Why

数字人才库的模版此前只能由本地库目录扫描生成（`origin=scan`），管理员无法在平台内直接补充一个尚未落库的人才，也无法整理分类——分类只是扫描出来的只读字段。实际运营中需要：把一个现成 Skills 组合包装成一个可选用的人才、按团队语义重新组织分类。本变更让管理员在平台内直接创建人才、并对分类做增/改/删。

## What Changes

- 新增「管理员手动创建人才」：写入 `agent_templates`（`origin='manual'`，slug 形如 `manual-<name>-<uuid>`），可设定 name/description/division/人格正文，并同时写 `agent_profiles`（昵称/头像/供应商）与 `agent_skills`（绑定平台已有 Skill，按 slug 库级共享）。
- 手动人才与扫描人才隔离：`origin` 字段标记来源；重扫按 slug 幂等 upsert，不会覆盖或删除 `manual-*` 人才。
- 分类作为 `agent_templates.division` 字段的轻量操作（无独立分类表）：
  - 改人才分类：`PUT /agents/templates/{id}/division`
  - 重命名分类（批量改字段）：`PUT /agents/divisions/rename`
  - 删除分类（该分类人才归「其他」，即 `division=''`）：`DELETE /agents/divisions/{name}`
- 上述写接口均 `require_admin`。
- 数据模型：`agent_templates` 增加 `origin TEXT DEFAULT 'scan'` 列（含迁移）。

## Impact

- Affected specs: `agent-library`（新增「手动创建人才」「分类管理」两项 Requirement，并说明重扫对手动人才的幂等隔离）
- Affected code: `backend/routes/agents.py`、`backend/database.py`（origin 列 + 迁移）、`frontend/src/views/Agents.vue`、`frontend/src/components/CreateTalentDialog.vue`、`frontend/src/api/index.js`
- 兼容性：新增列有默认值，存量 `scan` 人才行为不变；无破坏性变更。
