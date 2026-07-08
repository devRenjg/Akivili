## 1. 切换项目自动刷新（问题一）

- [x] 1.1 `App.vue` 的 `<router-view :key="$route.path">`，路由参数变化强制重挂载
- [x] 1.2 用 path 非 fullPath，忽略 query 变化不打断 tab 切换

## 2. 新项目空团队（问题二）

- [x] 2.1 移除 `create_project` 的 `_seed_leader` 调用
- [x] 2.2 删除 `_seed_leader` 函数及孤儿 import（sync_agent_memory/get_connection/DEFAULT_LEADER_SLUG）

## 3. 验证与文档

- [x] 3.1 隔离验证新建项目成员数=0；前端 build 通过
- [x] 3.2 README 升 v0.16.2；归档 change、同步 project-management spec（openspec validate）
