## 1. 对话框 @mention 引入成员

- [x] 1.1 移除「@谁」下拉，textarea 输入 `@` 触发团队成员浮层补全（onInput 检测光标前最近 @）
- [x] 1.2 浮层键盘操作：↑↓ 选择、Enter/Tab 选中、Esc 关闭；插入 `@昵称`
- [x] 1.3 send() 用 parseMentions 解析被 @ 成员：第一位作流式主受理人，其余入队
- [x] 1.4 mentionName 口径（昵称优先、无则角色名）与后端 parse_and_enqueue_mentions 一致
- [x] 1.5 底部「将唤醒：@xx」chip 实时提示

## 2. 后端 dispatch 解析人工指令 @ 多人

- [x] 2.1 dispatch 端点调 parse_and_enqueue_mentions（主受理人作 author_slug 避免重复入队）
- [x] 2.2 start_loop() 幂等确保协同循环在跑，能领取入队的成员 run
- [x] 2.3 @ 解析失败不阻断主受理人流式执行

## 3. 孤儿回收补齐 task_runs

- [x] 3.1 reclaim_orphan_runs 同时回收 run_queue→failed 与 task_runs→killed（补 ended_at）
- [x] 3.2 清理内存注册表 _RUN_PIDS/_KILLED 残留
- [x] 3.3 探针 run_orphan_reclaim_probe 覆盖两层（升 12/12）

## 4. 布局与表格

- [x] 4.1 详情页 max-width 1280→1440、输入框 3→6 行
- [x] 4.2 MarkdownView 表格：圆角外框/深色表头/斑马纹/数字右对齐/横向滚动

## 5. 验证与文档

- [x] 5.1 QA 31/31、concurrency 7/7、orphan 12/12、parseMentions 单测 7/7、build 通过
- [x] 5.2 README 升 v0.16.4/v0.16.5；同步 agent-collaboration spec；openspec validate
