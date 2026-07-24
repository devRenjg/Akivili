# 平滑重启 + Agent 任务恢复方案 —— 历轮问题回归门禁

**建立轮次**：第二十轮（2026-07-23）
**用户要求（原文）**：把之前轮次 Review 出来的问题记录下来作为这件事情的回归测试集合，之后所有的优化再完成后都过下之前出过问题的回归测试集才最终合入，以保障没有重复问题发生。

## 一句话

**每一轮优化完成、合入 `master` 之前，必须跑 `regression_gate.py` 且全绿。** 它把第 4~20 轮 Review 命中过的问题固化为可执行门禁，任一历轮问题若被重新引入即 exit 1、阻断合入。

## 怎么跑

```bash
# 合入前必跑（一条命令跑全部回归检查）
python openspec/changes/platform-graceful-restart/scripts/regression_gate.py

# 只看回归集清单（历轮问题 → 守卫映射）
python openspec/changes/platform-graceful-restart/scripts/regression_gate.py --list
```

退出码：`0` = 全绿、允许合入；`1` = 任一门禁失败、禁止合入。

## 门禁由 4 道检查组成

| # | 检查 | 防的是什么 |
|---|------|-----------|
| 1 | `openspec validate <change> --strict`（三份 change） | 结构 / Requirement / Scenario 非法 |
| 2 | `spec_consistency_probe.py --self-test` | 历轮旧口径 / 结构违规**正负样本单元测试**（回归集主体） |
| 3 | `spec_consistency_probe.py` 实扫三份 change | 真实正文出现历轮旧口径（期望 0 命中） |
| 4 | probe 规则覆盖断言 | 有人**静默删除** probe 规则后实扫仍 0 命中、旧问题却悄悄回归 |

> 第 4 道是第二十轮 reviewer 的明确担忧——"CI 全绿但旧 P0 重新进入方案"——的独立 backstop：断言每个历轮废弃字段仍被某条 probe pattern 守护。

## 回归集主体 = probe self-test

真正的"回归测试集合"是 `spec_consistency_probe.py` 里的 **FORBIDDEN + STRUCTURAL 规则 + `--self-test` 正负样本**。每轮 Review 命中的旧措辞在此都有：

- **正样本**：旧口径句 → 必须被拦（`any(k=='structural'...)` 为真）。
- **负样本**：对应的正确口径 → 必须放行（无命中）。

当前 self-test **106 条**全绿。

## 历轮问题 → 守卫映射

完整清单见 `regression_gate.py --list`（37 条回归项 + 5 个废弃字段覆盖断言）。要点：

| 轮次 | 代表性问题 | 守卫 |
|------|-----------|------|
| 4-8 | `committed_batch_end`/`prestart_failed`/`task_runs.lease_expires_at`/`winning_attempt_id` 旧命名 | probe FORBIDDEN |
| 9 | orphaned 直接回 queued（双执行） | STRUCTURAL orphaned→queued（segment） |
| 10 | gate 已释放却写 claimed→orphaned；protocol_incompatible 已起 CLI 回 queued | STRUCTURAL 三元一致性 |
| 11 | recovery_blocked 出现在 attempt 层 / →superseded 出边；token 冒充父级唯一 | STRUCTURAL 三类人工恢复 gate |
| 12 | 通用 sentiment allow 整段放行后门；NULL task 可执行；父流追加 manual_recovery | 删全局 allow + HISTORICAL_INVALID + 两条新规则 |
| 13 | HISTORICAL_INVALID marker 任意位置绕过 | marker 严格作用域（段首+引号+无现行措辞） |
| 15 | 自动 supersede 写序漏 recovery_resumed；SSE payload 用 recovery_source | STRUCTURAL R④/R⑤ |
| 17 | running NULL→abandoned；orphaned 恒 process_not_confirmed_dead；final=NULL 引用运行期字段；null migration 认 superseded_from；跨 token 无条件映射；写新预算值；reclaim 父恒带 blocked_reason | R17-1~7 |
| 18 | protocol 恒定局；task 完成度取单个最新 execution；无条件读回 child；增加或重置预算 | R18-1~4 |
| 19（**P0×2**） | running NULL 已确认清理写 abandoned；recovery_blocked 父重新排队；6 条 probe false-negative | R19-P0-1 / R19-P0-2 + 扩匹配 |
| 20 | probe allow 被同句无关 SHALL NOT/grant_delta 绕过（P1-6）；history_backlog 旧独立列；task 级 active 单值聚合；resolved cache 当真相源；terminal_source_status='unknown' | window 绑定 + R20-1~4 |
| 21（**P0×2**） | 全局自增 id/BIGSERIAL/MAX(messages.id) 充当已提交水位/续传游标（PG late-commit 越位）；断言 child 全局 id 必然大于父；successor 续订携带父全局 Last-Event-ID（两口径未归一）；三分列 partial unique + NOT EXISTS 保证并集唯一（READ COMMITTED 跨列并发双 child） | R21-1a（per-execution event_seq / conversation message_seq 行锁）+ R21-1b/1c（切 child + event_seq 起点回放、口径归一）+ R21-2（execution_edges 边表 UNIQUE(parent)） |

## 维护约定（每新增一轮 Review 必做）

1. 把该轮命中的旧口径补进 `spec_consistency_probe.py` 的 FORBIDDEN/STRUCTURAL + `--self-test` 正负样本。
2. 若废弃了某字段/命名，登记进 `regression_gate.py` 的 `REGRESSION_FORBIDDEN_FIELDS` 与 `REGRESSION_ROUNDS`。
3. 跑 `regression_gate.py` 确认全绿。
4. 才允许合入 `master`。

> 不新增门禁项、只改现有条目时，同样必须先跑本门禁全绿再合入。
