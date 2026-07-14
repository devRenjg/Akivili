# Akivili 测试矩阵（TestReport）

平台核心路径的单测/探针集合，用来把关键行为钉死、防止改一处崩一处。

## 约定

- **每个新功能/修复必须追加或扩展对应探针**，覆盖核心路径 + 边界，然后回归现有套件。
- 探针命名 `run_<feature>_probe.py`，运行时打印 `N/N 通过` 计数。
- **本文件是测试矩阵索引，每次新增/改动探针都要同步更新**（清单、覆盖、实测 N/N）。
  通过数以**脚本实跑打印的 N/N 为准**，不用 grep 静态计数（辅助函数/循环会让计数虚高）。
- 提交信息 / 根 `README.md` 更新日志里写明各套件通过数（如「QA 31/31、concurrency 7/7」）。

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

## 测试矩阵

> 实测通过数截至 2026-07-08。`*` = 需真实 CLI 供应商，非隔离桩。

### 端到端主套件
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_qa_suite.py` | 31/31 | 平台主回归：登录鉴权、api_key 脱敏、路径穿越防护（`../secret`）、项目/任务 CRUD、看板列、任务系统、Agent 配置全链路 |

### 协同与调度（collab 层）
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_scheduling_probe.py` | 10/10 | 并发度/重试上限从 Settings 读取、优先级领取（high>medium>none）、同级 FIFO、退避、异常型重试到上限、超时/error 失败分类 |
| `run_scheduling_events_probe.py` | 6/6 | 调度流水埋点：enqueued/claimed/done 事件入 run_events、重试记 retry、失败记 failed+fail_reason=exception、流水独立于 activities（不污染成员动态） |
| `run_task_gates_probe.py` | 10/10 | 单任务运行双闸熔断：总量闸/循环闸从 Settings 生效、mention 链达上限拒入队（防 @ 死循环）、assign/人工介入打断链清零、人工直接@（source 留空）不误伤、总量闸放大后长程任务可持续入队 |
| `run_concurrency_probe.py` | 7/7 | 并发池 MAX_CONCURRENCY 并行度、卡死 Agent 超时被 kill 不阻塞队列、慢 Agent 不饿死快 Agent |
| `run_timeout_and_qa_probe.py` | 14/14 | 静默超时(A) + 宽限保成果(B) + 硬墙钟(C)、超时收尾验收路由 |
| `run_subtask_autocomplete_probe.py` | 6/6 | 子任务执行完自动进 done、全子完成→父任务 reviewing、失败任务不推进 |
| `run_reactivate_probe.py` | 5/5 | 重跑子任务时父任务状态即时回写 in_progress |
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
| `run_stdout_display_probe.py` | 8/8 | CLI stdout 不落会话正文但进日志、无 jian 打标记、API 后端照落 |

### 能力包 / Skills
| 脚本 | 实测 | 覆盖 |
|---|---|---|
| `run_skill_downloadable_probe.py` | 7/7 | 「仅集成不下载」契约：downloadable=false 硬拦截 403、目录型 Skill 扫描 |
| `run_codex_cli_smoke.py` `*` | 冒烟 | Codex CLI 后端连通性烟测（单点，非断言式） |

### 工具（非测试）
| 脚本 | 说明 |
|---|---|
| `cleanup_test_data.py` | 真实库测试数据清理：测试项目精确 id 级联删、真实目录（Qlipoth/Agents）硬保护、删前自动备份 |

## 覆盖盲区（尚无专项探针）

- 前端组件（目前仅靠 `npm run build` 编译把关）
- Paladin / 外部 MCP 集成
- 多项目跨项目并发写共享记忆（活跃 OpenSpec change `platform-concurrency-scaling` 要防的风险点）

