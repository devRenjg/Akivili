# Tasks — platform-graceful-restart

> 规划态,分期落实,每期独立走探针验证、独立提交。M2.5 依赖 [agent-session-resume] 先出 resume 能力。

## 跨-change 执行顺序（已确认，风险低→高、价值早兑现）

两个 change 合并成 8 个可独立验证的落实步,**每步验证门通过才进下一步**;每步含「代码 + 新探针 + 全量回归」,一步一提交、可独立回退:

| 步 | 内容 | 依赖 | 验证门 |
|---|------|------|------|
| **1** | **PGR-M1**（起点） WAL + SSE 尾随续传 | — | `sse_tail_probe` + 全量回归,流式不退化 |
| 2 | ASR-S1 建表 + 同(task,agent)串行 + claude session_id 抓取 | — | `agent_session_build_probe`,串行不双跑 + 回归 |
| 3 | ASR-S2 claude resume + 快照水位增量 | 步2 | `agent_session_resume_probe`,**并发不漏话** + token 降 + 回归 |
| 4 | ASR-S3 降级链 + poisoned 丢 session | 步3 | `agent_session_fallback_probe`,4类降级不劣于现状 |
| 5 | ASR-S4 codex app-server + thread/resume | 步2 | `codex_session_resume_probe` + 回归 |
| 6 | PGR-M2 worker 剥离 + 状态外置 + kill 标记 | — | `worker_split_probe`,停 API 不断 Agent + 双进程回归 |
| 7 | **PGR-M2.5**（汇合点） defer 5min + 交棒 + 续跑 | 步4/5 + 步6 | `worker_handover_probe`(7子项) + 回归 |
| 8 | PGR-M3 Nginx 蓝绿 | 步6 | 手动压测切换,SSE/请求不断 |

**执行纪律**：① 验证门不过不推进;② 每步可独立回退（不影响已达成能力）;③ 需重启验证先问用户、按单实例流程;④ DB 建表/加列前先备份 `jianagency.db`。**本 change 的 M1/M2/M2.5/M3 = 上表步 1/6/7/8;ASR 的 S1-S4 = 步 2/3/4/5。**

## M1 — 流式与并发地基（WAL + SSE 尾随续传，不做文件唯一源/同步器）
- [ ] 1.1 SQLite 连接建立时开 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout`（与 concurrency 0.1 同一件事,择一先做）
- [ ] 1.2 SSE 端点改为**尾随 `run_logs`**：查 `run_logs WHERE run_id=? AND id > <前端已收最大 id> ORDER BY id`,轮询(~200ms)推新增行;CLI 写 run_logs 的路径(Worker 线程→`_log()`)**不动**
- [ ] 1.3 SSE 断点续传：前端重连带「已收最大 log id」,从该点续推,不重不丢;run 未起发「排队中」占位、已终态一次性回放全量 + 收尾态
- [ ] 1.4 新增探针 `sse_tail_probe`：CLI 写 run_logs → SSE 尾随可见;断连带 last-id 重连从断点续、不重不丢;run 终态回放全量
- [ ] 1.5 回归全量探针,确认流式体验不退化(stdout_display 等)、读 run_logs 逻辑不回归

## M2 — 执行层剥离（核心：重启 API 不断 Agent）
- [ ] 2.1 `_running` 内存计数 → 改查 DB（`SELECT COUNT(*) ... status='running'`）,并发上限判断走 DB
- [ ] 2.2 新增 `worker.py` 入口：`reclaim_orphan_runs` + `_loop` 并发池 + 孤儿巡检,不起 HTTP
- [ ] 2.3 API `startup` 不再 `start_loop()`,只保留建库/seed/静态托管;塞队列的路径不变
- [ ] 2.4 pid 落 `task_runs.pid`（已存在字段）,Worker 起 CLI 后写入,供跨进程可见与重启兜底
- [ ] 2.5 kill 改「标记法」：API 写 `kill_requested`（run_queue/task_runs 加列或轻表）→ Worker 轮询到 → 用进程内 pid 指纹 `taskkill /F /T` 杀树 + 落终态;沿用创建时间指纹防复用误杀;kill **不触发续跑**（用户主动停）
- [ ] 2.6 新增探针 `worker_split_probe`：① 两进程从共享队列原子领取不重复 ② API 写 kill 标记 Worker 杀成功 ③ 停 API 保 Worker 后进行中的 run 仍推进到终态（模拟 API 重启不断 Agent）
- [ ] 2.7 更新启动脚本/文档：分别拉起 API 与 Worker;`reclaim_orphan_runs` 归 Worker 启动时做
- [ ] 2.8 回归全量探针（concurrency/orphan/timeout/scheduling/mention 等）在「API+Worker 双进程」下通过

## M2.5 — 温和重启 + resume 续跑（核心：改执行层重启不丢上下文；依赖 agent-session-resume）
- [ ] 2.5.1 **前置确认**：[agent-session-resume] 的 S1/S2/S3/S4 已落地——在跑 run 有可用 `session_id`（claude 与 codex 均已接入 resume，且流中途 pin 落库）
- [ ] 2.5.2 **温和重启 defer 窗口**：重启意图到达 → 停领新活 → 轮询 `activeTasks`,全部在跑 run 自然收尾即零中断重启;等待上限 **5 分钟**（参数化可调）内未清零才转交棒硬路径
- [ ] 2.5.3 交棒标记（Worker 级,与 kill 标记同 DB 轮询机制,语义相反：交棒续、kill 不续）：`task_runs`/轻表加列
- [ ] 2.5.4 旧 Worker 收到交棒标记 → 停领新活 → 对每个在跑 run：`taskkill /F /T` 杀 CLI 进程树(pid 指纹防误杀) + 旧 run 落 `superseded`（**不触发自动流转**：不误判子任务 done/父任务 reviewing）+ 依 `agent_sessions` 有无可用 session_id 入队「续跑 run」(带 `superseded_from` + resume 意图 + **系统恢复标记**) 或普通重跑(无 session) → 退出
- [ ] 2.5.5 新 Worker 领取「续跑 run」→ **重发原始任务 prompt**（重新 `build_cli_prompt`）+ 走 [agent-session-resume] 的 resume 路径起 CLI;resume 未落地时 prompt 前置「新会话」披露（不喂空/不造「继续」指令）
- [ ] 2.5.6 **续跑豁免防死循环配额（失败原因白名单区分）**：续跑 run 标记为系统恢复类 → 豁免 `MAX_MENTION_CHAIN` 空转链计数、不占 `MAX_RUNS_PER_TASK`;与 Agent 自发 @ 严格区分
- [ ] 2.5.7 **副作用重复防护（不做服务端 exactly-once）**：续跑靠 session 记忆 + prompt 约束（「你之前可能已提交过，先检查再动手」「只做一次，即便非零退出也不重试」）防重复建卡/comment/改状态;关键写操作可加轻量自然去重，不追求精确幂等
- [ ] 2.5.8 `reclaim_orphan_runs` 增强：落终态某 running run 时若有 `session_id` → 追加入队续跑 run（带 `superseded_from` 幂等标 + 系统恢复标记）,覆盖非交棒的硬崩溃;poisoned 已丢 session 的 run 不续跑
- [ ] 2.5.9 防双续幂等：以「旧 run 是否已有 `superseded_from=该run` 的子 run」为幂等键,交棒杀 + reclaim 兜底对同一 run 至多入队一条续跑
- [ ] 2.5.10 无 session 兜底：首次执行无 session_id / poisoned 已丢 session 的 run → 落 failed 重排队(现状,等于从头重跑该次分派)
- [ ] 2.5.11 更新 Worker 重启脚本：发重启意图 → defer 等空闲窗口 →（超时才）发交棒标记 → 等旧 Worker 退（杀 CLI + 入队续跑完成）→ 起新 Worker(新代码)
- [ ] 2.5.12 新增探针 `worker_handover_probe`：① 有在跑 run 先 defer、收尾后零中断重启 ② defer 超时转交棒、旧 run 落 superseded 且不触发自动流转、续跑带 resume 意图+系统恢复标记 ③ 新 Worker 重发原 prompt 以 resume 续、上下文延续 ④ 防双续:交棒+reclaim 对同一 run 只生成一条续跑 ⑤ 无 session 走从头重跑兜底 ⑥ kill 标记不触发续跑 ⑦ 续跑不吃 mention-chain/runs-per-task 配额
- [ ] 2.5.13 回归全量探针在「交棒重启 Worker」场景下通过

## M3 — 连接平滑（Nginx 蓝绿）
- [ ] 3.1 新增 `deploy/nginx.conf`：`upstream` 指向 API 实例,反代 `:8100`,SSE 需 `proxy_buffering off` + 合理超时
- [ ] 3.2 蓝绿脚本：起新 API 实例(新端口)→ `/api/health` 通过 → 切 upstream → `nginx -s reload` → 旧 API 排空退出
- [ ] 3.3 手动验证：压测/SSE 进行中执行蓝绿切换,连接不断、请求不报错、Agent 不受影响
- [ ] 3.4 文档：更新 README 部署段(Nginx + API + Worker 三者拉起顺序);记忆 `backend-restart-single-instance` 的手动流程由 Nginx reload 取代

## 收尾（每期完成后）
- [ ] 固化：本 change 完成并验证后,把 `platform-graceful-restart` 能力规格从 change delta 固化进 `specs/platform-graceful-restart/spec.md`,change 移入 `changes/archive/`
- [ ] 联动更新 [platform-concurrency-scaling]：其 2.1（状态外置）标注「由 graceful-restart M2 提供」、2.2（多 worker 无状态消费）标注「M2 已出可多进程消费地基,concurrency 在其上做并发规模与公平」,避免重复实现
