# Worker 剥离最小版（做法 A）· 具体方案 + 对现有平台的收益与风险

> **定位**：restart/resume 线的第一块地基。把「执行层」从 API 进程剥离成独立 worker 进程，使改代码/重启 API 时后台协同的 Agent 不中断。对标 Multica 独立 daemon 的极简版。
> **做法 A（已定）**：只剥后台协同执行循环，不引 Redis、不请回废弃的 event_seq 重表。SSE 实时流暂不动（留独立后续 change）。
> **依据**：基于 `backend/collab.py`、`executor/runner.py`、`routes/runs.py`、`main.py` 现状实读。

---

## 0. 一句话

把 `collab.py` 的执行循环（`_loop`/`_claim_one`/`_process_one`/`_run_one` + CLI 子进程管理）从 API 进程搬到独立 `worker.py` 进程，API 只入队。**收益**：改代码重启 API 时后台 Agent 不中断（覆盖平台绝大多数执行）。**主要风险**：状态跨进程后，kill、SSE、孤儿回收三处依赖「执行在 API 进程内」的假设需要重新接线。

---

## 1. 现状（为什么能剥、剥的边界在哪）

实读四个文件后的关键事实：

| 现状事实 | 位置 | 对剥离的含义 |
|---|---|---|
| 执行循环 `_loop`/`_claim_one`/`_process_one`/`_run_one` 全在 collab.py，由 `main.py` startup 的 `start_loop()` 在 **API 进程内**跑 | collab.py:934/823/855/577；main.py | 这就是要搬走的整块 |
| 并发池状态 `_running`（set）、CLI 子进程 PID `_RUN_PIDS`（dict）都在**进程内存** | collab.py:801；runner.py:25 | 搬去 worker 后天然跟着 worker；API 不再持有 |
| CLI 子进程由 worker 侧 `subprocess.Popen` 起、PID 存内存、`kill_run` 用 taskkill | runner.py:80/159 | kill 能力必须跟到 worker（PID 在哪，kill 在哪） |
| **SSE 实时流是 `@分派` 请求直连执行生成器 yield**（不读 DB 回放） | runs.py:110 event_stream | 🔴 这条路径**不搬**（做法 A 边界）——它跑在 API 进程，靠 HTTP 请求驱动 |
| 入队 `enqueue_run` 写 `run_queue`（DB）；队列本就在 DB | collab.py:370 | API↔worker 通过 DB 队列解耦，天然可行 |
| 重启回收 `reclaim_orphan_runs` 在 API startup 跑，把遗留 running 判死 | main.py:102；collab.py:955 | 要挪到 worker startup（判死的是 worker 的遗留，不是 API 的） |
| 运行期孤儿巡检 `_orphan_sweep_loop` | collab.py:1105 | 跟执行走，挪去 worker |

**两条执行路径的区分（做法 A 的核心边界）**：
- **路径①「后台自动协同」**：Agent 互相 @、Leader 派活、子任务流转 → 走 `_loop` 领队列 → `_process_one` → `_run_one`。**占平台绝大多数执行。本 change 剥这条。**
- **路径②「前端 @分派实时流」**：用户在前端点分派、看 SSE 实时输出 → `runs.py` event_stream 直连生成器。**本 change 不剥**（剥它要改造成读 DB/中继回放 = 请回废弃复杂度）。重启 API 会断这条实时流，但重连后能从 DB（run_logs/activities）看到完整结果，非数据丢失。

---

## 2. 具体怎么做（4 步，每步可独立验证）

### 步骤 1 — 执行层搬到 `backend/worker.py`
- 新建 `worker.py` 作为独立进程入口：一个 `asyncio` 事件循环 + `collab._loop()` 的并发池。
- `_loop`/`_claim_one`/`_process_one`/`_run_one`/`_apply_settings` 这些**逻辑不重写**，保留在 collab.py（worker 和 API 都 import collab），只是**由 worker 进程调用 `start_loop()`**。
- `main.py` startup 里删掉 `collab.start_loop()`；`routes/runs.py:108` 那个幂等 `collab.start_loop()` 也删（API 不再起循环）。
- **验证**：worker 单独启动能领队列跑完 run；API 单独启动不跑执行循环（`_running` 恒空）。

### 步骤 2 — 进程树 containment（死 worker 连带清死 CLI 子进程）
- worker 起 CLI 子进程时纳入进程树：Windows 用 **Job Object**（把子进程 assign 进 job，worker 死 job 关、子进程连带被杀）；POSIX 用**进程组 / setsid**。
- 现状 `_RUN_PIDS` + `kill_run(taskkill /F /T)` 已有「杀进程树」能力，但依赖 worker 活着主动调；containment 是**被动兜底**——worker 崩了也能靠 OS 清理。
- **验证**：强杀 worker，其 CLI 子进程在约定时间内全退，无残留写 DB/文件。

### 步骤 3 — 重启整批 orphan 判死（对标 Multica RecoverOrphanedTasksForRuntime）
- 把 `reclaim_orphan_runs()` 从 API startup 挪到 **worker startup**：worker 启动时把遗留的 `running` run 整批判死重投（一条 UPDATE，不逐个精细接管）。
- API startup 不再调它（API 不再拥有执行、没有「自己的孤儿」）。
- **验证**：模拟上一代 worker 残留 running run，新 worker 启动整批判死、可重新领取、无双执行。

### 步骤 4 — `_claim_one` 加 CAS 冗余安全带
- UPDATE 加 `AND status='queued'`：`sa_update(RunQueue).where(id==x, status=='queued')`，rowcount=0 视为被抢返回 None。
- 单 worker 下纯冗余（零行为变化），为将来多 worker 铺路。两个调用方 `_tick`/`_loop` 已处理 None，安全。
- **验证**：单 worker 领取行为与改造前逐一致，不漏不重。

### 部署（start.ps1）
- 现状：start.ps1 起 API（`py main.py`）+ 前端。
- 改后：起 API + **worker（`py worker.py`）** + 前端，三个进程。
- 记忆 `backend-restart-single-instance` 纪律升级为「**API 单实例 + worker 单实例**」——重启 API 时只杀 API 进程（8100），worker 不动、继续跑 Agent；重启 worker 时只杀 worker。这正是剥离要的效果。

---

## 3. 对现有平台的收益

| # | 收益 | 说明 | 量级 |
|---|---|---|---|
| R1 | **改代码重启 API，后台 Agent 不中断** | 平台最高频诉求。改 API/业务代码重启 8100 时，worker 里跑着的多 Agent 协同、Leader 派活、子任务流转全不受影响，跑完正常落库 | 🟢 覆盖绝大多数执行 |
| R2 | **API 与执行的故障隔离** | API 崩溃/OOM 不再连带杀死在跑 Agent；反之 worker 卡死也不影响 API 响应用户查询 | 🟢 稳定性提升 |
| R3 | **进程隔离 = 未来简化的地基** | 有了独立 worker + containment，后续「砍 fencing、简化 recovery」才有物理依据（对标 Multica 用进程隔离换简化）。这是 restart/resume 整条线的前提 | 🟢 战略地基 |
| R4 | **重启回收归位、职责清晰** | 孤儿回收从「API 启动扫」改为「worker 启动扫自己的遗留」，语义更正确（API 本无执行、不该管孤儿） | 🟡 可维护性 |
| R5 | **多 worker 的前置就绪** | CAS 冗余安全带 + 状态外置意识，为将来多 worker 并发（规模化）铺好第一块 | 🟡 可扩展性 |

**没拿到的收益（做法 A 的边界，诚实说明）**：
- 用户正看某次「前端 @分派」的**实时流式输出**、恰好撞上 API 重启 → 实时流会断（重连从 DB 看结果，非数据丢失）。这是路径②，本 change 不覆盖，留后续。

---

## 4. 对现有平台的风险（逐项 + 缓解）

进程剥离的本质风险 = **原来靠「执行在 API 进程内、共享内存」成立的假设，跨进程后会断**。逐一排查：

| # | 风险 | 根因 | 影响 | 缓解 |
|---|---|---|---|---|
| K1 | **kill_run 失效** | `_RUN_PIDS` 在 worker 内存；用户在前端点「kill」走 API 进程，API 内存里没有 PID | 点 kill 杀不掉正在 worker 跑的 CLI | kill 请求要跨进程送达 worker：① API 写一个「kill 意图」到 DB（run_queue 加标志），worker 循环里检查并执行 kill；或 ② API→worker 轻量 IPC。**做法 A 优先方案①**（DB 标志，无新依赖） |
| K2 | **SSE 实时流断**（路径②） | event_stream 在 API 进程直连生成器；执行搬走后，API 进程内已无该生成器 | 前端 @分派看实时输出的路径需要重新接线 | ⚠️ **这是做法 A 的已知边界**：路径② 暂**保留在 API 进程**（@分派时 API 仍可临时起执行并流式——即路径①②暂时并存两套执行入口），或前端 @分派改为「入队 + 轮询/从 DB 读」。**需在步骤 1 明确：@分派走哪条**。这是本方案最大的待定细节（见第 5 节） |
| K3 | **双进程都连 PG，连接数翻倍** | API + worker 各自一个连接池 | PG 连接数上升 | S5b 已把连接池调优+可配（pool_size 默认 5）；worker 池可单独设小。总量可控 |
| K4 | **迁移/建表竞争** | API 和 worker 都可能跑 `run_migrations` | 两进程并发迁移损坏 alembic_version | S5b 已加 `pg_advisory_lock`——谁先拿锁谁迁，另一个等锁。**已解决** |
| K5 | **worker 崩溃无人拉起** | worker 独立进程，崩了 API 不知道 | Agent 停止执行但 API 正常，易被忽略 | start.ps1/部署加 worker 存活监控 + 自动重启；API 加一个「worker 心跳」健康检查（worker 定期写 DB 时间戳，API health 暴露） |
| K6 | **配置/代码不一致** | worker 和 API 是两个进程，改了代码只重启一个 | 行为分叉 | 重启纪律：改执行层代码重启 worker；改 API 层重启 API；改共享（collab/config）两个都重启。文档化 |
| K7 | **孤儿判死误伤**（跨代） | worker 重启时把「上一代遗留 running」判死，若判据不准会误杀正在跑的 | 正常 run 被误判死 | 判据要严：只判「无 worker 认领 + 静默超阈值」的 running（现 `reclaim_orphan_runs` 逻辑已是「startup 时 _running 必空 → 所有 running 皆无主」，单 worker 下成立）。**单 worker 前提下低风险**；多 worker 才需要 generation |

**风险总评**：K2（SSE）是唯一的**设计级待定**，其余都有现成缓解（K3/K4 已被 S5b 解决，K1/K5/K6/K7 是工程接线）。K2 决定「@分派实时流」这条路怎么走，是方案落地前必须和你敲定的点。

---

## 5. 落地前唯一待定：@分派实时流（路径②）怎么处理

执行剥离到 worker 后，前端「@分派 + 看实时 SSE 输出」这条路径有三种处理，请你定：

- **选项 α（最小改动）**：路径② 保留在 API 进程——@分派时 API 仍能临时起一次执行并 SSE 流式（即 API 也保留起 CLI 的能力，仅「后台自动协同」这条搬去 worker）。**代价**：两套执行入口并存，collab 逻辑要能被两个进程调用（本就 import 共享，可行），但「API 重启断该实时流」依旧。
- **选项 β（统一入队）**：前端 @分派改为「只入队，不在请求里跑」——分派后前端轮询/从 DB（run_logs）读进度，worker 统一执行。**收益**：执行入口唯一（全在 worker）、最干净；**代价**：前端 @分派的「实时流式」体验退化为「近实时轮询」。
- **选项 γ（后续再做 A+中继）**：先按 α 最小改动上线，把「实时流不断」留给未来独立 change（那时可评估 Redis 或 PG 轻量回放）。

**建议 γ**：本 change 保持最小——先按 α 让路径② 不动（继续在 API），把路径① 剥到 worker 拿到 90% 收益；实时流不断作为独立后续 change。这样本 change 不碰 SSE 改造，风险最小、最快见效。

---

## 6. 落地策略与验证

1. **分步落、每步探针**（对应 tasks.md 1~5 组）：搬迁 → containment → orphan 判死 → CAS → 回归。验证门不过不推进。
2. **可回退**：每步独立；最坏情况回退到「API 内跑 start_loop」的现状（把删掉的 start_loop 加回，worker 不启）。
3. **重启纪律升级**：API 单实例 + worker 单实例，分别管理（对齐并升级记忆 backend-restart-single-instance）。
4. **不引新依赖、不动 schema**（除 K1 若走 DB 标志方案需 run_queue 加一列 kill 标志）、不请回废弃复杂度。

---

## 7. 结论

- **怎么做**：4 步搬迁（执行层→worker.py / containment / orphan 判死归位 / claim CAS），start.ps1 多起一个 worker。逻辑不重写、只搬位置 + 接线。
- **收益**：改代码重启 API，后台 Agent 不中断（R1，最高频诉求）+ 故障隔离（R2）+ 未来简化地基（R3）。
- **风险**：K2（SSE 实时流）是唯一设计级待定，建议按选项 γ 先不碰、留后续；K3/K4 已被 S5b 解决；其余是工程接线。
- **最小、可回退、不背新依赖**——符合「废弃航母、从最小可行重拟」的方向。

---

*本方案对应 OpenSpec change `worker-split-minimal`。落地前需定第 5 节的 @分派处理选项（建议 γ）。*


