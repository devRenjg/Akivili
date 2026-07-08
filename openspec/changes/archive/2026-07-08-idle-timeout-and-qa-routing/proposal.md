## Why

任务 #62（更新数据+跨年组件）实跑暴露三个真问题：

1. **超时误伤 + 成果被销毁**：数据工程师流萤经 Narya/ingest 遍历全库补数据，真在干活却撞 60 分钟固定墙钟超时被 kill、标 failed；而其实数据已写好、Narya 已跑完。固定墙钟超时对"取数就是慢"的角色是误判，且**超时=失败**把已完成的成果一并销毁（会话里没有她的交付，因为还没来得及 `jian comment` 就被切了）。
2. **收尾跳过测试验收**：负责人派活时说过"两条线做完交测试专员统一验收"，但子任务全 done 后系统自动入队的收尾 run 指令写死「无需再派活/@任何人，直接汇总」，把验收环节吃掉了——测试专员从头到尾没被唤醒（0 run）。用户要的"找测试专员验证"被平台机制架空。
3. **僵尸运行**：花火子任务重跑后，两条旧 run 卡在 `running` 达 14 小时（进程早死、状态没落终态），干扰父任务进度聚合。

## What Changes

- **超时策略：固定墙钟 → 静默超时 + 保成果 + 硬墙钟兜底**（`collab.py`）：
  - **A 静默超时（idle）**：不看总耗时，看**多久没有新输出事件**——只要 Agent 持续产出（stdout/工具事件）就不判超时；仅**连续 `IDLE_TIMEOUT_SEC` 无任何事件**（真卡死）才触发。慢但在干活的任务永不被误杀。默认 15 分钟，数据类角色 30 分钟（`IDLE_TIMEOUT_OVERRIDES`）。
  - **B 超时保成果**：判超时后先给 `GRACE_SEC`（默认 90s）宽限并轮询——若该 run 在宽限内已产出真实交付（会话有其 jian comment/subtask 发言，或有 `jian status` 活动），视为**成功**（finalize succeeded、不 kill、不销毁）；宽限内仍无交付才 kill 进程树 + 落 failed。
  - **C 硬墙钟兜底（`HARD_WALL_SEC`）**：防极端失控（狂刷日志既不静默也不结束），默认 3 小时、数据类 4 小时的总时长天花板，到顶无条件终止。
- **收尾支持验收路由**（`progress.py`）：父任务全子完成的收尾 prompt 从写死"不许 @ 任何人"改为"**如原计划需要测试/验收，先 @ 相应成员验收、通过后再汇总**"；团队里有测试/QA/安全类成员时，`_qa_member_hint` 在提示里点名可选的验收成员。收尾不再架空验收环节。
- **僵尸运行清理**：本次两条卡死 14 小时的 `running`（run#70/#71，子任务已 done、进程已死）落终态 `killed`（删前备份 DB）。

## Capabilities

### Modified Capabilities
- `agent-collaboration`：卡死超时兜底由「固定墙钟」升级为「静默超时 + 宽限保成果 + 硬墙钟」；父任务收尾唤醒负责人时支持按需先做测试/验收路由。

## Impact

- 后端：`collab.py`（`IDLE_TIMEOUT_SEC`/`IDLE_TIMEOUT_OVERRIDES`/`GRACE_SEC`/`HARD_WALL_SEC`/`HARD_WALL_OVERRIDES` 常量 + `_idle_timeout`/`_hard_wall`/`_run_produced_deliverable`/`_grace_then_kill` + `_run_one` 的 `_drive` 逐事件消费替换旧 `wait_for` 墙钟；移除旧 `RUN_TIMEOUT_SEC`/`_run_timeout`）；`progress.py`（`_qa_member_hint` + 收尾 prompt 验收路由）。
- 前端：无改动。
- 数据：无迁移。一次性清理 run#70/#71 僵尸（备份 `jianagency.db.bak_before_zombie_finalize_20260708`）。
- 验证：新增 `TestReport/run_timeout_and_qa_probe.py` 12/12（保成果 done/无交付 kill+failed/静默与硬墙钟常量/收尾验收路由+点名测试成员+移除禁@措辞）；修复 3 个既有探针的假执行器签名漂移（缺 `persist_user_msg` 参数致新 `_drive` 调用失败），concurrency 2/7→7/7、subtask 2/6→6/6 恢复真实覆盖；memory-hygiene 11/11、stdout-display 8/8、reflect 6/6、QA 28/30。修复中发现并修正 `_grace_then_kill` 缺 `runner` 局部导入的真 bug（否则超时路径线上会崩）。
