# CLI Resume 能力实测：claude 与 codex

> **定位**：Session Resume 评估的**物理前提取证**。在为新的最小 `agent-session-resume` change 定稿前，用真实 CLI 跑通「建会话 → 拿 session id → resume 续跑」全链路，坐实两个 CLI 到底支不支持 resume、以什么语法、输出格式变不变。本文是**实测证据文档**，结论可复跑（探针脚本随仓）。
>
> **上游依据**：`Papers/Multica减法校准-restart-resume落地前置结论.md`（决策快照，标记旧「航母级」spec 废弃、以 Multica 为蓝本从最小可行重拟）。本文补齐该文未覆盖的一环——**我们自己的 CLI 实际行为**（Multica 用的是同族 CLI，但版本/平台/调用模式与我们不同，不能照搬其结论，必须自测）。
>
> **实测环境**：Windows 11 / claude 2.1.197 / codex-cli 0.144.1 / 均已登录（claude OAuth、codex API key）。
>
> **探针脚本（回归资产，随仓）**：`TestReport/run_claude_resume_probe.py`、`TestReport/run_codex_resume_probe.py`。产物 `*_run{1,2}.jsonl` / `*_report.txt` 按 TestReport 白名单规则不入库。
>
> **日期**：2026-07-24。

---

## 0. 一句话结论

两个 CLI **均原生支持 resume、无需任何改造或升级**，且实测结果比旧废弃 spec 的假设**更省**：claude 可预分配 session-id（连抓取都省）、codex `exec resume` 子命令直接可用（**不需要** app-server + JSON-RPC 重集成）、两者 resume 后 stream 输出格式与首轮完全一致（`_parse_line` 原样复用）、session id 全程不 fork（省覆盖逻辑）。**新 change 的执行层改动 100% 在我们平台侧，且两条 CLI 线均走轻路径。**

---

## 1. 两个 CLI 的实测事实对照

| 维度 | claude 2.1.197 | codex-cli 0.144.1 |
|------|----------------|-------------------|
| session id 获取方式 | ✅ **预分配** `--session-id <uuid>`（我们生成 UUID 传入，CLI 回显同一值） | CLI 自己生成，从首轮 `thread.started` 事件的 `thread_id` 字段**抓取** |
| resume 命令构造 | `-p --output-format stream-json --resume <uuid>` | `exec resume [OPTIONS] <session_id> -`（prompt 走 stdin） |
| resume 是否真续上下文 | ✅ 复述出首轮暗号 | ✅ 复述出首轮暗号 |
| stream 输出 schema 与首轮一致 | ✅ `{assistant, result, system}`（+text/message 内嵌）两轮相同 | ✅ `{item.completed/agent_message, thread.started, turn.started, turn.completed}` 两轮相同 |
| `_parse_line` 能否原样解析 resume 输出 | ✅ 能（顶层 type 未变） | ✅ 能（item.* schema 未变） |
| resume 后 session id 是否 fork | ✅ 不变（仍是预分配 UUID） | 不变（仍是原 thread_id） |
| 独有前置条件 | 两轮必须同一 `CLAUDE_CONFIG_DIR`（会话记录存该目录） | resume 从 session 自身恢复 cwd，**不接受 `--cd`** 参数 |

---

## 2. 关键实测细节（含踩坑）

### 2.1 claude —— 预分配 session-id 是最优路径

- **首轮**：`claude -p --output-format stream-json --session-id a1b2c3d4-0000-4000-8000-abcdef123456 ...`，退出码 0，stream-json 每行回显 `"session_id":"a1b2c3d4-..."` —— **正是我们传入的 UUID**。
- **第二轮**：同一 `CLAUDE_CONFIG_DIR` 下 `--resume <同一 uuid>`，正确复述首轮暗号「青龙七号玄武」，session_id 仍为该 UUID（不 fork）。
- **含义**：claude 侧我们可以**完全跳过「从输出解析 session_id」**——自己生成 UUID、自己记录、resume 时带上。比旧 spec S1.3「从 system/result 行提取 session_id」更省一步。

### 2.2 claude —— 隔离 config dir 是唯一硬前置

- backend `executor/claude_code.py:76` 把子进程 `CLAUDE_CONFIG_DIR` 指向隔离目录 `%TEMP%/akivili_claude_cfg`（固定路径，非随机）。会话记录（供 resume）就存在该目录。
- 实测证明：**只要两轮同一 config dir，隔离目录与 resume 不冲突**。反之若该目录丢失（worker 换机/换用户/清 TEMP），resume 会 miss → 必须走降级全量。这坐实了「降级链」的真实触发条件。

### 2.3 codex —— exec resume 子命令直接可用，但参数集不同于 exec

- **踩坑**：初次把 `codex exec` 的 `--cd <workdir>` 也带进 `exec resume`，被 CLI 拒绝：`error: unexpected argument '--cd' found`。
- **正解**：`codex exec resume [OPTIONS] <session_id> -`，OPTIONS（`--json` / bypass / skip-git）放在 SESSION_ID **之前**，且**不带 `--cd`**（resume 从 session 自身恢复 cwd）。
- 修正参数后退出码 0，正确复述首轮暗号，输出 schema 与首轮完全一致。

### 2.4 codex —— session id 在 thread.started 事件

- 首轮 `codex exec --json` 的第一条事件 `{"type":"thread.started","thread_id":"019fd0d6-..."}` —— `thread_id` 即 codex 的 session id。
- backend `executor/codex.py::_parse_line` 当前**忽略**该事件（顶层 type 非 item.* 走兜底返回 None）。接入 resume 需新增：从 `thread.started` 抓 `thread_id` 冒泡存库。

---

## 3. 对旧废弃 spec 假设的校准

旧 `agent-session-resume` change（21 轮 Review、已废弃）里几条与 CLI 相关的设计假设，被实测**推翻或简化**：

| 旧 spec 条目 | 旧假设 | 实测校准 | 净效果 |
|---|---|---|---|
| S1.3 | claude 从 `system`/`result` 行**解析** session_id | claude 可**预分配** `--session-id`，无需解析 | 🟢 更省——去掉解析逻辑 |
| S2.6 | resume 后 emitted id 可能 ≠ 请求 id（fork），需覆盖存储 + mismatch 判定 | 两个 CLI resume 后 id **均不变** | 🟢 去掉「id 可能 fork」的覆盖逻辑（mismatch 判定仅保留「失败 + no conversation found」一支） |
| S4.1 | codex 需从一次性 `exec` 改造成 `codex app-server --listen stdio://` + JSON-RPC `thread/resume`（重集成） | codex `exec resume` 子命令直接可用，仍是一次性 exec 模型 | 🟢 **重集成整块删除**——工作量差一个数量级 |
| S4.2 | app-server `thread/resume` 前查 rollout/thread 存在性 | 仍需要（`exec resume` 同样依赖 `~/.codex/sessions` 下 rollout 文件），但检查点从 JSON-RPC 挪到 exec 前的文件存在性校验 | 🟡 护栏保留，实现更轻 |
| S4.3/4.4 | `turn/start` + drain + 握手超时 + app-server 生命周期管理 | 不需要——沿用现有 `codex exec` 的 subprocess + `_parse_line` | 🟢 删除 |

**未被推翻、仍成立的**：`_parse_line` 两条线均原样复用（schema 未变）；降级链（首次/resume miss/poisoned/跨 provider/跨 workdir → 全量重建）；三项 Multica 护栏（retired_session / codex rollout 校验 / resume-unsafe 清单）与 CLI 能力正交，独立成立。

---

## 4. 对新最小 change 的净影响

**执行层（两线均轻路径，改动全在平台侧）**：

- **claude 线**（最优路径）：
  1. 生成 UUID，首轮 `--session-id <uuid>` 传入并记录（省抓取）
  2. 命中 session 时 `--resume <uuid>`
  3. `_parse_line` 原样复用
  4. 前置：worker 复用固定 `CLAUDE_CONFIG_DIR`；目录丢失 → 降级全量

- **codex 线**（轻路径，非重集成）：
  1. 首轮 `codex exec --json` 时从 `thread.started` 抓 `thread_id` 冒泡存库（`_parse_line` 补一个分支）
  2. 命中 session 时命令改为 `codex exec resume <flags> <thread_id> -`（**去掉 `--cd`**）
  3. `_parse_line` 的 item.* 分支原样复用
  4. 前置：resume 前校验 `~/.codex/sessions` 下 rollout 存在（对标 Multica `gateCodexResumeToRolloutPresence`）；不在 → 降级全量

**存储层**：需要一列存 CLI session id（claude=预分配 UUID、codex=抓来的 thread_id）+ workdir + provider/backend + committed 水位。粒度按「两者都要」——先 `task_runs`（同 run 续跑地基），再上浮 `(conversation, agent)`（跨 task 续接优化）。

**护栏层**（Multica 已验证，与 CLI 能力正交）：resume-unsafe 清单（复用现有 `retriable_fail` 分类扩展）、codex rollout 校验、retired_session（单 worker 下可 defer）。

---

## 5. 给决策者的三句话

1. **CLI 一行不用改**：claude/codex 都原生支持 resume，实测续跑成功、输出格式不变、id 不 fork。
2. **两线都比旧 spec 轻**：claude 预分配省抓取，codex `exec resume` 让「app-server 重集成」整块消失，是新 change 最大的减负点。
3. **前置条件已钉死**：claude 靠固定 config dir、codex 靠 rollout 在位，二者丢失都走既有降级链——降级不是新增复杂度，是已被实测触发条件坐实的安全网。

---

*附：所有结论可复跑——`py -3.12 TestReport/run_claude_resume_probe.py` / `run_codex_resume_probe.py`，各起两轮真实 CLI，产出 `*_report.txt` + `*_run{1,2}.jsonl` 原始事件流。*
