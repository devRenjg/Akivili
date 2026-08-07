# Session Resume 端到端实测：token 与记忆连续性

**日期**：2026-07-24　**方式**：真起 claude-cli + codex-cli，隔离 PG 库 + 隔离 config，直接驱动 `runner.execute_dispatch`（不经 HTTP 层）
**探针**：`run_resume_e2e_probe.py`（一次性联调探针，**验证完毕后已移除**——本 Paper 即其结论存档）
**产物**：`resume_e2e_report.txt`（当时逐项 PASS/FAIL + 真实 token 数，见下表）

> 该探针是一次性联调工具（真起 CLI 消耗真实 token，claude opus 单轮 ~0.3 USD），非回归资产。
> 结论已固化于本 Paper；如需复现，按下方「实测场景」重建探针即可（隔离 PG 库 + 注入真实 provider + 驱动
> `runner.execute_dispatch`）。功能正确性的持续回归由门禁内 `run_cross_task_resume_probe.py`（隔离桩）守护。

## 实测场景

阶段二「跨 task 续接」——同一 `(conversation, agent)`，两个独立 task：
1. **任务A（全量首建）**：prompt「记住暗号：紫色犀牛-7391，回复『已记住』确认」→ 收尾写 `agent_sessions` 缓存
2. **任务B（跨 task resume）**：全新 task、同 conv 同 agent → `runner` S5.3 命中 `agent_sessions` → `--resume` / `exec resume` 续接 → prompt「刚才的暗号是什么？」

claude、codex 各跑一遍（两线 resume 机制不同：claude 预分配 UUID / codex 抓 thread_id + rollout 校验）。

## 结果总览：17/17 PASS

| 维度 | claude | codex |
|------|:------:|:-----:|
| 任务A run 成功 | ✅ | ✅ |
| 任务A 捕获 session_id 落 task_runs | ✅ | ✅ |
| 任务A 后 agent_sessions 有缓存行 | ✅ | ✅ |
| thread rollout 文件在位 | —（claude 无此概念） | ✅ |
| 任务B run 成功 | ✅ | ✅ |
| 任务B resume 复用任务A 的 session | ✅（同 id） | ✅（同 id） |
| **会话记忆连续：答出暗号** | ✅ 紫色犀牛-7391 | ✅ 紫色犀牛-7391 |

## 两条证据链

### ① 功能正确性（硬证据）：会话记忆连续
两 CLI 在**任务B**都准确答出了**只在任务A 对话里出现**的暗号「紫色犀牛-7391」。
该暗号不在 persona / 系统提示 / 任务B 的 prompt 里——唯有 CLI 会话**真的续上**才答得出。
若走全量重建（增量 history 不含 A 的完整上下文），答不出。**这直接证明 resume 机制真实生效。**

### ② token 观测（如实记录）：短会话 resume **不省反增**

| 后端 | 任务A 全量首建（总输入 token） | 任务B resume（总输入 token） | 变化 |
|------|--:|--:|--:|
| claude | 41041 | 41156 | **+115** |
| codex | 42452 | 91440 | **+48988（+115%）** |

> 口径 = 总输入 token（claude = input + cache_creation + cache_read；codex = input_tokens）。
> 落 `task_runs.usage_input_tokens`（迁移 008 起捕获，claude=result / codex=turn.completed 事件）。

## 反直觉发现与根因（重要，修正原设计假设）

design.md 抉择二原写「跨 task 续接省历史重放」。实测**推翻了这个假设在短会话下的成立**：

**根因**：本平台的两种模式对比，不是教科书式的「全量历史 vs 增量历史」，而是：
- **全量首建**：平台把 `_clip_history`（条数+字符双限裁剪后的**精简** history）拼进 prompt 喂给 CLI
- **resume**：让 CLI 从磁盘 rollout 文件恢复**完整会话状态**——A 的全部轮次、工具调用、系统上下文

在**短会话**里，CLI 恢复的完整状态 **>** 平台裁剪后的精简 history，所以 resume 的总输入 token 反而更高
（codex 尤甚：+115%）。claude 差异小（+115 token）是因 opus 的常驻系统开销本就大，掩盖了差值。

**修正后的正确结论**：
- resume 省 token 的**前提** = 平台重放的 history ≥ CLI 恢复的会话状态，即**长对话**（多轮累积、裁剪后仍很大）。
- **短对话**下 resume 的真实价值是**会话连续性 / 上下文保真**（记忆不丢、CLI 内部状态延续），**不是省 token**。
- 对 best-effort 取舍**无影响**：resume 是优化/连续性而非正确性，miss 降级全量仍无害；只是「优化」的收益边界比原假设窄。

## 落地影响

- 阶段二**保留**：会话连续性价值真实存在（记忆连续实测证明），且不劣于现状（miss 降级全量）。
- 文档**去掉**「短会话省 token」的宣称；design.md 抉择二加实测修正块。
- 探针 token 断言 = **如实记录不卡阈值**（cache 行为受 5min TTL 干扰、单次硬阈值本质 flaky）；
  功能正确性由「会话记忆连续」硬判定。
- **潜在后续（非本 change，留 backlog）**：若要兑现 token 收益，给 S5.3 加「平台 history 规模超阈值才走
  跨 task resume」门控——短会话不 resume（省下的重放本就少、resume 恢复完整状态反而更贵）。

## 附：永久能力

迁移 008 起 `task_runs.usage_input_tokens / usage_cached_input_tokens / usage_output_tokens` 落库真实用量，
不止用于本次实测——是长期**成本可观测**基础设施（每个 CLI run 的真实 token 消耗均可查）。

*注：联调探针已移除（一次性工具，非回归资产）。token 捕获生产能力（迁移 008 + 后端 usage 提取落库）
保留在主线。跨 task 续接的持续回归由 `run_cross_task_resume_probe.py`（门禁内隔离桩）守护。*
