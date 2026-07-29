# Akivili 测试矩阵（TestReport）

平台核心路径的单测/探针集合，用来把关键行为钉死、防止改一处崩一处。

## 约定

- **每个新功能/修复必须追加或扩展对应探针**，覆盖核心路径 + 边界，然后回归现有套件。
- 探针命名 `run_<feature>_probe.py`，运行时打印 `N/N 通过` 计数。
- **本文件是测试矩阵索引，每次新增/改动探针都要同步更新**（清单、覆盖、实测 N/N）。
  通过数以**脚本实跑打印的 N/N 为准**，不用 grep 静态计数（辅助函数/循环会让计数虚高）。
- 提交信息 / 根 `README.md` 更新日志里写明各套件通过数（如「QA 29/29、concurrency 8/8」）。

## 隔离与安全

- 除 `run_collab_scenario.py`（需真实 CLI 供应商）外，所有探针在**临时 config/DB/workspace**
  下运行，monkeypatch `runner.execute_dispatch`，**不碰真实 `jianagency.db`、不调真实 LLM/CLI**。
- **入库白名单**：`.gitignore` 对 `TestReport/` 是 `TestReport/*` 全忽略 + 反选
  `run_*.py` / `cleanup_test_data.py` / `README.md`。运行产物（`qa_results_*`、
  `collab_scenario_*`、`weekly_report_*`、`shots/`，含真实内网地址与业务数据）**绝不入公开仓**。

## 运行方式

```bash
cd backend
# 单个探针（隔离，秒级~分钟级）
PYTHONUTF8=1 py -3.12 ../TestReport/run_scheduling_probe.py
# 主 QA 套件
PYTHONUTF8=1 py -3.12 ../TestReport/run_qa_suite.py
# 保留临时目录排查：加 --keep
```

### CI 门禁（一键全量）

```bash
# 跑全部 39 项门禁（38 隔离 probe + QA 主套件），任一失败即非零退出
python TestReport/run_ci_suite.py
python TestReport/run_ci_suite.py --list          # 只列清单不跑
python TestReport/run_ci_suite.py --exclude-slow  # 跳过并发/压力类长跑
```

- **GitHub Actions**（`.github/workflows/ci.yml`）：push 到 master + 所有 PR 自动跑
  `run_ci_suite.py`。runner 用 `windows-latest`（与开发环境一致）。S5 起门禁全量依赖 PG——
  workflow 先启动 runner **预装的 PostgreSQL**（默认停用，显式 `Start-Service postgresql*` +
  改 pg_hba 本地 trust + 建 akivili 超级用户/库），再跑门禁。
  > 注：目前**未开分支保护**——CI 红了在 Actions 页面可见，但不强制拦截合入。
  > ⚠️ CI 的 PG provisioning（预装 PG 启动/建库）尚未经真实 Actions 运行验证——首次 push 后需看
  > Actions 日志确认 `Start preinstalled PostgreSQL` 步骤跑通（windows-2025 镜像预装 PG 有已知启动
  > 问题 runner-images#13040，若启动失败按日志调整服务发现/PGDATA 定位）。
- probe 清单只在 `run_ci_suite.py` 的 `GATE` 里维护一处——新增 probe 时同步加入。
- 门禁**不含**需真实 CLI 的 `run_collab_scenario.py` / `run_codex_cli_smoke.py`（人工按需单跑）。
- 实测：**39/39 项、580 断言、~98s 全绿**（跑在 PostgreSQL 单引擎；2026-07-24 S5）。

## 全套测试 vs CI 门禁

**门禁是全套的子集**，不是两套东西。全套 = 仓库里所有测试脚本；门禁 = 其中能在
干净环境自动复现、被挑进 CI 每次自动跑的那批。

```
全套 44 个 run_*.py
├── run_ci_suite.py            ← 不是测试，是「调度器」(按 GATE 清单跑其余 39 个、收退出码)
├── 39 个 → 进 CI 门禁 ✅       (38 隔离 probe + run_qa_suite 主套件)
└── 4 个 → 不进门禁 ❌
      ├── run_collab_scenario.py   (需真实 claude/codex CLI + LLM)
      ├── run_codex_cli_smoke.py   (需真实 Codex CLI)
      ├── run_pg_e2e_probe.py      (需真实 PostgreSQL；数据底座 S4.6)
      └── run_pg_sqlite_consistency_probe.py  (需真实 PostgreSQL；数据底座 S4.5，一次性迁移一致性核验)
```

|  | 全套测试集合 | CI 39 门禁 |
|---|---|---|
| **范围** | 所有 `run_*.py`（44 个） | 其中挑进 `GATE` 的 39 个 |
| **触发** | 人工挑着单跑 / 本地 `run_ci_suite` 一键 | GitHub 每次 push/PR **自动** |
| **含真实外部依赖测试** | 含（4 个：2 CLI + 2 PG 专项） | 全部需 PG（S5 起门禁探针跑在 PostgreSQL 单引擎） |
| **保障对象** | 逻辑正确 + 与真实外部世界的集成 | 逻辑正确（鉴权/CRUD/ORM 等价/调度/回收…） |

**为什么分两层**（S5 起分界在「是否确定性可自动跑」——需真实 CLI/LLM 或一次性核验的留人工）：

- **进门禁**的 39 个：确定性桩测试，不接真实 CLI/LLM。S5 全仓零 sqlite 后，这批统一跑在
  **PostgreSQL 单引擎**上（每个探针用 `isolated_pg_db_url()` 建独立隔离库，跑完 atexit 删库），
  无 sqlite 回退。改一行代码撞坏 → 门禁立刻红。
- **留门禁外**的 4 个：2 个要真启动 claude/codex CLI、真调 LLM（依赖网络/凭证/CLI 安装，
  自动化跑不稳）；2 个是数据底座 S4 的一次性 PG 迁移一致性/端到端核验（`run_pg_e2e` /
  `run_pg_sqlite_consistency`，迁移窗口人工单跑）。

**新增测试时**：确定性桩 → 加进 `run_ci_suite.py` 的 `GATE`（会跑在 PG 隔离库上）；
依赖真实 CLI/LLM 或一次性核验 → 保持门禁外，并在下方矩阵用 `*` 标注。

> **GitHub Actions PG 收口（S5 Phase 3，已改 workflow）**：门禁探针全量依赖 PG。因保 Windows
> 开发环境一致（`services:` 容器仅 Linux runner 支持），`ci.yml` 改为在 `windows-latest` 上启动
> runner **预装的 PostgreSQL**：`Start-Service postgresql*` + 改 pg_hba 本地 trust + 建 akivili
> 超级用户/库 + 隔离库建删自检，再跑 39 门禁。**尚待首次真实 Actions 运行验证**（本地对 PG 容器已 39/39；
> 预装 PG 在 windows-2025 有已知启动问题 runner-images#13040，首跑看日志确认 provisioning 步骤跑通）。

## 测试矩阵

> 门禁实测通过数截至 2026-07-24（S5 全仓零 sqlite，39/39 跑在 PG）；部分业务探针计数为历史值。`*` = 需真实 CLI 供应商或一次性核验，非门禁隔离桩。

### 端到端主套件
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_qa_suite.py` | 29/29 | 平台主回归：登录鉴权、api_key 脱敏、路径穿越防护（`../secret`）、项目/任务 CRUD、看板列、任务系统、Agent 配置全链路（两条延迟微基准——任务列表 p95、假执行器 3 轮队列耗时——改为只采集 metrics 不卡阀值，避免 CI 硬件抖动弄红门禁；协同链路正确性仍由 order 断言守护） |

### 协同与调度（collab 层）
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_scheduling_probe.py` | 10/10 | 并发度/重试上限从 Settings 读取、优先级领取（high>medium>none）、同级 FIFO、退避、异常型重试到上限、超时/error 失败分类 |
| `run_scheduling_events_probe.py` | 6/6 | 调度流水埋点：enqueued/claimed/done 事件入 run_events、重试记 retry、失败记 failed+fail_reason=exception、流水独立于 activities（不污染成员动态） |
| `run_task_gates_probe.py` | 10/10 | 单任务运行双闸熔断：总量闸/循环闸从 Settings 生效、mention 链达上限拒入队（防 @ 死循环）、assign/人工介入打断链清零、人工直接@（source 留空）不误伤、总量闸放大后长程任务可持续入队 |
| `run_mention_chain_reset_probe.py` | 6/6 | 循环闸「产出即重置」口径（修 task149 误伤事故）：有产出的长链协作（每棒都落 jian comment/subtask）链长重置为 0 不被误掐、纯空转（无产出）mention 链仍累积到闸值如期熔断（保护不削弱）、混合链从最新往回数到最近一棒有产出即停、run 没起来（task_run_id 空）视为空转计入链长 |
| `run_rate_limit_probe.py` | 8/8 | 限流/429 观测：错误文本识别（429/rate limit/overloaded/quota/retry-after，不误伤普通错误）、限流 error 无产出归因 fail_reason=rate_limited、普通错误仍归 error_no_output、/runs/rate-limit-metrics 聚合窗口内 total/failed/rate_limited + 命中率 + 失败归因分布 |
| `run_mention_prompt_probe.py` | 11/11 | @ 触发把发言原话+任务上下文作为 prompt 传给成员（修 task140 事故：此前硬传空串→成员落「不要读任何文件」兜底模板收不到指令）、prompt 明示需要读文件/启动服务就正常做（去绝对禁令）、多人@各自拿到、_clip_history 历史回灌双限（条数+字符预算，至少留最新1条）从 Settings 生效 |
| `run_concurrency_probe.py` | 8/8 | 并发池：卡死 Agent 超时被 kill 不阻塞队列、并发不变式（peak≤MAX 安全属性恒真 + peak≥2 确有并行，不卡「恰好=3」这种硬件相关峰值）、3 worker 全完成、慢 Agent 不饿死快 Agent（并行度不再卡绝对墙钟阈值——避免 CI 硬件抖动假红） |
| `run_timeout_and_qa_probe.py` | 14/14 | 静默超时(A) + 宽限保成果(B) + 硬墙钟(C)、超时收尾验收路由 |
| `run_subtask_autocomplete_probe.py` | 6/6 | 子任务执行完自动进 done、全子完成→父任务 reviewing、失败任务不推进 |
| `run_reactivate_probe.py` | 5/5 | 重跑子任务时父任务状态即时回写 in_progress |
| `run_pg_concurrency_probe.py` | 4/4 | 数据底座 S5 并发正确性护栏（替退役的 S1 WAL 探针）：12写者×40轮=480 并发写不同行零丢失、20 并发原子 `UPDATE n=n+1` 行锁串行无丢更新（对照组 read-modify-write 观测丢 19/20，反证行锁必要）、10 并发 enqueue_run 同(task,agent)**恰好进 1 条**（迁移 003 的 uq_run_queue_active 部分唯一索引 + ON CONFLICT DO NOTHING 双层兜底 TOCTOU，从原「至少1条+告警」收紧为硬保证） |
| `run_migration_probe.py` | 15/15 | 数据底座 S2/S5 回归护栏（PG）：空库 alembic upgrade 建满18表、版本 stamp 到 head(003)、二次 upgrade 幂等(noop)、存量库(有表无version)自动 stamp 不重建不丢数据、002 数据规整(planning→backlog/archived→done/其它不动)、upgrade→downgrade base→upgrade 往返表数无损（改造中抓出并修复 001 downgrade 缺 PG 分支的真实缺陷：手写 _DROP_ORDER 违反 FK 依赖，PG 强制 FK 下 DependentObjectsStillExist；改为 drop_all 按依赖拓扑删）。003=run_queue (task_id,agent_slug) 活跃 run 部分唯一索引 |
| `run_orm_schema_parity_probe.py` | 75/75 | 数据底座 S3/S5（PG）：ORM 模型**声明**（Base.metadata 内省）↔ 迁移链建出的 **PG 实际 schema**（information_schema/Inspector）逐表逐列对齐——表集合、列名集、主键、外键(含 on_delete)、唯一约束。防迁移/模型/002 任一侧静默漂移 |
| `run_dialect_helper_probe.py` | 7/7 | 数据底座 S3.3/S5（PG-only）：方言 helper PG 分支正确——now_expr 编译为 to_char(UTC 秒级 text)、运行期写入格式 `YYYY-MM-DD HH:MM:SS` 与旧 datetime('now') 同形、timeutil.to_beijing 可解析(+8h)、elapsed_seconds PG 分支(EXTRACT EPOCH)算≈5秒、now_offset PG interval 形、insert_or_ignore PG on_conflict_do_nothing 幂等 |
| `run_collab_scenario.py` `*` | 12 断言 | 真实 CLI 端到端协同场景（claude-cli/codex-cli 供应商） |

### 记忆与反思（Agent 成长）
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_memory_hygiene_probe.py` | 11/11 | 近期动态只存净交付、Know-how 相关性精选 top-N、归属标记剥离、history 滑动窗口、反思质量门槛 |
| `run_reflect_probe.py` | 8/8 | 任务 done 触发经验反思、按角色写入 knowhow 托管段、超上限压缩合并保血缘、测试项目跳过 |
| `run_reflect_participants_probe.py` | 4/4 | 反思参与者口径 = 有 run ∪ 有本人发言：直接建卡型（无 run 有产出）成员也被纳入反思并沉淀 knowhow |
| `run_reflect_observability_probe.py` | 5/5 | 反思三类结果留痕：成功列汇总、无增量计数不报错、失败逐条留痕（错误类型+slug 可重跑）+ 汇总，杜绝失败被静默吞掉 |
| `run_lineage_probe.py` | 12/12 | 端到端链路关联键（run_queue.task_run_id 回填打通两表、messages.run_id 产出归因、@ 触发记 source_run_id/message_id 因果链、人工发起 source 留空）+ 链路下钻接口拼出 run 链（含 task_run_id 关联 + run_events 流水 + total_run_seconds 耗时聚合）+ 前端时间线视图字段契约（汇总/链路项/流水项渲染所需字段全锁定，防后端改动静默破坏 Runtime.vue） |

### 执行与运维健壮性
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_orphan_reclaim_probe.py` | 13/13 | 启动孤儿回收两层（run_queue→failed / task_runs→killed）、状态感知不误伤已完成任务、幂等 |
| `run_stale_pid_kill_probe.py` | 12/12 | 陈旧 pid 不得被 kill（task140 502 事故根因）：register_pid 存 (pid, 创建时间) 双因子指纹、kill_run 前校验身份（进程已退出/pid 被 OS 复用创建时间不符→拒杀，防 `taskkill /F /T` 误杀无辜进程树）、身份匹配的存活进程仍正常被杀、clear_pid 无条件清理（正常收尾路径挪出易抛异常的善后 try 块，杜绝 _persist_memory 异常导致陈旧 pid 残留）、拒杀后清登记 |
| `run_orphan_leak_probe.py` | 11/11 | 运行期孤儿泄漏防线（run#183/#185 泄漏事故）：`_finalize_if_running` 只在仍 running 时落终态（幂等，绝不覆盖 succeeded/killed）、execute_dispatch 生成器被中断兜底（客户端断连 aclose→抛 GeneratorExit 时补落终态再传播，不留 running 孤儿 + 清 pid）、运行期巡检 `sweep_orphan_task_runs`（扫 running 且最后日志静默超阈值的孤儿主动回收：未收尾任务→killed、已 done/reviewing→succeeded 保成果、新鲜在跑的不误杀、重复巡检幂等） |
| `run_stdout_display_probe.py` | 8/8 | CLI stdout 不落会话正文但进日志、无 jian 打标记、API 后端照落 |
| `run_pipe_deadlock_probe.py` | 5/5 | CLI 双管道死锁防护（run#243 事故根因）：`_StderrDrainer` 并发抽干 stderr——真实子进程狂写 stderr(~200KB 撑爆管道缓冲)+ 吐 stdout 时，用 drainer 后 stdout 完整读到/stderr 完整抽干/进程正常退出全程不挂起；对照组「读完 stdout 才读 stderr」在同负载下如期死锁（超时未完成，反证 bug 真实） |

### 能力包 / Skills
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_skill_downloadable_probe.py` | 7/7 | 「仅集成不下载」契约：downloadable=false 硬拦截 403、目录型 Skill 扫描 |
| `run_codex_cli_smoke.py` `*` | 冒烟 | Codex CLI 后端连通性烟测（单点，非断言式） |

### 数据底座 S4（需真实 PostgreSQL，门禁外 `*`）
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_pg_sqlite_consistency_probe.py` `*` | 37/37 | 数据底座 S4.5：SQLite⇄PG **逐行逐列全量一致性**校验（表集合+逐表行数+每行每列值，NUL 剔除/悬空外键排除同口径，主键唯一性保证无错位）。搬迁后跑，无 `AKIVILI_DB_URL` 指向 PG 时明确退出 |
| `run_pg_e2e_probe.py` `*` | 22/22 | 数据底座 S4.6：直连迁移后真实 PG 库跑端到端全链路（存量读 + 建项目/Agent/任务/enqueue/finalize/活动流 + 4 处方言查询 now_expr/now_offset/elapsed_seconds + 级联清理闭环），验证平台可运行在 PostgreSQL 上 |

> 运行前置：`AKIVILI_DB_URL=postgresql+asyncpg://…` 指向已建库+已迁移的 PG（见根 README「数据底座 S4」）。
> 注：S5 后**所有门禁探针都跑在 PG 上**，故「需 PG」已非留门禁外的理由；这两个是 S4 搬迁的**一次性核验**
> （逐行迁移一致性 / 迁移后端到端），只在迁移窗口人工单跑、不进每次 CI，故留门禁外。

### 工具（非测试）
| 脚本 | 说明 |
|---|---|
| `cleanup_test_data.py` | 真实库测试数据清理：测试项目精确 id 级联删、真实目录（Qlipoth/Agents）硬保护、删前自动备份 |
| `migrate_sqlite_to_pg.py`（在 `backend/`） | 数据底座 S4.5：SQLite→PG 全量数据迁移（只读源、依赖序插入、保留原始 id、NUL 剔除、悬空外键跳过、迁移后重置 PG 序列，`--truncate` 幂等重跑） |

## 覆盖盲区（尚无专项探针）

- 前端组件（目前仅靠 `npm run build` 编译把关）
- Paladin / 外部 MCP 集成
- 多项目跨项目并发写共享记忆（活跃 OpenSpec change `platform-concurrency-scaling` 要防的风险点）

