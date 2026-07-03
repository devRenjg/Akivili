## Why

早期多 Agent 协同用「串行单并发」后台循环：队列里一次只跑一个 Agent，完成才取下一个。两个问题：

1. **慢/卡死的 Agent 阻塞整条队列**——一个成员执行慢或子进程卡住，后面所有成员都排队干等，一个任务被 @ 到的多名成员无法同时开工。
2. **卡死无兜底**——CLI 子进程若挂起，会一直占着唯一的执行位，拖垮整个协同、留下僵尸进程，违背「Agent 必须持续工作不卡壳」的产品要求。

## What Changes

- **串行单并发 → 并发池**：后台循环 `_loop` 一次性把空闲并发槽填满，最多 `MAX_CONCURRENCY`（默认 3）个 Agent 同时执行；被 @ 到的多名成员可并行开工，慢成员不再阻塞快成员。
  - `_claim_one` 原子领取 queued run 标 `running`；`_process_one` 执行完落库终态并释放并发槽；保留 `_tick` 作确定性单步原语（测试用），与并发池共用同一套 claim/process。
- **新增卡死超时兜底**：单个 Agent 执行设超时 `RUN_TIMEOUT_SEC`（默认 360s），超时即 `runner.kill_run` 杀子进程 + 记 `task_failed` 活动 + 释放并发槽；卡死成员只占一个槽、到点被清理，不拖垮队列、不留僵尸。
- **修复并发填充 bug**：旧 `_loop` 每领取一个 run 就 `sleep(0.3)`，短任务永远填不满并发池（峰值卡在 2/3）；改为连续补槽，只有队列空或池满才休眠。
- 深度上限、pending 去重、Leader 自触发守卫等防死循环机制保持不变。

## Capabilities

### Modified Capabilities
- `agent-collaboration`: 将「队列串行调度」需求替换为「并发池调度」（多成员并行、`MAX_CONCURRENCY` 上限）+ 新增「卡死超时兜底」需求（`RUN_TIMEOUT_SEC` 超时 kill + 释放槽 + 记活动）。

## Impact

- 后端：`collab.py` 后台循环改并发池；`RUN_TIMEOUT_SEC` 抽成模块常量；`_tick` 保留供测试单步。
- 数据：无 schema 变更（沿用 `run_queue` / `activities`）。
- 验证：新增 `TestReport/run_concurrency_probe.py`（隔离临时库 + 假执行器）7/7 通过；隔离主套件 30/30 保持通过。
- 安全/成本：并发上限 + 深度上限 + 超时兜底共同防失控；放开权限执行不变（仅可信内网）。
