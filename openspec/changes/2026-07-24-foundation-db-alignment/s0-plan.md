# S0 执行方案（基线与锚点）

> 状态：**方案已定稿，执行推迟（用户指示：过几天执行）**
> 前置说明：S0 全程 **零代码改动、零 schema 改动、零后端重启**，纯只读快照 + 生成文档。
> S0 完成后停下来交付三份基准文件，用户确认无误再开 S1.1（第一个改代码的任务）。

## 目的

给 S1–S5 立三个不可动摇的基准：
- **结构基准**（S0.1）：当前 SQLite 全表结构快照，S2 写 `001_baseline` 迁移时逐字段比对源。
- **行为基准**（S0.2）：现有 QA 套件全绿清单，S1–S5 每个 `S*.V` 验收门"行为不回退"的对照。
- **收敛清单基准**（S0.3）：SQL 访问面 checklist，S1 收口旁路 / S3 收敛方言的可追溯依据。

---

## S0.1 — 结构基线快照

### 做什么
1. 复制 `backend/jianagency.db` → `backend/jianagency.db.bak_baseline-<YYYYMMDD>` 作为数据兜底。
2. 导出当前全部表结构到 `openspec/changes/2026-07-24-foundation-db-alignment/baseline_schema.sql`（只导结构，不导数据）。

### 命名与 git 约束（已核对）
- 副本命名 **必须** 用 `jianagency.db.bak_baseline-<YYYYMMDD>`。
  - 已用 `git check-ignore` 验证：该名匹配 `.gitignore` 的 `*.db.bak*`，**被忽略、不入 git**。✓
  - ❌ 不要用 `jianagency.db.baseline-*`（这种会被 git 追踪，把 36MB 数据带进仓）。
- `baseline_schema.sql`（纯结构、无数据）**纳入 change 目录追踪**，作为 S2 比对源。

### 怎么做
无 sqlite3 CLI，用 `py -3.12` 连库遍历 `sqlite_master` 导出：

```bash
cd /c/Code/JianAgency/backend
# 1. 数据兜底副本（gitignore 已忽略）
cp jianagency.db "jianagency.db.bak_baseline-$(date +%Y%m%d)"

# 2. 导出结构快照（sqlite_master 里所有 table/index/trigger/view 的 sql）
PYTHONUTF8=1 py -3.12 - <<'PY'
import sqlite3, pathlib
db = sqlite3.connect("jianagency.db")
out = []
rows = db.execute(
    "SELECT type, name, sql FROM sqlite_master "
    "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
    "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"
).fetchall()
for typ, name, sql in rows:
    out.append(f"-- [{typ}] {name}\n{sql.strip()};\n")
dest = pathlib.Path("../openspec/changes/2026-07-24-foundation-db-alignment/baseline_schema.sql")
dest.write_text("\n".join(out), encoding="utf-8")
print(f"exported {len(rows)} objects -> {dest}")
db.close()
PY
```

### 验收
- `baseline_schema.sql` 生成成功。
- 导出的 `table` 数量与 `backend/database.py` 里 `CREATE TABLE` 声明数量核对一致（当前基线 ~41 表），**无漏表**。
- 运行期 `PRAGMA table_info` 兜底补的列，已体现在实际库结构里（因为快照取自真实库，天然包含）——导出后抽查 2~3 张历经补列的表，确认列齐。

---

## S0.2 — 行为回归基准

### 现状（已确认，无需新建）
QA 设施完备，直接复用：
- 主套件 `TestReport/run_qa_suite.py`（782 行，实测 31/31）。
- 25 个专项 probe（scheduling 10/10、task_gates 10/10、lineage 12/12、orphan_reclaim 13/13、stale_pid_kill 12/12、orphan_leak 11/11、pipe_deadlock 5/5 等，见 `TestReport/README.md`）。
- 全部隔离运行（临时 config/DB/workspace，monkeypatch `runner.execute_dispatch`），**不碰真实 `jianagency.db`、不调真实 LLM/CLI**。安全。
- `run_collab_scenario.py`、`run_codex_cli_smoke.py` 需真实 CLI 供应商（带 `*`），S0 基线 **不纳入**（非隔离、依赖外部），只在人工验收阶段按需单跑。

### 做什么
1. 跑主套件 + 全部隔离 probe，记录每个脚本的实测 `N/N`。
2. 汇总成"基线全绿清单"存档到 change 目录 `baseline_regression.md`。

### 怎么做
```bash
cd /c/Code/JianAgency/backend
PYTHONUTF8=1 py -3.12 ../TestReport/run_qa_suite.py           # 主套件
# 逐个隔离 probe（示例，全量见 checklist）
PYTHONUTF8=1 py -3.12 ../TestReport/run_scheduling_probe.py
# ... 其余隔离 probe 同理
```
> 运行产物 `qa_results_*` 落在 `TestReport/`，被 gitignore 忽略（白名单只放 `run_*.py`）。基线全绿清单我手写进 change 目录（只记脚本名 + N/N + 日期，不含业务数据）。

### 验收
- 产出 `baseline_regression.md`：逐脚本 `脚本名 | 基线 N/N | 跑通日期`。
- 以脚本实跑打印的 `N/N` 为准（README 约定，不用 grep 静态计数）。
- 若某 probe 基线就不绿：**记录为已知红项**，不在 S0 修（S0 不改代码）；S1–S5 验收门只要求"不新增红项 / 不降低已绿项通过数"。

---

## S0.3 — SQL 访问面清单

### 做什么
把已统计的迁移面固化成可勾选 checklist（存 change 目录 `sql_surface_checklist.md`），每项带 `file:line`，作为 S1/S3 收敛的可追溯依据。

### 覆盖四类（数字为前期统计，执行时用脚本复核刷新）
- **398 个 SQL 调用点**，15 文件分布：database.py 37 / collab.py 36 / routes/runs.py 28 / progress.py 21 / routes/tasks.py 20 / executor/runner.py 17 / routes/agents.py 16 / routes/project_agents.py 14 / routes/agent_cli.py 12 / agent_config.py 8 / reflect.py 8 / activity.py 7 / skills.py 6 / projects.py 6 / skills(root) 4。
- **11 处旁路 `aiosqlite.connect`**（S1 收口目标）：routes/auth.py 3 / skills.py 2 / database.py 2 / auth.py 2 / agents.py 2。
- **41 处 `datetime('now')`**（S3 方言收敛目标）。
- **17 处 `AUTOINCREMENT`**（S3 主键策略覆盖目标）。

### 怎么做
执行时用 Grep 复核刷新行号（前期数字可能因近几天改动漂移），生成带 `file:line` 的清单。清单按"S1 待收口 / S3 待收敛"分区，每项一个复选框。

### 验收
- `sql_surface_checklist.md` 生成，四类每项带 `file:line`。
- S1 收口完 → 对照"11 处旁路"清零。
- S3 收敛完 → 对照"41 + 17"清零。
- 这份清单是 S1.V / S3.V 验收"是否真的收敛干净"的唯一勾稽表。

---

## S0 整体产出与边界

| 产出文件 | 位置 | 入 git |
|---|---|---|
| `jianagency.db.bak_baseline-<日期>` | `backend/` | ❌（gitignore 忽略） |
| `baseline_schema.sql` | change 目录 | ✓ |
| `baseline_regression.md` | change 目录 | ✓ |
| `sql_surface_checklist.md` | change 目录 | ✓ |

**边界红线**：
- S0 零代码 / 零 schema / 零重启。只读快照 + 写文档。
- S0 不碰 8100 后端进程；不碰 8000/8088 无关服务；不碰 wk_*.txt。
- S0 做完 **停下**，交付三份基准文件给用户确认，再开 S1.1。
