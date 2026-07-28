# CI 集成方案 · 把回归测试网接入自动化

**目标**：把现有 35+ 隔离 probe + QA 套件（合计 1059+ 断言）从「手动跑」升级为「每次
push/PR 自动跑」。远端是 GitHub（`github.com/devRenjg/Akivili`），用 **GitHub Actions**。

## 现状（已核查）

- 无任何 CI 配置（`.github/workflows/` 不存在）。
- 所有隔离 probe 均有失败退出码（`sys.exit(0 if passed==total else 1)` 或 `SystemExit`），
  天然适配 CI 门禁。
- **仅 2 个需真实 CLI、必须排除**：`run_collab_scenario.py`、`run_codex_cli_smoke.py`
  （baseline_regression 里标 `*`，依赖外部 claude/codex CLI，非隔离桩）。
- probe 全在临时 config/DB/workspace 下跑，monkeypatch `runner.execute_dispatch`，
  **不碰真实库、不调真实 LLM**——CI 环境安全。
- probe 需 `PYTHONUTF8=1`（含中文断言）；cwd 需在 `backend/`（probe 用 `from run_qa_suite import`
  且 `sys.path` 注入 backend）。
- 依赖仅 `backend/requirements.txt`（fastapi/uvicorn/pydantic-settings/aiosqlite/httpx/
  alembic/SQLAlchemy），无重外部服务。Python 3.12。
- `.gitignore` 对 `TestReport/` 是**白名单**：只 `run_*.py`/`cleanup_test_data.py`/`README.md`
  入仓 → 聚合 runner 必须命名 `run_*.py` 才能提交。

## 改动清单

### 1) 新增聚合 runner `TestReport/run_ci_suite.py`
单一入口，被 CI 和本地共用（避免 probe 清单在 yaml 和脚本里两处维护）：
- 显式列出**门禁 probe 清单**（35 个隔离 probe + QA 套件），**硬编码排除** 2 个真实-CLI probe。
- 逐个 `subprocess` 跑（`py -3.12`，注入 `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`，cwd=backend），
  解析退出码 + 抓打印的 `N/N` 计数。
- 汇总：打印每个 probe 的 PASS/FAIL + 总断言数；**任一 probe 非零退出 → 整体 `sys.exit(1)`**。
- `--list` 只列清单不跑；`--exclude-slow` 可选跳过并发/压力类长跑 probe（CI 快速门用）。
- 清单**注释标注**每个 probe 覆盖域，与 `TestReport/README.md` 矩阵对齐。

### 2) 新增 `.github/workflows/ci.yml`
- 触发：`push` 到 master + 所有 `pull_request`。
- **runner: `windows-latest`**（与开发环境 Win11 一致，零跨平台风险——probe 里任何
  Windows 假设如 `taskkill`/`netstat`/路径都不会翻车）。
- 步骤：checkout → `actions/setup-python@v5`(3.12, 内建 pip cache) →
  `pip install -r backend/requirements.txt` → `python TestReport/run_ci_suite.py`。
- 环境变量：`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`（中文断言 + Windows GBK 控制台避坑）。
- 不上传产物（qa_results 含内网数据，且已 gitignore）；失败时 runner 打印足够定位。

### 3) 更新 `TestReport/README.md`
- 「运行方式」加一节：`python TestReport/run_ci_suite.py`（本地一键全量）。
- 说明 CI 门禁 = 该 runner，PR 必须绿。

### 4)（可选）README 徽章
根 `README.md` 顶部加 CI status badge，一眼见主干健康。**默认不做**，除非你要。

## 验证（本步自检）

1. 本地跑 `python TestReport/run_ci_suite.py` → 复现 1059+ PASS、退出码 0。
2. 故意让一个 probe 失败（临时改断言）→ runner 退出码 1、汇总标红 → 还原。
3. `--list` 输出的清单与 baseline_regression + README 矩阵一致（不漏不多）。
4. yaml 用 `act` 或直接 push 到一个测试分支触发 Actions 验证（需你确认是否要真触发一次）。

## 风险与决策点

- **runner OS 已定：`windows-latest`**（与开发环境一致，零跨平台风险）。代价是 Actions
  计费 2x、runner 略慢——可接受。
- **风险 · 首次 push 触发**：合并 workflow 后第一次 push 会真跑 Actions。无副作用
  （只读测试、不碰真实库），但会消耗 Actions 额度。是否真触发一次由你定（见验证步骤 4）。
- **前置验证**：本地先跑 `run_ci_suite.py` 全绿，确认清单完整 + 无遗漏 probe，再落 yaml。

## 与 S3/S4 的关系

本 CI 是**独立工程化改进**，不属 S3 数据底座范畴，也不阻塞 S4。建议**单独提交**
（`ci:` 前缀），不混进 S3 的回滚锚点。落 master 后，S4 起所有改动自动受 CI 保护。
