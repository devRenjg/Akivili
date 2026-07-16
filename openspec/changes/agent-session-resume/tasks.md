# Tasks — agent-session-resume

> claude 与 codex 均必选（用户重度使用 codex）。claude 线 S1→S2→S3，codex 线 S4 可并行（依赖 S1 表结构与降级链框架）。规划态,落实前逐阶段推进。
>
> **跨-change 执行顺序**：本 change 的 S1/S2/S3/S4 = 总执行地图的第 2/3/4/5 步（起点 PGR-M1 是第 1 步，汇合点 PGR-M2.5 是第 7 步）。完整 8 步顺序、依赖与验证门见 [platform-graceful-restart] 的 `tasks.md` 顶部「跨-change 执行顺序」。**每步验证门通过才进下一步。**

## S1 — session 表 + 串行约束 + session_id 抓取（claude）
- [ ] 1.1 建表 `agent_sessions(id, conversation_id, agent_slug, session_id, committed_msg_id, provider_id, backend, workdir, updated_at)`,唯一键 `(conversation_id, agent_slug)`;`task_runs` 加列 `planned_through_msg_id`;`database.py` 建表 + 迁移（存量无行,首次执行自然走全量分支）
- [ ] 1.2 **同 (task, agent) 串行折叠（Review P0-3，非丢弃、非伪 index）**：`run_queue` 无 conversation_id 列，**不建** `(conversation_id,agent_slug)` partial index;改折叠模型——queued 未 claim→合并新触发消息水位到该行;running→持久化 `rerun_requested`+`pending_through_message_id`+触发来源;收尾事务内据 pending 至多建一个 successor（取代现状 `collab.py:392` 的 `return None` 丢弃）
- [ ] 1.3 `claude_code.py::_parse_line` 从 `system`(init)/`result` 行提取 `session_id`,经 `ExecEvent` 新字段冒泡（现在只当「执行完成」,不丢弃）
- [ ] 1.4 session_id 生命周期（三件套）：流中途首次见 id 即 **pin** 落库（防崩溃丢指针）;收尾以最新 id **覆盖存**,更新用 `SET session_id = COALESCE(?, session_id)`（空值不清旧指针）
- [ ] 1.5 首次执行（无 session 行）走现状全量回灌,不带 resume,成功后落 session（session_id + provider_id + backend + workdir + committed_msg_id）
- [ ] 1.6 探针 `agent_session_build_probe`：首次执行后 `agent_sessions` 有正确 session_id、committed_msg_id、provider/backend/workdir 落库;同 (task,agent) 重复触发折叠不丢、不并行起两条

## S2 — claude resume + 增量回灌（committed/planned 两阶段水位，at-least-once 安全）
- [ ] 2.1 `runner.py` 执行前查 `agent_sessions`,命中且 backend/provider/workdir 一致 → 走「增量 + resume」；否则走「全量首建」
- [ ] 2.2 **2 阶段水位**：`task_runs.planned_through_msg_id` = 构建本次 prompt 那刻的 `MAX(messages.id)`（本次快照终点）;`agent_sessions.committed_msg_id` **只在 run 成功收尾才推进**到 planned_through。构建后崩溃 committed 不提前推进（否则漏）
- [ ] 2.3 增量历史 SQL：`messages WHERE conversation_id=? AND id > committed_msg_id AND id <= planned_through_msg_id AND 作者非本 agent ORDER BY id`（参数化）;崩溃/中断时 committed 未推进→续跑从同一起点重取→重复但不漏（at-least-once）;本 Agent 自产历史由 session 记忆承载不重喂;仍过 `_clip_history` 防单次海量
- [ ] 2.4 `build_cli_prompt` 输入集从「全量历史」改为「增量历史」（格式复用【用户】/【队友】,仅数据源变）;增量为空时 prompt 仅本轮指令
- [ ] 2.5 `claude_code.py` 命中 session 时 `args += ["--resume", session_id]`
- [ ] 2.6 **resume 落地判定（Review P1-6：mismatch 只在失败时判）**：`失败 && (stderr "no conversation found" 或 emitted id≠请求 id)` → 判未落地 → 清 sid + 本次降级全量;`成功 && emitted id≠请求 id` → 正常（resume 后 fork 新 id）→ 接受并覆盖存，不判失败
- [ ] 2.7 探针 `agent_session_resume_probe`：二次执行带 `--resume`、prompt 只含增量、**并发场景**（A 跑期间 B 发言）下次触发 A 不漏 B 的话;token/prompt 长度对比全量下降

## S3 — 降级链 + poisoned 丢 session（保证不劣于现状）
- [ ] 3.1 resume 未落地（S2.6 判定）→ 清 session_id → 降级全量 + 成功后重开新 session
- [ ] 3.2 **poisoned 失败丢 session**：定义 poisoned 集（`iteration_limit` 迭代上限 / `api_invalid_request` 模型 400 / `codex_semantic_inactivity` codex 语义静默）;命中 → 主动丢弃 prior session、下次从头重建（不 resume 坏状态）
- [ ] 3.3 provider/backend 变更 → 弃旧 session、开新 + 全量;workdir 变更 → 同;（未来多机）runtime 不匹配不 resume
- [ ] 3.4 探针 `agent_session_fallback_probe`：模拟 ① "no conversation found" ② poisoned 失败 ③ 换 provider ④ 换 workdir，均正确回退/丢弃、不报错、结果与现状等价

## S4 — codex app-server 集成（必选，与 claude 线并行）
- [ ] 4.1 `codex.py` 从一次性 `codex exec --json -m - -` 改为 **每次执行 attempt 起一个 `codex app-server --listen stdio://` 进程 + JSON-RPC**（run 结束即关，非 Worker 全局共享，Review P1-7）
- [ ] 4.2 `thread/resume`（带 `threadId`/`cwd`/`model`/`developerInstructions`）恢复线程;可恢复协议错误（unknown thread/schema 漂移）回退 `thread/start`;传输/进程错误 fail-fast（不回退）
- [ ] 4.3 `turn/start` 发本轮实际 prompt（本轮指令 + 增量历史）;drain 流;捕获 `threadId` 作为 codex session_id 存 `agent_sessions`
- [ ] 4.4 握手超时（`HandshakeTimeout`）、进程组隔离、app-server 生命周期管理;复用 claude 线的降级链与 poisoned 分类
- [ ] 4.5 探针 `codex_session_resume_probe`：codex 二次执行 `thread/resume` 续接、协议错误回退 `thread/start`、poisoned/降级链生效

## 收尾
- [ ] 回归全量探针（mention/timeout/scheduling 等）确认 resume 改造 + 串行约束不回归协同/超时/调度行为
- [ ] 固化：本 change 完成并验证后,把 `agent-session-resume` 能力规格从 change delta 固化进 `specs/agent-session-resume/spec.md`,change 移入 `changes/archive/`
- [ ] 联动 [platform-graceful-restart]：其 M2.5「静默+resume」标注「resume 地基（含 claude/codex、pin 落库、续跑重发原 prompt）由 agent-session-resume 提供」
