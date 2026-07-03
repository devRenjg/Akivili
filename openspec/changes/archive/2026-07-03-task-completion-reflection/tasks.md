## 1. 反思引擎

- [x] 1.1 新增 `backend/reflect.py`：`reflect_on_task_done(task_id)` 主入口
- [x] 1.2 `_participants`：以 `task_runs` 为准收集本任务+子任务真正跑过的角色（去重、需已接入模型）
- [x] 1.3 `_reflect_one`：用角色自身 provider + persona 复盘，提炼 3-5 条 Know-how
- [x] 1.4 Know-how 写入受管段落 `knowhow`，去重合并；超 `KNOWHOW_MAX=30` 调模型压缩合并
- [x] 1.5 无可沉淀（回「无」/空）时不写；`asyncio.gather` 并发各角色、单个失败不拖累

## 2. 一次性模型调用

- [x] 2.1 `runner.run_oneshot(provider_id, system_prompt, prompt)`：不建 run/不落库/不碰会话，返回纯文本，超时/异常返回空串

## 3. 触发点

- [x] 3.1 `routes/tasks.py::set_status(done)` → `asyncio.create_task(reflect_on_task_done)`
- [x] 3.2 `routes/agent_cli.py::set_status(done)` → 同上（Leader/成员收尾）
- [x] 3.3 测试项目（`is_test_project`）跳过

## 4. per-run 记忆职责调整

- [x] 4.1 `runner._persist_memory` 改滚动受管段落 `recent`，保留最新 `_RECENT_RUNS_MAX=8` 条
- [x] 4.2 `finalize_run`（超时/取消路径）也调 `_persist_memory`，杜绝做完不沉淀

## 5. 验证

- [x] 5.1 新增 `TestReport/run_reflect_probe.py`（隔离库 + 假 run_oneshot）：参与者写入 / 未参与不写 / 超限压缩 / 测试项目跳过，6/6 通过
- [x] 5.2 并发探针 7/7、隔离主套件 30/30 保持通过
- [x] 5.3 真实 Claude 端到端：完成任务后参与角色产出真实 Know-how（非结论复述）

## 6. 文档与规格

- [x] 6.1 更新 README：能力概览/协同设计新增「任务完成经验反思」；版本记录补 v0.12.0
- [x] 6.2 新增 `specs/agent-reflection/spec.md`；更新 `specs/agent-collaboration` 超时需求
- [x] 6.3 归档本 change 提案
