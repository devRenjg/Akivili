# S0.2 行为回归基准（全绿清单）

> 跑通日期：2026-07-24 | 运行环境：`py -3.12` + `PYTHONUTF8=1`，工作目录 `backend/`
> 口径：以脚本实跑打印的 `N/N` 为准（TestReport/README.md 约定，不用 grep 静态计数）。
> 隔离性：全部脚本在临时 config/DB/workspace 下运行，monkeypatch `runner.execute_dispatch`，不碰真实 `jianagency.db`、不调真实 LLM/CLI。
> 已核验：本次回归运行期间 `backend/jianagency.db` mtime 未变（16:50，早于 19:16 的回归运行），确认零污染真实库。
> 用途：S1–S5 每个 `S*.V` 验收门「行为不回退」的对照基准——不新增红项、不降低已绿项通过数。

## 主套件

| 脚本 | 基线 N/N | exit | 跑通日期 |
|---|---|---|---|
| `run_qa_suite.py` | **31/31** | 0 | 2026-07-24 |

## 隔离 probe（24 个）

| 脚本 | 基线 N/N | exit | 跑通日期 |
|---|---|---|---|
| `run_agents_overview_probe.py` | 22/22 | 0 | 2026-07-24 |
| `run_concurrency_probe.py` | 7/7 | 0 | 2026-07-24 |
| `run_lineage_probe.py` | 13/13 | 0 | 2026-07-24 |
| `run_memory_hygiene_probe.py` | 11/11 | 0 | 2026-07-24 |
| `run_mention_chain_reset_probe.py` | 6/6 | 0 | 2026-07-24 |
| `run_mention_prompt_probe.py` | 13/13 | 0 | 2026-07-24 |
| `run_orphan_leak_probe.py` | 11/11 | 0 | 2026-07-24 |
| `run_orphan_reclaim_probe.py` | 13/13 | 0 | 2026-07-24 |
| `run_pipe_deadlock_probe.py` | 5/5 | 0 | 2026-07-24 |
| `run_rate_limit_probe.py` | 8/8 | 0 | 2026-07-24 |
| `run_reactivate_probe.py` | 5/5 | 0 | 2026-07-24 |
| `run_reflect_observability_probe.py` | 5/5 | 0 | 2026-07-24 |
| `run_reflect_participants_probe.py` | 4/4 | 0 | 2026-07-24 |
| `run_reflect_probe.py` | 8/8 | 0 | 2026-07-24 |
| `run_scheduling_events_probe.py` | 6/6 | 0 | 2026-07-24 |
| `run_scheduling_probe.py` | 10/10 | 0 | 2026-07-24 |
| `run_skill_downloadable_probe.py` | 7/7 | 0 | 2026-07-24 |
| `run_stale_pid_kill_probe.py` | 12/12 | 0 | 2026-07-24 |
| `run_stdout_display_probe.py` | 8/8 | 0 | 2026-07-24 |
| `run_subtask_autocomplete_probe.py` | 6/6 | 0 | 2026-07-24 |
| `run_task_gates_probe.py` | 10/10 | 0 | 2026-07-24 |
| `run_timeout_and_qa_probe.py` | 14/14 | 0 | 2026-07-24 |
| `run_wal_concurrency_probe.py` | 8/8 | 0 | 2026-07-27 |
| `run_migration_probe.py` | 15/15 | 0 | 2026-07-27 |

**隔离 probe 合计：227/227**（S1 新增 `run_wal_concurrency_probe.py` 8/8；S2 新增 `run_migration_probe.py` 15/15，2026-07-27）

## 不纳入基线（需真实 CLI 供应商，非隔离桩，`*`）

| 脚本 | 说明 |
|---|---|
| `run_collab_scenario.py` `*` | 真实 CLI 端到端协同场景（claude-cli/codex-cli 供应商），依赖外部，仅人工验收阶段按需单跑 |
| `run_codex_cli_smoke.py` `*` | Codex CLI 后端连通性烟测，单点非断言式 |

## 基线结论

- **S0 基线（2026-07-24）：主套件 31/31 + 隔离 probe 204/204 = 235/235，全绿，零已知红项。**
- **S1 后（2026-07-27）：主套件 31/31 + 隔离 probe 212/212 = 243/243**——S1 新增 `run_wal_concurrency_probe.py` 8/8（WAL/busy_timeout 生效 + 480 并发写零 locked + 收口护栏），其余 22 个 probe 数不变。
- **S2 后（2026-07-27）：主套件 31/31 + 隔离 probe 227/227 = 258/258**——S2 新增 `run_migration_probe.py` 15/15（空库 upgrade 逐字节对齐基线 + 版本 stamp + 幂等 + WAL + 存量库自动 stamp 不丢数据 + up/down 往返无损），其余 23 个 probe 数不变。
- 后续 S3–S5 每阶段 `S*.V` 验收门必须：主套件仍 31/31、24 个隔离 probe 各自不低于上表通过数、无脚本从绿转红。
- 若某阶段引入新 probe，追加到本清单并记录其基线 N/N。
- **S5 修订（2026-07-24，全仓零 sqlite）**：主套件基线由 31/31 降为 **29/29**——两条**机器速度依赖的延迟微基准**改为只采集 metrics（不卡阀值）：①「任务列表 p95<300ms」（本地 ~85ms、CI 冷启动超 300ms）②「假执行器 3 轮队列性能达标 <1000ms」（本地 ~950ms、CI 1087ms）。二者均在共享/冷启动 CI runner 上偶发超阈红门禁，属把微基准塞进 pass/fail 门的反模式；协同链路的**功能正确性**仍由 order 断言守护，未削弱。这是**主动降级 2 条时间墙断言**，非回归。同期退役 `run_wal_concurrency_probe`（WAL 随 sqlite 消亡）+ `run_orm_engine_probe`（PRAGMA/busy_timeout PG 无对应物），新建 `run_pg_concurrency_probe`；门禁探针数 40→39、全跑在 PostgreSQL 上（39/39·579 断言）。详见 `TestReport/README.md` 与 [[s5-postgres-only-progress]]。
