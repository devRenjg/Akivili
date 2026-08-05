# Proposal — agent-session-resume-minimal

> **定位**：以 Multica 生产实现为蓝本、经 CLI 实测校准的**最小可行** Session Resume。取代已废弃的 `agent-session-resume`（21 轮 Review 的「航母级」重方案）。
>
> **上游依据**（均已入 git，commit `3fdb9b2`）：
> - `Papers/Multica减法校准-restart-resume落地前置结论.md` — 减法决策快照（哪些过度、哪些不能砍、哪些缺护栏）
> - `Papers/CLI-resume能力实测-claude与codex.md` — 两个 CLI 的真实 resume 行为取证（预分配 / thread_id / 轻路径 / 不 fork）
>
> **与废弃版的关系**：废弃版 `changes/agent-session-resume/` 原地保留作历史对照。本 change 是它的**减法重拟**——核心价值（存 session_id + `--resume` + 增量回灌省 token）不变，但砍掉航母级的 message_seq 行锁水位、attempt 级 fencing、NULL migration 状态机等（那些属于 graceful-restart 的重并发控制，不是 resume 本身所需）。

## Why

现状：每次 Agent 执行（一次 @ 分派）都**独立建一次性 CLI 会话**，靠把**整个 task 会话历史全量回灌**成一段大 prompt 喂给 CLI 来恢复上下文（`runner.py::build_context` + `executor/base.py::build_cli_prompt`，`_clip_history` 双限裁剪）。三个实测确认的痛点：

1. **Token 浪费**：每次执行把历史整段重喂，长对话 token 成本随轮数线性增长；`_clip_history` 到上限即裁剪，**丢早期上下文**。
2. **上下文不连贯**：CLI 每次「重读一遍历史文本」，不如它自己维护的连续会话记忆（claude/codex 原生 session）。
3. **弃用了 CLI 原生续接能力**：实测确认 claude（`--session-id` 预分配 + `--resume`）与 codex（`exec resume <thread_id>`）**均原生支持 resume、续跑成功、输出格式不变、session id 不 fork**（见 CLI 实测 Paper 第 1 节），我们完全没用。

**这同时是 [platform-graceful-restart] 阶段 5「交棒续跑」的地基**：worker 重启/超时打断后要让同一个 run 续跑，前提是先有 session_id 存储 + `--resume` 能力。故 resume 既是独立的省 token 优化，又是平滑重启的前置（见内存 [[graceful-restart-resume-changes]]）。

**主攻场景（用户拍板「两者都要」，先地基后优化）**：
- **① 同 run 续跑（地基，优先）**：worker 重启/kill 打断后，同一 run 用存下的 session_id `--resume` 接着跑，不从头重来。session_id 存 `task_runs`。
- **② 跨 task 续接（优化，其后）**：同一 `(conversation, agent)` 的下一次执行接上一次的 CLI 会话，省历史重放。session_id 上浮到 `(conversation, agent)` 粒度。

## What Changes

> 规划态，claude 与 codex 均必选（用户重度使用 codex）。**本 change 暂不改代码**，落实时逐阶段推进。

- **CLI session_id 捕获/预分配（实测校准）**：
  - **claude**：不解析、直接**预分配** —— 执行前生成 UUID，`--session-id <uuid>` 传入并记录（实测 CLI 回显同一 UUID，见 CLI 实测 Paper 2.1）。比废弃版 S1.3「从 system/result 行解析」省一步。
  - **codex**：从首轮 `codex exec --json` 的 `thread.started` 事件抓 `thread_id`（`executor/codex.py::_parse_line` 当前忽略该事件，需补一分支），写回存储。
- **session 存储**：新增列存 CLI session_id（列名 `cli_session_id`）+ workdir + provider/backend + committed 水位。
  - 阶段一（同 run）：存 `task_runs`（每个 run 一条 session 指针）。
  - 阶段二（跨 task）：上浮到轻表 `agent_sessions(conversation_id, agent_slug, session_id, committed_msg_id, provider_id, backend, workdir, updated_at)`，唯一键 `(conversation_id, agent_slug)`。
- **resume 命令构造（实测校准，两线均轻路径）**：
  - **claude**：命中 session → `-p --resume <uuid>`；`_parse_line` 原样复用（实测 schema 不变）。
  - **codex**：命中 session → `codex exec resume <flags> <thread_id> -`（**去掉 `--cd`**，resume 从 session 自身恢复 cwd，见 CLI 实测 Paper 2.3）；`_parse_line` item.* 分支原样复用。**不需要** app-server + JSON-RPC 重集成（废弃版 S4.1 整块删除）。
- **增量回灌（省 token 的收益兑现点）**：resume 命中时，prompt 只带「本轮增量指令 + 上次以来的新消息」，不再全量重放。用 `committed_msg_id`（成功才推进）+ 本次快照终点两个水位，崩溃时 committed 未推进 → 续跑从同一起点重取 → **重复但不漏**（at-least-once）。
- **降级链（保证不劣于现状）**：首次执行（无 session）→ 全量回灌（现状）；resume 未落地 / 跨 provider / 跨 workdir / session 记录丢失 → 回退全量 + 重建 session。**任何降级都不劣于现状。**
- **Multica 护栏（生产验证，与 CLI 能力正交）**：
  - **resume-unsafe 清单**：poisoned 失败（迭代上限 / 模型 400 / codex 语义静默 / 上下文溢出）主动丢弃 prior session、下次从头重建（不 resume 坏状态）。复用现有 `retriable_fail` 分类扩展。
  - **codex rollout 在位校验**：resume 前查 `~/.codex/sessions` 下对应 rollout 存在，不在 → 降级全量（对标 `gateCodexResumeToRolloutPresence`）。
  - **retired_session**：单 worker 下边缘 case，本 change **defer**（对标 Multica 迁移 234，多 worker 时再补）。

## Capabilities

### New Capabilities

- `agent-session-resume`：Agent 执行的 CLI 会话复用能力——每次执行不再独立建一次性会话 + 全量回灌历史，而是维护一条可续接的 CLI session：
  - **同 run 续跑**（阶段一）：run 被打断（worker 重启/kill）后，用存下的 session_id `--resume` 让同一 run 从上次上下文续跑。
  - **跨 task 续接**（阶段二）：同一 `(conversation, agent)` 再次执行时 resume 上次会话 + 只喂增量上下文。
  - 含首次/resume-miss/poisoned/provider 变更/workdir 变更的降级链，保证不劣于现状。

## Impact

- **规划态，暂不改代码。** 落实时预计涉及：
  - `executor/claude_code.py`：`--session-id <uuid>` 预分配 + `--resume <uuid>`（`_parse_line` 不变）
  - `executor/codex.py`：`_parse_line` 补 `thread.started`→`thread_id` 抓取分支 + resume 时命令改为 `exec resume <flags> <thread_id> -`（去 `--cd`）
  - `executor/base.py`：`build_cli_prompt` 支持增量模式（全量→增量数据源切换）
  - `executor/runner.py`：查/写 session 存储、committed 水位推进、增量取历史、降级链
  - `models/tables.py` + 迁移：阶段一 `task_runs` 加 session 列；阶段二新增 `agent_sessions` 表
- **关联能力**：[agent-collaboration]（多 Agent 协同/会话）、[agent-execution]（执行）、[platform-graceful-restart]（其阶段 5「交棒续跑」依赖本 change 出的 session_id 存储 + `--resume` 地基）。
- **不破坏多 Agent 协同**：每个 (conversation, agent) 独立 session，互不干扰；别人的发言经增量回灌补给，语义正确。
- **CLI 前置条件（实测坐实，落入降级链）**：claude 依赖 worker 复用固定 `CLAUDE_CONFIG_DIR`（会话记录存该目录）；codex 依赖 `~/.codex/sessions` 下 rollout 在位。二者丢失均触发既有降级全量——非新增复杂度，是被实测坐实触发条件的安全网。
- **与废弃版的差异**：砍掉 message_seq per-conversation 行锁水位、attempt 级 fencing、session owner 四阶段 CAS、NULL migration 状态机等——这些是 graceful-restart 的高并发控制，不是 resume 本身所需。resume 的水位用简单的 committed + 快照终点两段、at-least-once 语义即可（对标 Multica 靠 CLI 原生 session 记忆、不自管细粒度水位）。
