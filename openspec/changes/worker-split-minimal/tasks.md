# Tasks — worker-split-minimal

> 规划态。未打勾 = 未实现。**最小可行优先**：先跑通单 worker 剥离，每步独立探针验证，验证门不过不推进。
> 本 change 替代已废弃 [platform-graceful-restart] 阶段 2，以 Multica 极简 daemon 为蓝本。

## 1 — 执行层搬迁到独立 worker 进程
- [ ] 1.1 新增 `backend/worker.py`：进程入口，含事件循环 + 并发池，复用 `collab.py` 的 `_loop`/`_claim_one`/`_process_one`/`_run_one`（先搬迁、不重写逻辑）
- [ ] 1.2 `main.py` 去掉 `collab.start_loop()`：API 进程不再跑执行循环，只保留入队（`enqueue_run`）与查询路由；`routes/runs.py` 里 `collab.start_loop()` 的幂等调用一并移除
- [ ] 1.3 `start.ps1` 增加启动 worker 进程（与 API、前端并列）；worker 崩溃可独立重启
- [ ] 1.4 探针 `worker_split_probe`：API 进程重启期间，worker 领取并跑完一个 run，验证「API 停 ≠ 执行停」

## 2 — 进程树 containment（死进程不再写）
- [ ] 2.1 worker 起 CLI 子进程时纳入进程树管控：Windows 用 Job Object（`CREATE_BREAKAWAY_FROM_JOB` + AssignProcessToJobObject），POSIX 用进程组/`setsid`——worker 死则 CLI 子进程被 OS 连带清理
- [ ] 2.2 探针 `containment_probe`：强杀 worker 进程，验证其 CLI 子进程在约定时间内全部退出（无残留写库/写文件）

## 3 — 重启整批 orphan 判死（对标 Multica RecoverOrphanedTasksForRuntime）
- [ ] 3.1 worker 启动时，把非本代遗留的在跑 run（`status IN ('running','claimed')`）整批判死重投——一条 UPDATE 标 `failed` + 重入队（不逐个精细接管、不做 generation 交棒）
- [ ] 3.2 探针 `orphan_batch_recovery_probe`：模拟上一代 worker 残留在跑 run，新 worker 启动后整批判死、任务可被重新领取，无双执行

## 4 — claim 冗余安全带（为多 worker 铺路，单 worker 零行为变化）
- [ ] 4.1 `_claim_one` 的 UPDATE 加 `AND status='queued'` 条件（CAS 冗余）：rowcount=0 视为被抢返回 None（两个调用方 `_tick`/`_loop` 已处理 None）；单 worker 下纯冗余、零行为变化
- [ ] 4.2 探针 `claim_cas_noop_probe`：单 worker 下 claim 行为与改造前逐一致（不漏领、不重复）

## 5 — 回归
- [ ] 5.1 全量 QA 探针跑通（剥离后执行链路不回归）：入队→worker 领取→跑完→收尾→活动流
- [ ] 5.2 单实例重启纪律核对（对齐记忆 backend-restart-single-instance）：API 与 worker 各自单实例，重启前杀净对应端口/进程
