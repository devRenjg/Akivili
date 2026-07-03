## 1. 数据模型

- [x] 1.1 tasks 加 `priority TEXT DEFAULT 'none'`、`parent_task_id INTEGER`（_migrate ALTER）
- [x] 1.2 新增 `activities` 表（task_id, actor_type, actor_name, action, detail, created_at）
- [x] 1.3 状态枚举扩展 backlog/blocked；STATUSES 与中文标签映射更新

## 2. 后端：活动埋点 + 接口

- [x] 2.1 activities 写入辅助（log_activity）
- [x] 2.2 建任务→created；改状态→status_changed(from/to)；改优先级→priority_changed；指派→assigned
- [x] 2.3 runner：执行开始→task_started；完成→task_completed；失败→task_failed
- [x] 2.4 GET /tasks/{id}/activities（activity + 对话消息按时序合并）
- [x] 2.5 GET/POST /tasks/{id}/subtasks；PUT /tasks/{id}/priority
- [x] 2.6 list_tasks 带 priority、子任务 done/total 进度

## 3. 前端：两栏详情

- [x] 3.1 TaskThread 重构为大弹窗两栏（中间主区 + 右侧属性栏）
- [x] 3.2 中间：标题+描述 + 子任务区（进度环+勾选+新建）+ 活动/对话时间线（系统事件小圆点 / 对话气泡）+ @输入框
- [x] 3.3 右侧属性栏：状态 picker、优先级 picker、负责人、创建/更新时间、执行日志区（每次 run 可展开 run_logs）
- [x] 3.4 activitiesApi / subtasksApi / priority 接入

## 4. 前端：看板增强

- [x] 4.1 卡片加优先级标识（圆点）+ 子任务进度环
- [x] 4.2 状态列适配（待办/规划中/进行中/验证中/已完成；阻塞作标记；归档区）
- [x] 4.3 拖拽/新建/编辑 兼容优先级

## 5. 验证（无头 Chrome 自测 + 遵守测试安全规则）

- [x] 5.1 建任务→活动时间线出现 created；改状态→出现 status_changed
- [x] 5.2 详情两栏正常渲染；右栏改状态/优先级生效并写活动
- [x] 5.3 子任务创建 + 进度环 done/total 正确
- [x] 5.4 执行任务→时间线出现 task_started/completed；执行日志区可展开
- [x] 5.5 看板卡片显示优先级+子任务进度
- [x] 5.6 更新 README、归档 change
