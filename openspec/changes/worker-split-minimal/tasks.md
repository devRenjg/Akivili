# Tasks — worker-split-minimal

> 规划态。未打勾 = 未实现。**最小可行优先**：每步独立探针验证，验证门不过不推进。
> 本 change 替代已废弃 [platform-graceful-restart] 阶段 2，以 Multica 极简 daemon 为蓝本。
>
> **执行顺序经讨论定档**（2026-07-24）：
> - **组 0（CAS）先行**：`_claim_one` 缺 `AND status='queued'` 条件本身即隐患，且是「新旧 worker 并存 / 未来多 worker」防重复执行的前置。独立、低风险、可单独回归 → 剥离动手前先落地。
> - **SSE 走做法 A**：只搬「队列路径」执行到 worker；`routes/runs.py:110` 的 SSE 直连对话路径**暂留 API 进程**。重启 API 仍会打断「正在直连对话的那一次」，但后台协同 Agent（绝大多数）不受影响。「API 进程彻底无执行、重启真正零打断」= 做法 B，延后到独立 change。
> - **状态下沉 DB 为设计原则**：搬迁/新增代码优先让状态经 DB 交接；但 `_RUN_PIDS`（pid+创建时间双因子防误杀）本版**保持不动**，不强改。

## 0 — claim 冗余安全带（剥离前置，先做）
- [ ] 0.1 `_claim_one` 的 UPDATE 加 `AND status='queued'` 条件（CAS）：`rowcount=0` 视为被抢 → 返回 None。**改前置动作**：Grep 全部调用方确认对 None 的处理（已知 `_tick`:926 / `_loop`:941 均已处理 None），列出影响面。单 worker 下纯冗余、零行为变化。
- [ ] 0.2 探针 `claim_cas_noop_probe`：单 worker（当前架构）下 claim 行为与改造前逐一致——不漏领、不重复领；并发领取同一行时至多一方成功。
- [ ] 0.3 独立回归 + 合入（不与后续剥离绑定，可先单独交付）。

## 1 — 执行层搬迁到独立 worker 进程（做法 A：只搬队列路径）
- [ ] 1.1 新增 `backend/worker.py`：进程入口，含事件循环 + 并发池，复用 `collab.py` 的 `_loop`/`_claim_one`/`_process_one`/`_run_one`（先搬迁、不重写逻辑）。
- [ ] 1.2 `main.py` 去掉 startup 里的 `collab.start_loop()`：API 进程不再跑**队列执行循环**，只保留入队（`parse_and_enqueue_mentions`/`enqueue_run`）与查询路由。
- [ ] 1.3 **`routes/runs.py` SSE 直连路径按做法 A 保留**：`event_stream` 仍在 API 进程内 `execute_dispatch` 起 CLI；仅删除其中为「拉起队列循环」而调的 `collab.start_loop()`（第 108 行）——入队交给 worker 领，直连执行本身不动。**在代码注释显式标注这是做法 A 的已知边界**（重启 API 会打断此路径正在进行的那一次）。
- [ ] 1.4 `start.ps1` 增加启动 worker 进程（与 API、前端并列）；worker 崩溃可独立重启。
- [ ] 1.5 探针 `worker_split_probe`：API 进程重启期间，worker 领取并跑完一个**队列路径** run，验证「API 停 ≠ 后台协同执行停」。

## 2 — 进程树 containment（死进程不再写）
- [ ] 2.1 worker 起 CLI 子进程时纳入进程树管控：Windows 用 Job Object（`AssignProcessToJobObject` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`），POSIX 用进程组/`setsid`——worker 死则 CLI 子进程被 OS 连带清理。
- [ ] 2.2 探针 `containment_probe`：强杀 worker 进程，验证其 CLI 子进程在约定时间内全部退出（无残留写库/写文件）。

## 3 — 重启整批 orphan 判死（对标 Multica RecoverOrphanedTasksForRuntime）
- [ ] 3.1 worker 启动时，把非本代遗留的在跑 run（`run_queue.status='running'` 且非本代）整批判死重投——一条 UPDATE 标终态 + 重入队（不逐个精细接管、不做 generation 交棒）。**沿用现有 `reclaim_orphan_runs`（collab.py:955）的两层清理（run_queue + task_runs）语义**，迁移到 worker 启动路径。
- [ ] 3.2 探针 `orphan_batch_recovery_probe`：模拟上一代 worker 残留在跑 run，新 worker 启动后整批判死、任务可被重新领取，无双执行。

## 4 — 回归
- [ ] 4.1 全量 QA 探针跑通（剥离后执行链路不回归）：入队→worker 领取→跑完→收尾→活动流；SSE 直连路径（做法 A 保留）仍正常流式。
- [ ] 4.2 单实例重启纪律核对（对齐记忆 backend-restart-single-instance）：API 与 worker 各自单实例，重启前杀净对应端口/进程；注意 worker 也可能有 `--reload` orphan 陷阱。

## 设计原则（贯穿全 change）
- **状态经 DB 交接优先**：搬迁/新增代码时，run 生命周期状态优先落 `run_queue`/`task_runs`，减少对进程内共享内存（`_running` 集合）的跨进程依赖。`_RUN_PIDS`（pid+创建时间双因子防误杀）为 worker 进程内 kill 机制、天然进程内，本版保持不动。
- **做法 A 是阶段终点，非最终态**：SSE 直连路径留在 API 是有意的折中（对标 Multica 单实例、避免滑向 WS+Stream relay 航母）；「彻底零打断」的做法 B 归后续独立 change。
