# Tasks — agent-session-resume-minimal

> 规划态，落实前逐阶段推进、每阶段探针通过才进下一步。claude 线改动小、codex 线需补 thread_id 抓取，两线可并行。
>
> **阶段划分**（用户拍板「两者都要」，先地基后优化）：
> - **阶段一 = 同 run 续跑**：session_id 存 `task_runs`，worker 重启/kill 打断后同一 run `--resume` 续跑。是 [platform-graceful-restart] 阶段5交棒续跑的地基。
> - **阶段二 = 跨 task 续接**：session_id 上浮 `agent_sessions(conversation, agent)`，纯省 token。**best-effort 缓存**（用户拍板）——无 owner CAS/串行折叠，并发写互盖最坏 miss 一次降级全量。
>
> **上游依据**：`Papers/CLI-resume能力实测-claude与codex.md`（CLI 行为取证）+ `Papers/Multica减法校准-restart-resume落地前置结论.md`（减法边界）。

## 阶段一 —— 同 run 续跑地基（claude 线先行）

### S1. 存储 + claude 预分配
- [ ] 1.1 `task_runs` 加列：`cli_session_id TEXT`（claude=预分配 UUID / codex=抓来的 thread_id）、`session_backend TEXT`（claude/codex）、`session_workdir TEXT`、`session_committed_msg_id INTEGER`（成功才推进的增量水位）。走 Alembic 迁移（对齐现有 001-004 链，PG 单引擎）。
- [ ] 1.2 `claude_code.py`：执行前生成 UUID（`uuid.uuid4()`），命令加 `--session-id <uuid>`，把该 UUID 通过 `ExecContext` 或 `on_pid` 同机制回传给 runner 记录。实测确认 CLI 回显同一 UUID（CLI 实测 Paper 2.1），故无需从输出解析。
- [ ] 1.3 `runner.py`：run 启动时把预分配 UUID + backend + workdir 写入 `task_runs.cli_session_id` 等列（run 开始即落，不等收尾——保证打断后能查到）。
- [ ] 1.4 探针 `run_session_capture_probe.py`：claude run 执行后 `task_runs.cli_session_id` == 传入 UUID、backend/workdir 正确落库；run 中途查库能查到 session_id（不等收尾）。

### S2. claude resume + 增量回灌
- [ ] 2.1 `runner.py`：执行前查本 run（或其恢复来源 run）的 `cli_session_id`；命中且 backend/workdir 一致 → 走「resume + 增量」；否则走「全量首建」（现状）。
- [ ] 2.2 增量水位：`session_committed_msg_id`（上次成功已消费到的 message id）+ 本次快照终点（构建 prompt 那刻的 `MAX(messages.id)`，本 run 内临时算）。增量 = `messages WHERE conversation_id=? AND id > committed AND id <= snapshot_end AND 作者非本 agent ORDER BY id`（参数化）。成功收尾才把 committed 推进到快照终点；崩溃/中断 committed 未推进 → 续跑从同一起点重取 → **重复但不漏**（at-least-once）。
- [ ] 2.3 `executor/base.py::build_cli_prompt`：输入从「全量历史」改为「增量历史」（格式复用【用户】/【队友】，仅数据源变）；增量为空时 prompt 仅本轮指令。
- [ ] 2.4 `claude_code.py`：命中 session 时 `cmd += ["--resume", session_id]`（`_parse_line` 原样复用，实测 schema 不变）。
- [ ] 2.5 **前置：固定 CLAUDE_CONFIG_DIR**。确认 `claude_code.py:76` 的 `%TEMP%/akivili_claude_cfg` 在 worker 进程内稳定（同一 worker 两次执行命中同目录）。目录丢失 → resume miss → 降级全量（见 S3.1）。
- [ ] 2.6 探针 `run_claude_resume_incremental_probe.py`：二次执行带 `--resume`、prompt 只含增量、token/长度较全量下降；并发场景（A 跑期间 B 发言）下次触发 A 不漏 B 的话；committed 崩溃未推进时续跑不漏。

### S3. codex 线（thread_id 抓取 + exec resume，与 claude 并行）
- [ ] 3.1 `codex.py::_parse_line`：补一个分支，顶层 `type == "thread.started"` 时提取 `thread_id`，经 `ExecEvent`（如 `ExecEvent("system", ..., meta={"session_id": thread_id})`）冒泡回 runner。当前该事件走兜底返回 None（CLI 实测 Paper 2.4）。
- [ ] 3.2 `runner.py`：codex run 从冒泡事件捕获 thread_id，写入 `task_runs.cli_session_id`（backend=codex）。
- [ ] 3.3 `codex.py`：命中 session 时命令改为 `codex exec resume <flags> <thread_id> -`（OPTIONS 放 id 前、**去掉 `--cd`**，resume 从 session 自身恢复 cwd，CLI 实测 Paper 2.3）；`_parse_line` 的 item.* 分支原样复用。
- [ ] 3.4 **codex rollout 在位校验**（对标 Multica `gateCodexResumeToRolloutPresence`）：resume 前查 `~/.codex/sessions` 下对应 thread 的 rollout 文件存在，不在 → 不 resume、降级全量。
- [ ] 3.5 探针 `run_codex_resume_incremental_probe.py`：codex 二次执行 `exec resume` 续接、prompt 只含增量；rollout 不存在时降级全量不报错。

### S4. 降级链（保证不劣于现状）
- [ ] 4.1 首次执行（无 session）→ 全量回灌（现状路径，不变）。
- [ ] 4.2 resume 未落地判定：`失败 && (stderr "no conversation found" / rollout 缺失 / session 记录丢失)` → 清 `cli_session_id` + 本次降级全量 + 成功后重建。（实测两线 id 不 fork，故无「成功但 id 变」的 mismatch 分支——比废弃版 S2.6 简化。）
- [ ] 4.3 **resume-unsafe（poisoned）丢 session**：定义 poisoned 集（迭代上限 / 模型 400 / codex 语义静默 / 上下文溢出），命中 → 主动丢弃 prior session、下次从头重建（不 resume 坏状态）。复用现有 `retriable_fail` 分类扩展。
- [ ] 4.4 provider/backend 变更、workdir 变更 → 弃旧 session、全量重建。
- [ ] 4.5 探针 `run_session_fallback_probe.py`：模拟 ① no conversation found ② rollout 缺失 ③ poisoned 失败 ④ 换 provider ⑤ 换 workdir，均正确降级全量、不报错、结果不劣于现状。

## 阶段二 —— 跨 task 续接（best-effort 缓存，省 token）

> 前提：阶段一验证通过。粒度 = **best-effort 缓存**（用户拍板），无 owner CAS/串行折叠，并发写互盖最坏 miss 一次降级。

- [ ] 5.1 新增轻表 `agent_sessions(conversation_id, agent_slug, cli_session_id, session_committed_msg_id, provider_id, backend, workdir, updated_at)`，唯一键 `(conversation_id, agent_slug)`。走 Alembic 迁移。
- [ ] 5.2 run 成功收尾时把 session 指针 + committed 水位 **upsert** 到 `agent_sessions`（`ON CONFLICT (conversation_id, agent_slug) DO UPDATE`，后写覆盖——best-effort，不加锁不做 owner 校验）。
- [ ] 5.3 `runner.py` 查 session 顺序：先查本 run（阶段一，同 run 续跑）→ 未命中查 `agent_sessions`（阶段二，跨 task 续接）→ 都没有走全量首建。
- [ ] 5.4 跨 task 命中时同样走 S2/S3 的 resume + 增量 + 降级链（复用，不新增逻辑）。
- [ ] 5.5 探针 `run_cross_task_resume_probe.py`：同 (conversation, agent) 第二个 task 命中上个 task 的 session、resume 续接、增量正确；并发两 task 写同一 key 时后写覆盖不报错、下次至多 miss 一次降级（不崩不脏）。

## 收尾
- [ ] 回归全量探针（mention/timeout/scheduling/kill-signal/containment 等）确认 resume 改造不回归协同/超时/调度/重启行为。
- [ ] 更新 `TestReport/run_ci_suite.py` 纳入本 change 新增探针，更新 `TestReport/README.md` 计数。
- [ ] 更新 `README`（如涉及执行层行为说明）。
- [ ] 固化：本 change 完成并验证后，把 `agent-session-resume` 能力规格从 change delta 固化进 `specs/agent-session-resume/spec.md`，change 目录移入 `changes/archive/<date>-agent-session-resume-minimal/`。
- [ ] 联动 [platform-graceful-restart]：其阶段 5「交棒续跑」标注「resume 地基（session_id 存储 + `--resume` + 增量回灌）由 agent-session-resume-minimal 提供」。
