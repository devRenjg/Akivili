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
- [x] 1.1 `task_runs` 加列：`cli_session_id TEXT`（claude=预分配 UUID / codex=抓来的 thread_id）、`session_backend TEXT`（claude/codex）、`session_workdir TEXT`、`session_committed_msg_id INTEGER`（成功才推进的增量水位）。走 Alembic 迁移（对齐现有 001-004 链，PG 单引擎）。〔迁移 005 + TaskRun model，已验证 004→005 upgrade、DB revision=005、ORM 15 列与 DB 完全对齐〕
- [x] 1.2 `claude_code.py`：执行前生成 UUID（`uuid.uuid4()`），命令加 `--session-id <uuid>`，把该 UUID 通过 `on_session` 回调回传给 runner 记录。实测确认 CLI 回显同一 UUID（CLI 实测 Paper 2.1），故无需从输出解析。〔`base.py` 加 `on_session` 回调 + `ExecContext.cli_session_id`；claude 生成 UUID→回传→注入 `--session-id`；codex/api 签名同步接受不使用〕
- [x] 1.3 `runner.py`：run 启动时把预分配 UUID + backend + workdir 写入 `task_runs.cli_session_id` 等列（run 开始即落，不等收尾——保证打断后能查到）。〔`_on_session` 回调 + `_record_session` 异步写库；已端到端验证三件套落库、committed 保持 NULL〕
- [x] 1.4 探针 `run_session_capture_probe.py`：claude run 执行后 `task_runs.cli_session_id` == 传入 UUID、backend/workdir 正确落库；run 中途查库能查到 session_id（不等收尾）。〔已验证：自生成/预分配两用例回传值==命令注入值；纳入 CI suite〕

### S2. claude resume + 增量回灌（attempt 间续跑）

> **数据流**（架构现实修正见 proposal）：run_queue.cli_session_id 是 attempt 间传递载体。
> 第 1 次 attempt 无 → 全量首建 → claude 生成 UUID → on_session 同时写 task_runs + run_queue；
> 失败重试（同 item，attempts+1）读 run_queue.cli_session_id → 传入 execute_dispatch → resume + 增量。

- [x] 2.0 迁移 006：`run_queue` 加 `cli_session_id TEXT` + `session_committed_msg_id INTEGER`（attempt 间传递 session 指针 + 增量水位）。走 Alembic（Revises 005）。model 同步。〔已验证 005→006 upgrade、run_queue 2 列建成、ORM parity 75/0〕
- [x] 2.1 `collab.py::_run_one`：把 item 的 `cli_session_id` / `session_committed_msg_id` 传入 `execute_dispatch`（新参数 `resume_session_id` / `committed_msg_id` / `queue_item_id`）；`execute_dispatch` 命中且 backend 是 CLI（claude/codex）→ 走「resume + 增量」，否则「全量首建」（现状）。〔`_RQ_COLS` 加 2 列使 item 自动带上；调用侧传参〕
- [x] 2.2 增量水位：`session_committed_msg_id`（上次成功已消费到的 message id）+ 本次快照终点（构建 prompt 那刻的 `MAX(messages.id)`，本 attempt 内临时算）。增量 = `messages WHERE conversation_id=? AND id > committed AND id <= snapshot_end AND 作者非本 agent ORDER BY id`（参数化）。成功收尾才把 committed 推进到快照终点并写回 run_queue；崩溃/中断 committed 未推进 → 下次 attempt 从同一起点重取 → **重复但不漏**（at-least-once）。〔`_advance_committed` 仅 status==succeeded 调用，写 run_queue+task_runs〕
- [x] 2.3 `executor/base.py::build_cli_prompt`：**无需改**——已是「本轮指令最前 + history 附后」结构，history 为空只返回本轮指令。只要上游把 `ctx.history` 从全量换成增量即可（数据源变、函数不变）。本任务 = 在 `execute_dispatch` 按 resume 命中与否切换 history 数据源。〔execute_dispatch 按 resume_hit 分叉 history 取法，build_cli_prompt 未改〕
- [x] 2.4 `claude_code.py`：`ctx.cli_session_id` 非空（resume 命中）→ 命令用 `--resume <id>`（而非首建的 `--session-id`）；`_parse_line` 原样复用（实测 schema 不变）。〔S2.7 探针验证首建/续跑命令分支互斥正确〕
- [x] 2.5 **前置：固定 CLAUDE_CONFIG_DIR**。确认 `claude_code.py:76` 的 `%TEMP%/akivili_claude_cfg` 在 worker 进程内稳定（同一 worker 两次执行命中同目录）。目录丢失 → resume miss → 降级全量（见 S4.2）。〔现有代码已用固定路径、非随机；符合前置〕
- [x] 2.6 `runner.py::_on_session`：session_id 回写时**同时**更新 `run_queue.cli_session_id`（attempt 间传递），不只 task_runs。〔`_record_session` 加 queue_item_id 参数，同事务写 task_runs + run_queue〕
- [x] 2.7 探针 `run_claude_resume_incremental_probe.py`：命令分支（首建--session-id/续跑--resume）+ 增量 SQL（含队友+用户、排除本 agent 自产）。〔已验证全过；纳入 CI suite〕〔并发/token 对比属真起 CLI 的集成验证，留待联调〕

### S3. codex 线（thread_id 抓取 + exec resume，与 claude 并行）
- [x] 3.1 `codex.py`：`_extract_thread_id` 从顶层 `type == "thread.started"` 事件提取 `thread_id`；在 `_reader` 解析循环里检测（只回传一次），经 `on_session` 用 `call_soon_threadsafe` 回传（同 on_pid，非塞进 _parse_line 改其返回契约）。当前该事件走兜底返回 None（CLI 实测 Paper 2.4）。〔S3.5 探针验证抓取正确〕
- [x] 3.2 `runner.py`：codex run 从 `on_session` 回调捕获 thread_id，`_record_session` 写入 `task_runs.cli_session_id` + `run_queue.cli_session_id`（backend=codex，S2.6 已统一）。〔复用 S1.3/S2.6 的 _on_session 链路，codex/claude 同机制〕
- [x] 3.3 `codex.py`：命中 session（ctx.cli_session_id 非空）→ 命令改为 `codex exec resume <flags> <thread_id> -`（OPTIONS 放 id 前、**去掉 `--cd`**，resume 从 session 自身恢复 cwd，CLI 实测 Paper 2.3）；`_parse_line` 的 item.* 分支原样复用。〔S3.5 探针验证命令分支互斥、无 --cd、id 在 resume 后〕
- [x] 3.4 **codex rollout 在位校验**（对标 Multica `gateCodexResumeToRolloutPresence`）：`codex_rollout_present` 查 `~/.codex/sessions/**/*<thread_id>*.jsonl` 存在（CODEX_HOME 可覆盖）；runner resume_hit 判定对 codex 额外调它，不在 → resume_hit=False 降级全量。〔S3.5 验证真实命中/不存在·空→降级〕
- [x] 3.5 探针 `run_codex_resume_incremental_probe.py`：codex 命令分支（首建 exec --json / 续跑 exec resume）+ thread_id 抓取 + rollout 校验降级。〔已验证全过；纳入 CI suite〕〔真起 codex 续接/token 属集成验证，留待联调〕

### S4. 降级链（保证不劣于现状）
- [x] 4.1 首次执行（无 session）→ 全量回灌（现状路径，不变）。〔resume_hit 判定：无 session_id → False → 全量〕
- [x] 4.2 resume 未落地判定：`_should_drop_session` 的 `_RESUME_MISS_RE`（no conversation found / session·thread not found / rollout not present / resume fail 等）→ 清 `run_queue.cli_session_id`+`session_committed_msg_id` → 下次 attempt 全量重建。（实测两线 id 不 fork，无「成功但 id 变」mismatch 分支——比废弃版 S2.6 简化。）〔S4.5 验证〕
- [x] 4.3 **resume-unsafe（poisoned）丢 session**：`_POISONED_RE`（迭代上限 / 模型 400 invalid_request / 上下文溢出 / codex 语义静默，对标 Multica resume-unsafe 清单）命中 → 清 session、下次从头重建。限流(429)**不丢**（会话仍有效，走重试）。〔S4.5 验证 10 类分类〕
- [x] 4.4 provider/backend 变更 → 弃旧 session、全量重建。〔resume_hit 判定：非 CLI backend(api)→False；backend 类型不符→走首建〕workdir 变更：**阶段一 attempt 间续跑 workdir 天然不变**（同 task 同 project_dir），workdir 一致性校验留待阶段二跨 task（届时 workdir 可能变）。
- [x] 4.5 探针 `run_session_fallback_probe.py`：① _should_drop_session 分类(resume_miss/poisoned/不丢) ② resume_hit 各降级入口(无session/api/rollout缺) ③ 清 session 的 DB 效果。〔已验证全过；纳入 CI suite〕

## 阶段二 —— 跨 task 续接（best-effort 缓存，省 token）

> 前提：阶段一验证通过。粒度 = **best-effort 缓存**（用户拍板），无 owner CAS/串行折叠，并发写互盖最坏 miss 一次降级。

- [x] 5.1 新增轻表 `agent_sessions(conversation_id, agent_slug, cli_session_id, session_committed_msg_id, provider_id, backend, workdir, updated_at)`，唯一键 `(conversation_id, agent_slug)`。走 Alembic 迁移。〔迁移 007 走 ORM `AgentSession.__table__.create(checkfirst=True)` 保 parity（SERIAL/now() 与模型零漂移）；实测 DB 升级到 007、parity 79/0、migration 15/15（19 表）〕
- [x] 5.2 run 成功收尾时把 session 指针 + committed 水位 **upsert** 到 `agent_sessions`（`ON CONFLICT (conversation_id, agent_slug) DO UPDATE`，后写覆盖——best-effort，不加锁不做 owner 校验）。〔`runner._upsert_agent_session` 复用 `models.upsert` helper；`_session_used` 持有本 run 实际会话 id；仅 CLI backend 且有 id 时写、容错吞异常〕
- [x] 5.3 `runner.py` 查 session 顺序：先查本 run（阶段一，同 run 续跑）→ 未命中查 `agent_sessions`（阶段二，跨 task 续接）→ 都没有走全量首建。〔`runner._lookup_agent_session`；`session_source` 标记 run/cross_task；放 runner 内部（用户拍板方案 A），所有 dispatch 入口自动获得跨 task 续接〕
- [x] 5.4 跨 task 命中时同样走 S2/S3 的 resume + 增量 + 降级链（复用，不新增逻辑）。〔S5.3 设好 resume_session_id/committed 后，既有 resume_hit 路径（ctx.cli_session_id + 增量 SQL + codex rollout + S4 降级）自动生效，零新增；collab 降级路径额外清 agent_sessions 缓存行防下个 task 复命中坏会话〕
- [x] 5.5 探针 `run_cross_task_resume_probe.py`：同 (conversation, agent) 第二个 task 命中上个 task 的 session、resume 续接、增量正确；并发两 task 写同一 key 时后写覆盖不报错、下次至多 miss 一次降级（不崩不脏）。〔A upsert/lookup 往返 · B 后写覆盖至多一行 · C 并发写同 key 不崩恰一行 · D 两段查找顺序 · E miss 降级，全绿；纳入 GATE（45→46）〕

## 收尾
- [x] 回归全量探针（mention/timeout/scheduling/kill-signal/containment 等）确认 resume 改造不回归协同/超时/调度/重启行为。〔全量 CI 门禁 43/43·589 断言全绿；修复两处假后端桩漏传 `on_session` 形参（commit 2ce77fa）〕
- [x] 更新 `TestReport/run_ci_suite.py` 纳入本 change 新增探针，更新 `TestReport/README.md` 计数。〔阶段一 4 探针 + 阶段二 1 探针（S5.5）已在 GATE；全量门禁 41→46（--exclude-slow 39→44）、README 计数同步、矩阵表加 Session Resume 小节（含跨 task）〕
- [x] 更新 `README`（执行层行为说明）：阶段一 = attempt 间续跑，对**外部行为不变**（用户/协同/调度视角无感，仅同 run 重试时省 history token）；无需改用户向 README。执行层细节以本 change design.md + CLI 实测 Paper 为准。
- [x] 端到端联调（真起 CLI）：`run_resume_e2e_probe.py` claude+codex 各跑跨 task 续接，17/17 全绿。
  **功能全部正确**——session 捕获/agent_sessions 缓存/resume 续接/codex rollout/**会话记忆连续（两线都答出只在对话里的暗号）**。
  **token 反直觉发现**：短会话 resume 不省反增（claude 41041→41156、codex 42452→91440），根因=resume 恢复
  CLI 完整会话状态 > 平台精简重放 `_clip_history`；省 token 只在长对话成立。已如实沉淀
  `Papers/Session-Resume端到端实测-token与记忆.md` + design.md 抉择二实测修正块。附带交付 token 捕获永久能力（迁移 008）。
- [ ] 固化（**待本轮合 master 后做**）：阶段一+二代码 + 联调 + token 捕获均已交付、门禁绿、真实证据落 Paper。
  待合入 master 后把能力规格固化进 `specs/agent-session-resume/spec.md`、目录移入
  `changes/archive/<date>-agent-session-resume-minimal/`。（固化时把「短会话 resume 不省 token、价值在连续性」写进规格，不宣称短会话省 token。）
- [x] 联动 [platform-graceful-restart]：其跨-change 表阶段 3/4 标 ✅ 已由本 change 阶段一交付、阶段 5 header 标注 resume 地基来源，并显式记录与废弃航母版的 3 处差异（codex 无 app-server / session 载体 / at-least-once 水位）。
