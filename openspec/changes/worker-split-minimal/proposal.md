## Why

平滑重启（改代码不中断用户访问、不打断在跑 Agent）的**根因**是：Agent 执行的寿命 = 后端 API 进程的寿命。执行层（`collab.py` 的 `_loop`/`_claim_one`/`_process_one`/`_run_one` + CLI 子进程）与 HTTP/SSE/业务路由**绑在同一个 Python 进程**里，进程一重启，在跑的 CLI 子进程被连带杀死、`_process_one` 协程消失、收尾跑不到。

要打破这个绑定，第一步是**把执行层从 API 进程剥离成独立的 worker 进程**。这是「平滑重启 / session resume」整条线的**地基与前提**——只有执行层独立，API 才能随意重启而 Agent 继续跑；也只有独立进程 + 进程树 containment，后续才能像 Multica 那样用「进程隔离」换取大量 fencing/recovery 复杂度的简化。

**本 change 的定位（刻意最小）**：只做「Worker 剥离最小版」这一块，不铺开成大方案。它替代已废弃的 [platform-graceful-restart] 阶段 2——那套 21 轮 Review 的航母级方案（execution_edges 三类边表 + recovery 三表 + NULL 三态迁移 + 五重 fencing）已整体废弃（见其 proposal 顶部 DEPRECATED banner 与 `Papers/Multica减法校准-restart-resume落地前置结论.md`）。本 change 以 **Multica 生产实现的极简 daemon 模型**为蓝本重拟。

## What Changes

> 规划态，最小可行优先。落地前走探针验证。

- **执行层剥离为独立进程（做法 A：只搬队列路径）**：新增 `backend/worker.py`，把**队列路径**的 `_loop`/`_claim_one`/`_process_one`/`_run_one` 及并发池、CLI 子进程管理搬到 worker 进程；API 进程（`main.py`）不再 `start_loop()`，只保留入队（`enqueue_run`）与查询。
- **SSE 直连对话路径暂留 API（做法 A 的有意折中）**：`routes/runs.py:110` 的 SSE `event_stream` 在 HTTP 请求协程内直接 `execute_dispatch` 起 CLI（「双执行入口」的第二条）。本 change **不搬它**——重启 API 仍会打断「此刻正在直连对话的那一次」，但后台协同 Agent（数量最多、跑最久的那批）已随队列路径迁出、不受影响。「API 进程彻底无执行、重启真正零打断」= 做法 B（直连也入队、SSE 从 `run_logs` 回放），**归后续独立 change**，不塞进最小版（避免滑向 Multica 那套 WS+Redis Stream relay 的完全体）。
- **进程树 containment**：worker 起的 CLI 子进程纳入进程树管控（Windows Job Object / POSIX 进程组），worker 死则其 CLI 子进程被连带清理——保证「死进程不会再写库/文件」，这是省 fencing 的物理前提（对标 Multica daemon）。
- **重启整批 orphan 判死**：worker 启动时，把上一代 worker 遗留的在跑 run（`running`/`claimed` 且非本代）整批判死重投（对标 Multica `RecoverOrphanedTasksForRuntime` 一条 SQL），不逐个精细接管。
- **API↔worker 解耦契约**：队列在 PG（`run_queue` 已在 DB），worker 通过 DB 领取；API 只写队列。二者可独立重启。

## 边界（本 change 明确不做）

- **不做** execution/attempt 一对多、execution_edges 边表、recovery_budgets/operations/requests 三表、NULL conversation 三态迁移机（均属已废弃的航母方案）。
- **不做** 多 worker 并发（先跑**单 worker**，多 worker 归后续 change）。
- **不做** session resume 本身（存 session_id/--resume 归独立的 session-resume 最小 change）。
- **不做** Nginx 蓝绿、连接层平滑（后续）。
- **多 worker 并发**：不做（本 change 单 worker）。但 claim CAS 冗余安全带（`_claim_one` 加 `AND status='queued'`）**提前到组 0 先做**——它独立、低风险，且是「新旧 worker 重启瞬间并存 / 未来多 worker」防重复执行的前置，应在引入第二个执行进程之前就位（详见 tasks.md 组 0 与执行顺序说明）。

## 从废弃方案迁移过来、仍成立的结论

- attempt 级 fencing 与 message_seq 行锁水位是必需防线——但**本 change 不涉及**（无 attempt 层、无 resume），留给后续。
- 三项 Multica 已验证护栏（retired_session / Codex rollout 校验 / resume-unsafe 清单）——归 session-resume 最小 change，不在此。

## 设计原则

- **状态经 DB 交接优先**：Python 相较 Go（Multica）的软肋之一是进程内共享状态（`_running` 集合、`_RUN_PIDS` 注册表）靠 GIL「碰巧原子」、跨进程不可见。剥离成双进程后，run 生命周期状态 SHALL 优先落 `run_queue`/`task_runs` 由 DB 表达，减少跨进程的进程内内存依赖——这既是 Python 软肋的对症解药，又天然对齐 Multica「控制面/执行面经 DB 分离」。
  - 例外：`_RUN_PIDS`（pid + 创建时间双因子防复用误杀）是 **worker 进程内**的 kill 机制，天然属于执行进程本地状态，本版**保持不动**、不强行下沉。
- **单实例前提**：本 change 仍是**单 worker**。多 worker 的原子领取由组 0 的 CAS 冗余安全带预留，但并发多 worker 本身不在本 change。

## Impact

- 规划态，暂不改代码。落地涉及：`backend/worker.py`（新增）、`main.py`（去掉 `start_loop`）、`collab.py`（执行层搬迁 + `_claim_one` 加 CAS 条件）、`start.ps1`（多起一个 worker 进程）。
- 关联：替代已废弃 [platform-graceful-restart] 的阶段 2；为未来 session-resume、多 worker 提供进程隔离地基。
