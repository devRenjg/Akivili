# Tasks

## 1. 后端：状态感知回收
- [x] 1.1 `reclaim_orphan_runs` 对 `task_runs` 的 running 孤儿：任务已 `done/reviewing` → `succeeded`，否则 → `killed`
- [x] 1.2 已完成任务的孤儿不记「回收失败」活动（避免误导）

## 2. 前端：卡片完成显示不被单条 run 污染
- [x] 2.1 `Workspace.vue` 新增 `effRunStatus`：任务 done/reviewing 时卡片显「执行完成」

## 3. 数据订正与验证
- [x] 3.1 手动订正 task 65 的成果 run（run 79）→ succeeded
- [x] 3.2 孤儿回收探针升级并通过 13/13
- [x] 3.3 QA 31/31、concurrency 7/7 回归全绿
