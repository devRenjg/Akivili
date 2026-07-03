## 1. 后端：并发池

- [x] 1.1 `_loop` 改并发池：一次性填满空闲槽（`MAX_CONCURRENCY=3`），只有队列空/池满才休眠
- [x] 1.2 `_claim_one` 原子领取 queued run 标 running；`_process_one` 执行完落库终态 + `_running.discard` 释放槽
- [x] 1.3 保留 `_tick` 确定性单步原语（与并发池共用 claim/process），供测试逐步驱动
- [x] 1.4 修复填充 bug：删掉旧「每 claim 一个就 sleep(0.3)」导致短任务填不满池

## 2. 后端：卡死超时兜底

- [x] 2.1 `RUN_TIMEOUT_SEC` 抽成模块常量（默认 360s）
- [x] 2.2 `_run_one` 里 `asyncio.wait_for(_consume(), RUN_TIMEOUT_SEC)`，超时 → `runner.kill_run` + 记 `task_failed` 活动
- [x] 2.3 超时/异常后仍在 `_process_one` finally 落库终态并释放并发槽

## 3. 验证（隔离临时库 + 假执行器）

- [x] 3.1 新增 `TestReport/run_concurrency_probe.py`（monkeypatch execute_dispatch/kill_run，RUN_TIMEOUT_SEC 临时调小）
- [x] 3.2 卡死 Agent 超时被 kill、run 落 done、记 task_failed、释放槽
- [x] 3.3 并发池峰值达 MAX_CONCURRENCY、3 并行明显快于串行
- [x] 3.4 慢 Agent 不饿死快 Agent（快的先完成）
- [x] 3.5 隔离主套件 `run_qa_suite.py` 30/30 保持通过

## 4. 文档与规格

- [x] 4.1 更新 README：多 Agent 协同设计新增「并发池调度 + 卡死兜底」小节，功能列表/路线图措辞同步
- [x] 4.2 更新 `specs/agent-collaboration/spec.md`：串行 → 并发池 + 卡死兜底
- [x] 4.3 归档本 change 提案
