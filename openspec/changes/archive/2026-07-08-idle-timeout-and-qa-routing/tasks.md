## 1. 超时策略 A+B+C（collab.py）

- [x] 1.1 新增 `IDLE_TIMEOUT_SEC`/`IDLE_TIMEOUT_OVERRIDES`/`GRACE_SEC`/`HARD_WALL_SEC`/`HARD_WALL_OVERRIDES` + `_idle_timeout`/`_hard_wall`
- [x] 1.2 `_run_one` 的 `_drive`：逐事件消费，对每次取事件设 idle 超时，超硬墙钟即停；替换旧 `wait_for(墙钟)`
- [x] 1.3 `_run_produced_deliverable`：查该 run 是否已有 jian 交付（消息/活动）
- [x] 1.4 `_grace_then_kill`：判超时后宽限轮询——有交付→finalize succeeded 保成果；无→kill 进程树+finalize failed
- [x] 1.5 移除旧 `RUN_TIMEOUT_SEC`/`_run_timeout`

## 2. 收尾验收路由（progress.py）

- [x] 2.1 收尾 prompt 从写死「不许 @ 任何人」改为「如需验收先 @ 相应成员、通过后再汇总」
- [x] 2.2 `_qa_member_hint`：团队有测试/QA/安全成员时在提示里点名可选验收成员

## 3. 僵尸清理

- [x] 3.1 备份 DB，把卡 14 小时的 run#70/#71（子任务已 done、进程已死）落终态 killed，全库无残留 running

## 4. 验证与文档

- [x] 4.1 新增 `run_timeout_and_qa_probe.py` 12/12（保成果/失败/常量/验收路由）
- [x] 4.2 修复 3 个探针假执行器签名漂移；concurrency 7/7、subtask 6/6 恢复；其余回归绿
- [x] 4.3 README 升 v0.16.0；归档 change、同步 `agent-collaboration` spec（`openspec validate --specs`）
