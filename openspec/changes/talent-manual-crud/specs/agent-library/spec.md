# agent-library

## ADDED Requirements

### Requirement: 手动创建人才

管理员 SHALL 能在平台内直接创建一个数字人才，写入 `agent_templates`（标记 `origin='manual'`），并可同时设定人格资料与绑定平台已有 Skill。

#### Scenario: 管理员创建人才
- **WHEN** 管理员提交人才的 name/description/division/人格正文（可选昵称/头像/供应商/Skill 列表）
- **THEN** 系统以 `manual-<slug(name)>-<uuid>` 为 slug 登记一条 `origin='manual'` 的模版，写入 `agent_profiles`，并按 slug 建立 `agent_skills` 绑定，且触发人格记忆同步

#### Scenario: 昵称唯一
- **WHEN** 管理员提交的昵称与已有 Agent 昵称冲突
- **THEN** 系统拒绝创建并提示昵称已被占用

#### Scenario: 非管理员被拒
- **WHEN** 非管理员调用创建接口
- **THEN** 系统返回未授权，不创建任何记录

#### Scenario: 重扫不覆盖手动人才
- **WHEN** 系统重新扫描库目录
- **THEN** 按 slug 幂等 upsert 仅作用于扫描来源人才，`manual-*` 人才不被更新或删除

### Requirement: 分类管理

管理员 SHALL 能对分类做重命名、删除，并调整单个人才的分类；分类是 `agent_templates.division` 字段的轻量操作，无独立分类表。空分类（`division=''`）语义为「其他」。

#### Scenario: 改人才分类
- **WHEN** 管理员为某人才设定新的分类（或置空归「其他」）
- **THEN** 系统更新该模版的 `division` 字段

#### Scenario: 重命名分类
- **WHEN** 管理员把分类 A 重命名为 B
- **THEN** 系统把所有 `division=A` 的模版批量改为 `division=B`

#### Scenario: 删除分类
- **WHEN** 管理员删除分类 A
- **THEN** 系统把所有 `division=A` 的模版置为 `division=''`（归「其他」），不删除人才本身

#### Scenario: 非管理员只读
- **WHEN** 非管理员浏览人才库
- **THEN** 界面不提供任何变更入口（新增/扫描/改分类/改名/删除分类/编辑资料/邀请加入均隐藏），且后端对应写接口拒绝其调用

### Requirement: 展示人才集成的 Skills

人才详情 SHALL 返回并展示该人才（按 slug）已集成的 Skills。

#### Scenario: 查看已集成 Skills
- **WHEN** 用户打开某人才详情
- **THEN** 系统返回该人才启用的 Skill 列表（含名称与描述），界面以标签形式展示；无集成时显示「未集成任何 Skill」

### Requirement: 人才库排序

人才库列表 SHALL 优先展示更"活跃"的人才：先按已加入项目数降序，项目数相同再按已完成任务数降序，最后按分类、名字稳定兜底。

#### Scenario: 默认排序
- **WHEN** 用户浏览人才库列表
- **THEN** 项目数多的人才排在前；项目数相同的，已完成任务数越多越靠前
