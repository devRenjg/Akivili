## 1. recent 只存净结论（P0-1）

- [x] 1.1 `_persist_memory` 只取本轮该 Agent 经 jian 落库的净交付，去掉 stdout 兜底
- [x] 1.2 `_RECENT_RUNS_MAX` 8 → 3
- [x] 1.3 无净交付则不记 recent（未走 jian 已由执行层打标记）

## 2. knowhow 相关性精选注入（P0-2）

- [x] 2.1 `memory.select_relevant_knowhow` + `_tokens`（jieba 优先、零依赖退化）
- [x] 2.2 `runner._compose_injected_memory`：精选 knowhow(top-8) + recent + workspace
- [x] 2.3 注入时剥离 `<!-- akivili:task:ID -->` 归属标记
- [x] 2.4 条目 ≤ top_n 或无任务关键词时全给（不误删）

## 3. 反思质量门槛（P1-3）

- [x] 3.1 反思 prompt 增「宁缺毋滥 + 低价值任务回『无』、不写通用套话」

## 4. history 回灌滑动窗口（P1-4）

- [x] 4.1 `_HISTORY_MAX_MSGS`(20) + `_clip_history`，CLI/API 两路径同时生效

## 5. 看板对齐与排序

- [x] 5.1 卡片执行状态 `.tc-run` `margin-left:auto` 固定右对齐
- [x] 5.2 顶层任务排序 `order_idx,id` → `created_at DESC, id DESC`

## 6. 验证与文档

- [x] 6.1 新增 `run_memory_hygiene_probe.py` 11/11；回归与基线一致
- [x] 6.2 真实记忆冒烟：数据向任务精选命中、标记剥离
- [x] 6.3 README 升 v0.15.0；归档 change、同步 4 个 spec（`openspec validate --specs`）
