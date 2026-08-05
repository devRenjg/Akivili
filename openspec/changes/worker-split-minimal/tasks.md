# Tasks — worker-split-minimal

> 规划态。未打勾 = 未实现。**最小可行优先**：每步独立探针验证，验证门不过不推进。
> 本 change 替代已废弃 [platform-graceful-restart] 阶段 2，以 Multica 极简 daemon 为蓝本。
>
> **执行顺序经讨论定档**（2026-07-24）：
> - **组 0（CAS）先行**：`_claim_one` 缺 `AND status='queued'` 条件本身即隐患，且是「新旧 worker 并存 / 未来多 worker」防重复执行的前置。独立、低风险、可单独回归 → 剥离动手前先落地。
> - **SSE 走做法 A**：只搬「队列路径」执行到 worker；`routes/runs.py:110` 的 SSE 直连对话路径**暂留 API 进程**。重启 API 仍会打断「正在直连对话的那一次」，但后台协同 Agent（绝大多数）不受影响。「API 进程彻底无执行、重启真正零打断」= 做法 B，延后到独立 change。
> - **状态下沉 DB 为设计原则**：搬迁/新增代码优先让状态经 DB 交接；但 `_RUN_PIDS`（pid+创建时间双因子防误杀）本版**保持不动**，不强改。

## 0 — claim 冗余安全带（剥离前置，先做）✅ 已合入 master（9f460a0）
- [x] 0.1 `_claim_one` 的 UPDATE 加 `AND status='queued'` 条件（CAS）+ SELECT 加 `FOR UPDATE OF run_queue SKIP LOCKED`（对标 Multica ClaimAgentTask 两层防护）：`rowcount=0` 视为被抢 → rollback + 返回 None。调用方 `_tick`/`_loop` 均已处理 None，零改动。单 worker 下纯冗余、零行为变化。
- [x] 0.2 探针：`run_pg_concurrency_probe` 新增 GROUP 4——12 并发 `_claim_one` 抢 M 条 queued，断言无重复领（无双执行）/无遗漏/多余领取者拿 None。真 PG 验证通过。
- [x] 0.3 独立回归 + 合入（组0 单独成 commit 9f460a0，先行交付；全量门禁 39/39 无回归）。

## 1 — 执行层搬迁到独立 worker 进程（做法 A：只搬队列路径）✅ 已合入 master（32be36a）
- [x] 1.1 新增 `backend/worker.py`：进程入口 = migrations（advisory lock 串行）→ `reclaim_orphan_runs(scope="queue")` → `start_loop`（复用 collab 的 `_loop`/`_process_one`/`_run_one`）→ 常驻。
- [x] 1.2 `main.py` 去掉 startup 里的 `reclaim_orphan_runs` + `start_loop()`（并删净死导入 `collab as collab_mod`）：API 进程不再跑队列执行循环，只入队 + 查询。
- [x] 1.3 `routes/runs.py` SSE 直连路径按做法 A 保留：删除第 108 行的 `collab.start_loop()`，直连 `execute_dispatch` 本身不动；proposal/spec 已显式标注做法 A 边界。
- [x] 1.4 `start.ps1` 改为 6 步，增起 worker 进程（与 API、前端并列），停止逻辑一并 Stop-Process worker。
- [x] 1.5 验证「API 停 ≠ 执行停」：worker 独立进程实测，杀 API 重启后 worker PID 全程未变（进程隔离实证）。reclaim scope 切分探针 17/17；kill 信号探针 13/13；BUG C（task_run_id 提前回填）由 scheduling 探针 Test F 守住。

## 2 — 进程树 containment（死进程不再写）✅ 已实现
- [x] 2.1 新增 `executor/containment.py`（纯 ctypes，不引 pywin32）：worker 启动 `init_containment()` 建 Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）；两处 CLI 起进程点（claude_code.py/codex.py）起进程后 `contain(pid)` 加入 Job。worker 死（含强杀/崩溃）→ 最后一个 Job 句柄关闭 → OS 连带终止全部 CLI 子进程。Job 未就绪（API 直连路径/初始化失败）静默降级、退回 kill_run 兜底，功能不回退。POSIX 走进程组（接口占位）。
- [x] 2.2 探针 `run_containment_probe`：强杀迷你 worker（taskkill /F，**不带 /T**，验证 OS 靠 Job 自动清而非递归杀树）后，其 contain 的 sleeper 子进程在约定时间内退出。3/3 通过。

## 3 — runtime_id 代际 orphan 回收（对标 Multica RecoverOrphanedTasksForRuntime）⏸️ DEFERRED（2026-07-24 决策）
> **为何 defer**：组 3 的核心价值是**解锁多 worker**（每个 worker 一个 `runtime_id`，reclaim 只回收
> 「上一代我的 runtime_id」的在途 run，避免多 worker 互相误杀 —— 对标 Multica `WHERE runtime_id=$1`）。
> 但多 worker 的收益（执行吞吐水平扩展 / 执行层高可用 / 滚动升级）**在当前单 worker + 单机场景下
> 兑现不了**，且本平台执行瓶颈通常在**大模型 provider 额度/限流**（见任务273 的 403 预算耗尽），
> 而非本地算力——多 worker 解决不了额度瓶颈。组 1 的「按路径切分 reclaim（scope=queue）」对**单
> worker 已完全够用**（防住了唯一真实风险：worker 误杀 API 直连 run）。故组 3 = 纯为未来投资，先 defer。
>
> **触发再做的条件**（满足其一）：① 出现「单 worker MAX_CONCURRENCY=3 并发不够、队列长期堆积」且瓶颈
> 确在本地算力（非大模型额度）；② 要多机部署 worker（此时还需共享项目文件系统等额外基建，runtime_id
> 只是第一块砖）。届时建议先做**最小版（做法 α：稳定 runtime_id，不上心跳表）**，心跳/活跃集合判定
> （做法 β，Multica 完整体 `agent_runtime`+`last_seen_at`）留到真需要精确快速回收时再补。
>
> **worker-split-minimal 的原始目标（平滑重启：改代码重启 API 不打断在跑 Agent）已由组 0+1+2 达成。**
> 组 3 属「多 worker 横向扩展」话题，与「平滑重启 + Session Resume」是两条线，不阻塞后者。
- [ ] 3.1 （deferred）worker 每次启动生成 runtime_id；`_claim_one` 领取时写入 run 行；reclaim 改为按 runtime_id 回收「上一代我的」在途 run。
- [ ] 3.2 （deferred）探针 `orphan_runtime_recovery_probe`：多 worker 场景下 worker-B 启动不误杀 worker-A 正在跑的 run，只回收自己上一代的孤儿。

## 4 — 回归
- [x] 4.1 全量 QA 门禁跑通（剥离后执行链路不回归）：门禁 39→40 项、40/40、601 断言、0 失败（含新增 kill_signal 探针 + orphan scope 切分 + scheduling Test F）。
- [x] 4.2 单实例重启纪律核对（组1 已实测）：API（56752）与 worker（28632）各自单实例、非 reload；重启杀净对应进程、无 orphan reload worker。**待补记忆**：`backend-restart-single-instance` 需从「单进程」更新为「双进程」（API + worker 各杀各起）。

## 设计原则（贯穿全 change）
- **状态经 DB 交接优先**：搬迁/新增代码时，run 生命周期状态优先落 `run_queue`/`task_runs`，减少对进程内共享内存（`_running` 集合）的跨进程依赖。`_RUN_PIDS`（pid+创建时间双因子防误杀）为 worker 进程内 kill 机制、天然进程内，本版保持不动。
- **做法 A 是阶段终点，非最终态**：SSE 直连路径留在 API 是有意的折中（对标 Multica 单实例、避免滑向 WS+Stream relay 航母）；「彻底零打断」的做法 B 归后续独立 change。
