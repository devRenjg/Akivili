# Tasks — agent-session-resume

> claude 与 codex 均必选（用户重度使用 codex）。claude 线 S1→S2→S3，codex 线 S4 可并行（依赖 S1 表结构与降级链框架）。规划态,落实前逐阶段推进。
>
> **跨-change 执行顺序（对齐 PGR 阶段 0～6）**：总地图为 PGR 阶段 0（补规格）→ 1（DB 协议地基）→ 2（Worker 剥离）→ **3（Claude resume = 本 change S1-S3）→ 4（Codex app-server = 本 change S4）**→ 5（交棒+有界恢复）→ 6（Nginx 蓝绿）。本 change 的 claude 线 S1→S2→S3 = PGR 阶段 3，codex 线 S4 = PGR 阶段 4（依赖 S1 表结构与降级链框架，可与 claude 线并行）。**resume 地基（session_id/pin/committed 水位）是 PGR 阶段 5 交棒续跑的前置**。完整阶段依赖与验证门见 [platform-graceful-restart] 的 `design.md`「实施阶段」表。**每阶段验证门通过才进下一步。**

## S1 — session 表 + 串行约束 + session_id 抓取（claude）
- [ ] 1.1 建表 `agent_sessions(id, conversation_id, agent_slug, session_id, committed_msg_id, provider_id, backend, workdir, updated_at, session_version, current_task_run_id)`,唯一键 `(conversation_id, agent_slug)`（Review P1-3：`session_version` + `current_task_run_id` 供 pin/覆盖 CAS 防迟到事件覆盖新 pointer）;`task_runs` 加列 `planned_through_msg_id`;`database.py` 建表 + 迁移（存量无行,首次执行自然走全量分支）
- [ ] 1.2 **同 (task, agent) 串行折叠（Review P0-3/P0-B，DB 唯一性 + 折叠，非丢弃）**：建 `partial unique index ON run_queue(task_id, agent_slug) WHERE status IN ('queued','claimed','running')` 从 DB 层保单 active（应用层查重有 TOCTOU，不足以防并发 POST 各插一条）;折叠模型——queued 未 claim→原子 `MAX`/`COALESCE` 合并新触发消息水位到该行（不先读后写）;claimed·running→持久化 `rerun_requested`+`pending_through_message_id`+触发来源;收尾事务内据 pending 至多建一个 successor（取代现状 `collab.py:392` 的 `return None` 丢弃）。各终态折叠：failed→仍建 successor;killed→取消 pending 不建;superseded→pending 由 recovery child 继承不另建
- [ ] 1.3 `claude_code.py::_parse_line` 从 `system`(init)/`result` 行提取 `session_id`,经 `ExecEvent` 新字段冒泡（现在只当「执行完成」,不丢弃）
- [ ] 1.4 session_id 生命周期（三件套 + CAS 防迟到覆盖，Review P1-3）：流中途首次见 id 即 **pin** 落库（防崩溃丢指针）;收尾以最新 id **覆盖存**,更新用 `SET session_id = COALESCE(?, session_id)`（空值不清旧指针）;**pin 与覆盖 SHALL 带 CAS 条件**——`WHERE current_task_run_id=? AND session_version=?`（写入者仍是当前活跃 attempt/版本才更新），成功更新时 `session_version=session_version+1`、`current_task_run_id` 指向本 attempt;防上一轮迟到的流事件覆盖下一轮已建立的新 session pointer
- [ ] 1.5 首次执行（无 session 行）走现状全量回灌,不带 resume,成功后落 session（session_id + provider_id + backend + workdir + committed_msg_id）
- [ ] 1.6 探针 `agent_session_build_probe`：首次执行后 `agent_sessions` 有正确 session_id、committed_msg_id、provider/backend/workdir 落库;同 (task,agent) 重复触发折叠不丢、不并行起两条

## S2 — claude resume + 增量回灌（committed/planned 两阶段水位，at-least-once 安全）
- [ ] 2.1 `runner.py` 执行前查 `agent_sessions`,命中且 backend/provider/workdir 一致 → 走「增量 + resume」；否则走「全量首建」
- [ ] 2.2 **2 阶段水位**：`task_runs.planned_through_msg_id` = 构建本次 prompt 那刻的 `MAX(messages.id)`（本次快照终点，落该 attempt 行）;`agent_sessions.committed_msg_id` **只在 run 成功收尾才推进到 `committed_batch_end`（本轮实际连续喂达的末尾），SHALL NOT 直接推进到 planned_through**（见 2.3a）。构建后崩溃 committed 不提前推进（否则漏）
- [ ] 2.3 增量历史 SQL：`messages WHERE conversation_id=? AND id > committed_msg_id AND id <= planned_through_msg_id AND 作者非本 agent ORDER BY id`（参数化）;崩溃/中断时 committed 未推进→续跑从同一起点重取→重复但不漏（at-least-once）;本 Agent 自产历史由 session 记忆承载不重喂
- [ ] 2.3a **🔴 海量增量连续前缀分批（Review P0-C，禁用「保新丢旧」裁剪）**：`_clip_history` 是保留最新、丢弃较早——若套在增量上会丢中段而 committed 仍推进到 planned_through → 中段永久漏喂。改为：从 committed 之后最旧一条起按连续前缀截取本轮量，收尾 `committed_msg_id` 只推进到本轮实际连续喂达的最后一条 id（`committed_batch_end`），不直接推进到 planned_through;超上限在连续前缀处截断、剩余尾部下一轮从新起点续喂，**禁止跳段**;「连续前缀」= 排除自产后的 eligible message 连续扫描区间（不要求原始 ID 无空洞）;单条超预算消息至少完整投递一条不卡死
- [ ] 2.3b **🔴 backlog 自动续批（Review P0-4）**：run 成功且 `committed_batch_end < planned_through_msg_id` 时，**同事务**（连同 committed 推进 + session pointer 更新）建 `history_backlog` successor run（`trigger=history_backlog`/`history_batch_no`/`history_batch_end`/`history_backlog_from_execution_id`），不依赖新用户 @;successor 不计 mention-chain、受独立最大批次数/token 预算、复用同 session;全批消费完才清 backlog;达上限转人工提示不无限跑
- [ ] 2.4 `build_cli_prompt` 输入集从「全量历史」改为「增量历史」（格式复用【用户】/【队友】,仅数据源变）;增量为空时 prompt 仅本轮指令
- [ ] 2.5 `claude_code.py` 命中 session 时 `args += ["--resume", session_id]`
- [ ] 2.6 **resume 落地判定（Review P1-6：mismatch 只在失败时判）**：`失败 && (stderr "no conversation found" 或 emitted id≠请求 id)` → 判未落地 → 清 sid + 本次降级全量;`成功 && emitted id≠请求 id` → 正常（resume 后 fork 新 id）→ 接受并覆盖存，不判失败
- [ ] 2.7 探针 `agent_session_resume_probe`：二次执行带 `--resume`、prompt 只含增量、**并发场景**（A 跑期间 B 发言）下次触发 A 不漏 B 的话;**海量增量场景**（增量超单轮上限）分多轮连续前缀喂、committed 逐轮推进到 committed_batch_end、跨轮无跳段无遗漏;token/prompt 长度对比全量下降
- [ ] 2.7a **backlog 自动续批探针（Review 故障注入 7/8）**：`history_backlog_probe`(海量增量需 3 批、无新用户触发时仍自动建 successor 逐批消费完，committed 追平 planned_through)、`oversized_message_probe`(单条超预算消息至少完整投递一条、committed 越过不卡死)

## S3 — 降级链 + poisoned 丢 session（保证不劣于现状）
- [ ] 3.1 resume 未落地（S2.6 判定）→ 清 session_id → 降级全量 + 成功后重开新 session
- [ ] 3.2 **poisoned 失败丢 session**：定义 poisoned 集（`iteration_limit` 迭代上限 / `api_invalid_request` 模型 400 / `codex_semantic_inactivity` codex 语义静默）;命中 → 主动丢弃 prior session、下次从头重建（不 resume 坏状态）
- [ ] 3.3 provider/backend 变更 → 弃旧 session、开新 + 全量;workdir 变更 → 同;（未来多机）runtime 不匹配不 resume
- [ ] 3.4 探针 `agent_session_fallback_probe`：模拟 ① "no conversation found" ② poisoned 失败 ③ 换 provider ④ 换 workdir，均正确回退/丢弃、不报错、结果与现状等价

## S4 — codex app-server 集成（必选，与 claude 线并行）
- [ ] 4.1 `codex.py` 从一次性 `codex exec --json -m - -` 改为 **每次执行 attempt 起一个 `codex app-server --listen stdio://` 进程 + JSON-RPC**（run 结束即关，非 Worker 全局共享，Review P1-7）
- [ ] 4.2 `thread/resume`（带 `threadId`/`cwd`/`model`/`developerInstructions`）恢复线程;**resume 前 SHALL 检查 `CODEX_HOME` 下对应 rollout/thread 记录存在性 + workdir 一致性（Review P1-4）**——rollout 不存在或 workdir 不一致 → 不 resume、降级 `thread/start` 全量;可恢复协议错误（unknown thread/schema 漂移）回退 `thread/start`;传输/进程错误 fail-fast（不回退）
- [ ] 4.3 `turn/start` 发本轮实际 prompt（本轮指令 + 增量历史）;drain 流;捕获 `threadId` 作为 codex session_id 存 `agent_sessions`
- [ ] 4.4 握手超时（`HandshakeTimeout`）、进程组隔离、app-server 生命周期管理;复用 claude 线的降级链与 poisoned 分类
- [ ] 4.5 探针 `codex_session_resume_probe`：codex 二次执行 `thread/resume` 续接、协议错误回退 `thread/start`、poisoned/降级链生效

## 收尾
- [ ] 回归全量探针（mention/timeout/scheduling 等）确认 resume 改造 + 串行约束不回归协同/超时/调度行为
- [ ] 固化：本 change 完成并验证后,把 `agent-session-resume` 能力规格从 change delta 固化进 `specs/agent-session-resume/spec.md`,change 移入 `changes/archive/`
- [ ] 联动 [platform-graceful-restart]：其 M2.5「静默+resume」标注「resume 地基（含 claude/codex、pin 落库、续跑重发原 prompt）由 agent-session-resume 提供」
