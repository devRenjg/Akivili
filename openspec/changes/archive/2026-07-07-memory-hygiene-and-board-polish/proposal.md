## Why

Kickoff 介绍任务完成后核对各 Agent 记忆，发现「越做越强」的记忆机制存在若干会随时间放大的隐患：

1. **recent 段落是最大的低价值 token 块**：`_persist_memory` 存的是本轮发言全文，CLI Agent 常把「jian.bat 调用 / 设 PYTHONUTF8 / GBK 编码崩」这类过程碎语当结论存进 recent（负责人的 recent 段占 1412 字符、是其 knowhow 的 3 倍），既占上下文又可能诱导模型下次也纠结这些无关细节。
2. **knowhow 无差别全量注入 + 同质化污染**：`build_context` 把整份记忆文件塞进系统提示。低含金量任务（如纯介绍/沟通）会让每个角色都沉淀「用固定五段结构做自我介绍」这类通用套话，稀释真正的领域硬经验；且随任务增多 knowhow 持续膨胀，无关经验一并注入。
3. **history 全量回灌是更大的幻觉源**：协同长 thread 的所有消息全量回灌，叠加记忆/花名册/Skills，累积到数万 token，触发 lost-in-the-middle（模型忽略中段、混淆来源）。
4. **看板体验**：工作区卡片「执行完成」标记因有无子任务进度而左右横跳；顶层卡片按 `order_idx` 排序而非时间，最新任务不在最上。

## What Changes

- **recent 只存净结论（P0-1）**：`_persist_memory` 只记本轮该 Agent 经 `jian comment`/`jian subtask` 落库的净交付，**不再拿流式 stdout 兜底**（过程碎语不进 recent）；滚动上限 `_RECENT_RUNS_MAX` 8 → **3**。无净交付则不记（未走 jian 已由执行层打醒目标记，不重复噪声）。
- **knowhow 按相关性精选注入（P0-2）**：新增 `memory.select_relevant_knowhow(slug, task_text, top_n)`——用 jieba 分词做「当前任务 ↔ 各 knowhow 条目」关键词重叠打分，注入系统提示时只放最相关的 top-N（默认 8）。**文件里 knowhow 全量保留**，仅注入时精选。注入的 knowhow/recent 一并**剥离 `<!-- akivili:task:ID -->` 归属标记**（对模型无意义、占 token）。`build_context` 改经 `_compose_injected_memory` 组装（精选 knowhow + recent + workspace），不再 dump 整份文件。
- **反思质量门槛（P1-3）**：反思 prompt 增「宁缺毋滥 + 只沉淀本专业领域的新方法/新坑/新诀窍；常规沟通/介绍/汇报类无专业增量则回『无』、不写通用套话」。
- **history 回灌滑动窗口（P1-4）**：新增 `_HISTORY_MAX_MSGS`（默认 20）+ `_clip_history`，回灌只保留最近 N 条消息（早期丢弃），对 CLI 与 API 两条路径同时生效。
- **看板对齐与排序**：卡片执行状态用 `margin-left:auto` 固定在右侧（有无子任务进度都不横跳）；顶层任务列表排序 `order_idx, id` → **`created_at DESC, id DESC`**（最新在最上）。

## Capabilities

### Modified Capabilities
- `agent-execution`：上下文恢复明确「记忆按当前任务相关性精选注入、会话历史滑动窗口截断」。
- `agent-memory`：per-run「近期动态」只存净交付、滚动保留最新少量条；注入时按相关性精选、剥离归属标记。
- `agent-reflection`：反思设质量门槛，低价值任务允许回「无」、不灌通用套话。
- `task-board`：看板按创建时间倒序展示，卡片执行状态右对齐固定。

## Impact

- 后端：`executor/runner.py`（`_compose_injected_memory` + `_clip_history` + `_RECENT_RUNS_MAX`/`_HISTORY_MAX_MSGS`/`_KNOWHOW_INJECT_TOP_N` 常量 + `_persist_memory` 只存净结论）、`memory.py`（`select_relevant_knowhow` + `_tokens` 分词，jieba 优先、零依赖退化）、`reflect.py`（prompt 门槛）、`routes/tasks.py`（board 排序）。
- 前端：`views/Workspace.vue`（`.tc-run` 右对齐）。build 后生效。
- 依赖：分词优先用已装的 jieba；缺失时自动退化为「英文词 + 中文 2-gram」零依赖方案，不新增强制依赖。
- 数据：无迁移。记忆文件 knowhow/recent 全量保留，仅注入策略变化。
- 验证：新增 `TestReport/run_memory_hygiene_probe.py` 11/11（相关性精选/标记剥离/条目≤N全给/recent上限/history窗口/反思门槛）；`run_stdout_display_probe` 8/8、`run_reflect_probe` 6/6、QA 28/30 与基线一致；真实记忆冒烟：数据向任务注入的 8 条全为数据经验、前端向经验被正确过滤、标记已剥离。
