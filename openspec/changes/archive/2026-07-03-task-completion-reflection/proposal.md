## Why

任务完成后，参与角色应当有**经验沉淀与 Know-how 学习**，以后做同类工作更好。但此前：

1. **触发点缺失**——记忆写入发生在每个 run 执行结束时，而「卡片拖入已完成」这条路径根本没有写记忆的动作，"完成→学习"链路不存在。
2. **内容是流水账**——写入的只是任务结论的原样截断，不是"这类活怎么做更好"的可迁移要领。
3. **超时取消导致漏写**——写记忆挂在执行生成器尾部，执行超时被取消时收尾代码跑不到，做完的任务也不沉淀（数据工程师超时案例即如此）。
4. **无治理**——append 模式无限追加，记忆会越来越大、污染人格上下文。

## What Changes

- **新增 `reflect.py` 经验反思引擎**：任务进入 `done` 时触发，对本任务（含子任务）里真正跑过 run 的每个角色，用其**自身模型 + 人格**做一次轻量复盘，提炼 3-5 条 Know-how。
  - 参与者以 `task_runs` 为准（有 run = 真参与、有产出），去重收集，仅纳入已接入模型的角色。
  - 反思 prompt 明确要求"聚焦方法/坑/诀窍/判断依据、不要复述结论数字"；无可沉淀时回「无」不写。
  - Know-how 存独立受管段落 `<!-- akivili:managed:knowhow -->`，去重合并；超上限（默认 30 条）时再调模型压缩合并为 Top-N。
- **新增一次性模型调用 `runner.run_oneshot`**：给定人格 + 指令返回纯文本，不建 run、不落 messages、不碰任务会话，供反思/总结类"借模型想一段话"的场景；超时/异常返回空串。
- **触发点**：`routes/tasks.py::set_status(done)`（管理员拖入已完成）+ `routes/agent_cli.py::set_status(done)`（Leader/成员 `jian status done` 收尾），均 `asyncio.create_task` fire-and-forget 不阻塞。测试项目跳过。
- **per-run 记忆职责调整**：`runner._persist_memory` 从"无限 append 结论存档"改为**滚动受管段落** `<!-- akivili:managed:recent -->`（默认保留最新 8 条），只做轻量近期动态；经验学习交给反思。

## Capabilities

### Added Capabilities
- `agent-reflection`: 任务完成时参与角色各自复盘、沉淀 Know-how 到自身记忆；产出为可迁移经验而非结论存档；Know-how 受管段落去重与滚动上限治理；测试项目跳过。

### Modified Capabilities
- `agent-memory`: 收工记忆分为「近期动态」滚动段落（per-run 轻量流水，保留最新 N 条）与「Know-how」段落（任务完成反思产出），均为受管段落、不影响手写内容。

## Impact

- 后端：新增 `reflect.py`；`executor/runner.py` 新增 `run_oneshot` + `finalize_run` 也触发记忆落库、`_persist_memory` 改滚动段落；`routes/tasks.py`、`routes/agent_cli.py` 在 `done` 触发反思。
- 数据：无 schema 变更（沿用 `task_runs` / `messages` / `activities` + 记忆文件受管段落）。
- 成本：反思为每个参与者一次额外模型调用（并发、失败不拖累其他）；受管段落上限防膨胀。
- 验证：新增 `TestReport/run_reflect_probe.py`（隔离库 + 假 run_oneshot）6/6；并发探针 7/7、隔离主套件 30/30 保持通过；真实 Claude 端到端验证：完成任务后参与角色产出 5 条真实 Know-how。
