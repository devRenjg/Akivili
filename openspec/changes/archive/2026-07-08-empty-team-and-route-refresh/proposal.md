## Why

新建项目后暴露两个问题：

1. **切换项目页面不自动更新**：在网页里从项目 A 切到项目 B（工作区、团队概览），看到的仍是 A 的旧数据，必须手动刷新才更新。根因：`ProjectDetail.vue`/`Workspace.vue` 的 `pid = Number(route.params.id)` 在组件 setup 时只算一次、数据只在 `onMounted` 加载；Vue Router 切换同名路由组件（`/projects/1` → `/projects/2`）复用组件实例、不重新 mount，导致 pid 与数据都停留在旧项目。
2. **新建项目默认塞入固定负责人**：`create_project` 后自动调 `_seed_leader` 把 `specialized-project-owner`（星，直播保障项目的负责人）设为新项目 Team Leader。用户希望自己为每个新项目挑选合适的负责人，而非所有项目都默认复用同一个。

## What Changes

- **路由切换强制重挂载**：`App.vue` 的 `<router-view>` 加 `:key="$route.path"`——路由 path 变化时强制重挂载组件，切项目/任务即自动加载新数据、无需手动刷新。用 `path`（非 `fullPath`）忽略 `?tab=` 等 query 变化，不打断同页 tab 切换。一行覆盖所有路由参数驱动的页面。
- **新项目从空团队开始**：移除 `create_project` 里的 `_seed_leader` 调用（及其函数、孤儿 import）。新建项目不再自动加入任何成员/负责人，由用户自行从人才库导入选定 Agent、再用现有「设为负责人」（`PUT /projects/{pid}/agents/{id}/leader`）指定 Team Leader。

## Capabilities

### Modified Capabilities
- `project-management`：新建项目从空团队开始（不默认拉入固定负责人）；切换项目时页面自动展示对应项目数据。

## Impact

- 前端：`App.vue`（router-view `:key`）。build 后生效，无需重启后端。
- 后端：`routes/projects.py`（移除 `_seed_leader` 调用与函数、清理孤儿 import：`sync_agent_memory`/`get_connection`/`DEFAULT_LEADER_SLUG`）。
- 数据：无迁移。既有项目团队不受影响；仅影响此后新建的项目。
- 验证：隔离验证新建项目成员数=0（PASS）；前端 build 通过；`openspec validate --specs`。
