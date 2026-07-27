# S0.3 SQL 访问面清单

> 复核日期：2026-07-24（行号以当日 `master` 工作区为准，S1/S3 动手前若有漂移用 Grep 重刷）。
> 统计范围：`backend/**/*.py`，排除 `TestReport/`、`*_probe.py`、`test_*`。
> 用途：S1 连接收口 / S3 方言收敛的唯一勾稽表——收口/收敛完对照本表清零。

## 复核结论（与 s0-plan 前期估算的差异，已订正）

| 类别 | s0-plan 估算 | 本次复核实测 | 说明 |
|---|---|---|---|
| SQL 调用点 | 398 / 15 文件 | **254 / 19 文件** | 估算偏高；且前期漏登 4 个带 SQL 的文件（`routes/auth.py`、`auth.py`、`agents.py`、`agent_memory_sync.py`）。以本表为准。 |
| 旁路 `aiosqlite.connect` | 11 | **11**（其中 9 为待收口，2 为锚点/豁免） | 数量一致；下文区分「待收口 9」与「豁免 2」。 |
| `datetime('now')` | 41 | **41**（其中 1 为 docstring，实际代码站点 40） | 数量一致；`timeutil.py:1` 是模块 docstring 里的说明文字，非执行站点。 |
| `AUTOINCREMENT` | 17 | **17** | 完全一致，全部集中在 `database.py` 建表 DDL。 |

---

## 类别 A：SQL 调用点分布（254 / 19 文件）—— S3.4 分批迁移依据

按文件降序（`.execute` / `.executemany` / `.executescript`）：

- [ ] `database.py` — 38
- [ ] `collab.py` — 36
- [ ] `routes/runs.py` — 28
- [ ] `progress.py` — 21
- [ ] `routes/tasks.py` — 20
- [ ] `executor/runner.py` — 17
- [ ] `routes/agents.py` — 16
- [ ] `routes/project_agents.py` — 14
- [ ] `routes/agent_cli.py` — 12
- [ ] `routes/agent_config.py` — 8
- [ ] `reflect.py` — 8
- [ ] `activity.py` — 7
- [ ] `routes/skills.py` — 6
- [ ] `projects.py` — 6
- [ ] `skills.py` — 4
- [ ] `routes/auth.py` — 4
- [ ] `agents.py` — 4
- [ ] `auth.py` — 3
- [ ] `agent_memory_sync.py` — 2

**合计 254。** S3.4 的分批任务号（a–j）以此表为准；`routes/skills.py`(6) 对应 s0-plan 里写的 `skills(root)`，另有独立 `skills.py`(4)——两者是不同文件，S3.4j 需分别覆盖。

---

## 类别 B：旁路 `aiosqlite.connect`（11 处）—— S1 收口目标

### B1. 待收口（9 处，改走 `get_connection()`）

- [ ] `routes/auth.py:22`（S1.2）
- [ ] `routes/auth.py:40`（S1.2）
- [ ] `routes/auth.py:52`（S1.2）
- [ ] `auth.py:42`（S1.3）
- [ ] `auth.py:57`（S1.3）
- [ ] `skills.py:111`（S1.4）
- [ ] `skills.py:132`（S1.4）
- [ ] `agents.py:95`（S1.5）
- [ ] `agents.py:122`（S1.5）

### B2. 豁免 / 锚点（2 处，不消除）

- [x] `database.py:226` — `init_db()` 建库路径，**S1.6 明确保留**（init 路径不收口）。
- [x] `database.py:323` — `get_connection()` 工厂本体，**是 S1 收口的目标出口**，不是待消除的旁路。

> **S1.V 验收**：全库 `grep aiosqlite.connect` 仅剩上述 B2 两处（init + 工厂），B1 九处清零。

---

## 类别 C：`datetime('now')`（41 处）—— S3.3 方言收敛目标

按文件（`file:line`）：

- [ ] `database.py` — 19 处：`:27 :28 :42 :57 :65 :76 :85 :93 :102 :114 :121 :141 :142 :152 :163 :178 :184 :210 :318`（建表 DDL 默认值为主）
- [ ] `collab.py` — 4 处：`:839 :997 :1001 :1056`
- [ ] `routes/agent_config.py` — 4 处：`:70 :73 :91 :93`
- [ ] `progress.py` — 3 处：`:111 :130 :298`
- [ ] `routes/tasks.py` — 2 处：`:115 :154`
- [ ] `routes/agents.py` — 2 处：`:226 :229`
- [ ] `executor/runner.py` — 2 处：`:710 :725`
- [ ] `projects.py` — 1 处：`:53`
- [ ] `routes/runs.py` — 1 处：`:127`
- [ ] `routes/auth.py` — 1 处：`:28`
- [ ] `routes/agent_cli.py` — 1 处：`:214`
- [x] `timeutil.py:1` — **docstring 说明文字，非执行站点**，收敛时忽略（实际代码站点 40 处）。

> **S3.V 验收**：`grep datetime('now')` 在业务代码归零，统一走 ORM 时间表达式 / 单一 helper（`timeutil.py` 已是转换层锚点）。DDL 默认值随 S2 `001_baseline` 迁移 + S3 ORM 模型一并表达。

---

## 类别 D：`AUTOINCREMENT`（17 处）—— S3.3 主键策略覆盖目标

全部集中在 `database.py` 建表 DDL（`file:line`）：

- [ ] `database.py` — 17 处：`:21 :32 :46 :61 :69 :80 :89 :106 :118 :131 :146 :156 :171 :182 :193 :203 :316`

> **S3.V 验收**：改由 ORM 主键策略（`Integer primary_key=True` + 引擎自增）表达；PG 侧走 `IDENTITY`/序列，SQLite 侧保持 rowid 自增，方言差异由 ORM 吸收。

---

## 勾稽总表（供各 V 门核对）

| 类别 | 基线数 | S1.V 后应为 | S3.V 后应为 |
|---|---|---|---|
| B1 旁路待收口 | 9 | **0** | 0 |
| C datetime 代码站点 | 40 | 40（S1 不动） | **0** |
| D AUTOINCREMENT | 17 | 17（S1 不动） | **0** |
| A 手写 SQL 调用点 | 254 | 254（S1 不动） | **仅剩迁移文件** |
