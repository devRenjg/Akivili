## 1. 重跑即时回写（问题一）

- [x] 1.1 `routes/runs._reactivate_on_redispatch`：重跑 done/reviewing 任务→即时回写 in_progress，父任务同理
- [x] 1.2 auto_dispatch 调用回写；仅对已收尾任务生效，首次执行不误伤
- [x] 1.3 前端 `rerunTask` 乐观更新 + `optimisticReactivate`（本地即时翻牌）

## 2. 会话结构化输出（生成侧）

- [x] 2.1 `JIAN_CLI_USAGE` 加排版要求：Markdown 结构化，不用 ━━━ 装饰线当标题
- [x] 2.2 负责人收尾 prompt 加同款 Markdown 结构要求

## 3. 渲染层次增强（渲染侧）

- [x] 3.1 `MarkdownView` 标题字号/字重/颜色差 + h1/h2 底部分隔线；粗体标签更深；列表留白/marker

## 4. 验证与文档

- [x] 4.1 新增 `run_reactivate_probe.py` 5/5；前端 build 通过；回归绿
- [x] 4.2 README 升 v0.16.1；归档 change、同步 3 个 spec（`openspec validate --specs`）
