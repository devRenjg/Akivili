## Why

平滑重启（改代码不中断用户访问、不打断在跑 Agent）的**根因**是：Agent 执行的寿命 = 后端 API 进程的寿命。执行层（`collab.py` 的 `_loop`/`_claim_one`/`_process_one`/`_run_one` + CLI 子进程）与 HTTP/SSE/业务路由**绑在同一个 Python 进程**里，进程一重启，在跑的 CLI 子进程被连带杀死、`_process_one` 协程消失、收尾跑不到。

要打破这个绑定，第一步是**把执行层从 API 进程剥离成独立的 worker 进程**。这是「平滑重启 / session resume」整条线的**地基与前提**——只有执行层独立，API 才能随意重启而 Agent 继续跑；也只有独立进程 + 进程树 containment，后续才能像 Multica 那样用「进程隔离」换取大量 fencing/recovery 复杂度的简化。

**本 change 的定位（刻意最小）**：只做「Worker 剥离最小版」这一块，不铺开成大方案。它替代已废弃的 [platform-graceful-restart] 阶段 2——那套 21 轮 Review 的航母级方案（execution_edges 三类边表 + recovery 三表 + NULL 三态迁移 + 五重 fencing）已整体废弃（见其 proposal 顶部 DEPRECATED banner 与 `Papers/Multica减法校准-restart-resume落地前置结论.md`）。本 change 以 **Multica 生产实现的极简 daemon 模型**为蓝本重拟。

## What Changes

> 规划态，最小可行优先。落地前走探针验证。

- **执行层剥离为独立进程**：新增 `backend/worker.py`，把 `_loop`/`_claim_one`/`_process_one`/`_run_one` 及并发池、CLI 子进程管理搬到 worker 进程；API 进程（`main.py`）不再 `start_loop()`，只保留入队（`enqueue_run`）与查询。
- **进程树 containment**：worker 起的 CLI 子进程纳入进程树管控（Windows Job Object / POSIX 进程组），worker 死则其 CLI 子进程被连带清理——保证「死进程不会再写库/文件」，这是省 fencing 的物理前提（对标 Multica daemon）。
- **重启整批 orphan 判死**：worker 启动时，把上一代 worker 遗留的在跑 run（`running`/`claimed` 且非本代）整批判死重投（对标 Multica `RecoverOrphanedTasksForRuntime` 一条 SQL），不逐个精细接管。
- **API↔worker 解耦契约**：队列在 PG（`run_queue` 已在 DB），worker 通过 DB 领取；API 只写队列。二者可独立重启。

## 边界（本 change 明确不做）

- **不做** execution/attempt 一对多、execution_edges 边表、recovery_budgets/operations/requests 三表、NULL conversation 三态迁移机（均属已废弃的航母方案）。
- **不做** 多 worker 并发（先跑**单 worker**，多 worker 归后续 change）。
- **不做** session resume 本身（存 session_id/--resume 归独立的 session-resume 最小 change）。
- **不做** Nginx 蓝绿、连接层平滑（后续）。
- **原子 claim CAS**：单 worker 下非必须（单进程单事件循环串行 claim + 迁移 003 的 `uq_run_queue_active` 部分唯一索引已兜底），但**改造 `_claim_one` 时顺手加 `AND status='queued'` 条件 UPDATE 冗余安全带**（零行为变化，为将来多 worker 铺路）。

## 从废弃方案迁移过来、仍成立的结论

- attempt 级 fencing 与 message_seq 行锁水位是必需防线——但**本 change 不涉及**（无 attempt 层、无 resume），留给后续。
- 三项 Multica 已验证护栏（retired_session / Codex rollout 校验 / resume-unsafe 清单）——归 session-resume 最小 change，不在此。

## Impact

- 规划态，暂不改代码。落地涉及：`backend/worker.py`（新增）、`main.py`（去掉 `start_loop`）、`collab.py`（执行层搬迁 + `_claim_one` 加 CAS 条件）、`start.ps1`（多起一个 worker 进程）。
- 关联：替代已废弃 [platform-graceful-restart] 的阶段 2；为未来 session-resume、多 worker 提供进程隔离地基。
