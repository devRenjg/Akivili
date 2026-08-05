# Design — agent-session-resume-minimal

> 本文只讲**技术抉择与「为什么能砍」**，不重复 proposal 的 What / tasks 的 How。三个抉择：① 增量水位的 at-least-once 语义；② best-effort 缓存的并发行为；③ 两阶段 session 查找顺序。附一节「相对废弃版砍了什么、为什么砍得掉」。

## 抉择一：增量水位 = committed + 快照终点两段，at-least-once

**目标**：resume 命中时只喂「上次以来的新消息」，而非全量重放（省 token 的收益兑现点）。

**两个水位**：
- `session_committed_msg_id`（持久，落 `task_runs` / `agent_sessions`）：**上次成功已消费到的最高 message id**。只在 run 成功收尾才推进。
- 快照终点 `snapshot_end`（临时，run 内算）：构建本次 prompt 那刻的 `MAX(messages.id)`。

**增量集**：`messages WHERE conversation_id=? AND id > committed AND id <= snapshot_end AND 作者非本 agent ORDER BY id`（参数化）。

**为什么是 at-least-once 而非 exactly-once**：
- 崩溃/中断发生在「已喂给 CLI 但 committed 未推进」之间 → 续跑时从**同一个 committed 起点**重取 → 那批消息**重复喂**一次。
- 这是**可接受的**：重复喂历史消息只是让模型多看一遍上下文，无副作用（不触发工具、不重复交付）；而「漏喂」才是真错误（模型永远看不到某段对话）。故设计选择 **committed 只在成功后推进**，宁可重复不可遗漏。
- 对标 Multica：Multica 靠 CLI 原生 session 记忆承载历史、不自管消息水位；我们比它多一层增量水位是因为**多 Agent 在同一 conversation 里 @ 来 @ 去**——A 的 session 记不住 B 说了什么，B 的发言必须由我们的增量回灌补给。这层不能砍（减法 Paper C2），但可以用最简的「两段水位 + at-least-once」而非废弃版的「per-conversation 行锁 message_seq」。

**为什么不用 message_seq 行锁**（废弃版 P0-1）：行锁 message_seq 是为解决「PG late-commit：小 id 消息迟提交、落在 committed 之后被永久漏灌」。那是 graceful-restart 高并发写场景的坑。本 change 的写入模型是**单 worker 串行消费队列**（worker-split-minimal 已落地），同一 conversation 的消息由同一 worker 顺序写，不存在并发 late-commit → 用 `messages.id` 自增序即可，无需 conversation 内行锁重排。**若未来上多 worker**，再引入 message_seq（届时 graceful-restart 也需要，一并做）。

## 抉择二：agent_sessions = best-effort 缓存，并发写后写覆盖

**目标**（阶段二）：同一 `(conversation, agent)` 的下一个 task 复用上一个 task 的 CLI 会话，省历史重放。

**并发模型 = best-effort，不做 owner 唯一性**：
- `agent_sessions` upsert 用 `ON CONFLICT (conversation_id, agent_slug) DO UPDATE`，**后写覆盖**，不加锁、不校验 owner。
- 最坏情况：两个 task 几乎同时收尾、都写同一 key → 后写者覆盖前写者的 session 指针。下一次 resume 可能指向「较旧那条」的 session → 该 session 若已被对方并发使用，resume 至多 **miss 一次 → 降级全量**（S4 降级链）。**无数据损坏、无脏状态**，只是偶发少省一次 token。

**为什么砍得掉废弃版的 owner 强一致机制**：
- 废弃版为 `agent_sessions` 引入了 session owner 四阶段 CAS（acquire→pin→final→retire）、串行折叠、run_queue.conversation_id 回填、两组互补 partial unique index、NULL task 三层硬门——几百行前置。其根源是把 session 当**强一致资源**，要保证「同 conversation 同 agent 至多一个 active run 持有 owner，迟到写不能覆盖新 owner」。
- 但 session 复用本质是**优化、不是正确性**：resume 命中省 token，miss 就降级全量、结果不劣于现状。既然 miss 无害，就没必要用 owner CAS 去消灭并发 miss——降级链已经兜住了。用强一致机制保护一个 best-effort 优化，是过度设计（减法 Paper 结论）。
- 边界诚实：这条取舍成立的前提是**「miss 一次降级全量」真的无害**。降级全量 = 回到现状路径（全量回灌），本 change 上线前现状就是这样跑的 → 无害成立。若未来某场景下「降级全量」有不可接受代价（如历史已超单轮上限、全量喂不下），那才需要重新评估 owner 控制——但那是 backlog 分批的问题，不是 owner 的问题。

## 抉择三：两阶段 session 查找顺序 = 先本 run，后 (conversation, agent)

`runner.py` 执行前查 session 的顺序：
1. **本 run 的 `cli_session_id`**（阶段一）：run 被打断后重跑（同一 run id 或其恢复来源 run），优先续同一 run 的会话——这是「同 run 续跑」，最强连续性。
2. **`agent_sessions` 的 `(conversation, agent)`**（阶段二）：本 run 无 session（全新 task）时，查该 (conversation, agent) 上次留下的会话——这是「跨 task 续接」。
3. **都没有** → 全量首建（现状）。

**为什么这个顺序**：同 run 续跑的 session 与本次执行意图**完全一致**（就是被打断的那次），连续性最强、最该优先；跨 task 续接是「换了个 task 但同一对话同一 agent」，连续性次之；全量首建是保底。三级递降，每级 miss 都平滑降到下一级，最终降到现状，不劣化。

**backend/provider/workdir 一致性校验**：任何一级命中后，都要校验 session 的 backend（claude/codex）、provider、workdir 与本次执行一致；不一致 → 视为 miss、降到下一级。（换了 CLI 或换了工作目录，旧 session 无意义。）

## 相对废弃版：砍了什么、为什么砍得掉

| 废弃版机制 | 本 change | 为什么砍得掉 |
|---|---|---|
| message_seq per-conversation 行锁水位 | 用 `messages.id` 自增序 + 两段水位 | 单 worker 串行写，无并发 late-commit（减法 Paper C2 的行锁前提不成立于单 worker） |
| attempt 级 fencing（current_attempt_id + 五重状态校验） | 无 | fencing 是 graceful-restart 防「残留 CLI 进程迟到写库」的机制，属那个 change；resume 本身不产生迟到写（session 复用是读优化） |
| session owner 四阶段 CAS | best-effort upsert 后写覆盖 | session 复用是优化非正确性，miss 降级全量无害（抉择二） |
| run_queue.conversation_id 回填 + 两组 partial unique index + NULL 三层硬门 | 无 | 这些是为 owner 唯一性 + NULL conversation 脏数据防御，owner 砍了它们就无存在理由 |
| NULL migration 三态状态机（几百行） | 无 | 同上，是 owner/串行折叠的衍生复杂度 |
| codex app-server + JSON-RPC thread/resume 重集成 | `codex exec resume <thread_id> -` | CLI 实测确认 exec resume 子命令直接可用、schema 不变（CLI 实测 Paper 2.3-2.4） |
| resume 后 id fork 的覆盖 + mismatch 判定 | 无（仅保留「失败+no conversation found」降级） | CLI 实测确认两线 id 不 fork（CLI 实测 Paper 第1节） |

**保留的**：增量回灌（多 Agent 模型必需，减法 Paper C2）、降级链（健壮性）、resume-unsafe 清单 + codex rollout 校验（Multica 生产验证的护栏，减法 Paper D2/D3）。

**结论**：本 change ≈ 废弃版的「主干 + 两个护栏」，去掉「为强一致 owner 与高并发写而生的全部前置」。那些前置属于 [platform-graceful-restart]，不属于 resume。
