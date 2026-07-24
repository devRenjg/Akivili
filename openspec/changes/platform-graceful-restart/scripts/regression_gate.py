#!/usr/bin/env python3
"""regression_gate —— 平滑重启 + Agent 任务恢复方案「历轮问题回归门禁」（第二十轮建立）。

用户要求（第二十轮）：把历轮 Review 出过的问题编码为**永久回归测试集**，之后每轮优化
完成、合入 master **之前**都必须整体过一遍，杜绝已修问题重复发生。

本脚本是那道门禁的**单一入口**——一条命令跑全部回归检查，任一失败 exit 1：

  1. openspec validate --strict（三份 change 各一次）——结构/Requirement/Scenario 合法。
  2. spec_consistency_probe --self-test——历轮旧口径/结构违规的**正负样本单元测试**
     （第 9~20 轮的 forbidden + structural 规则全部自测，含第二十轮 P1-6 的窗口绑定与
     4 条跨真相源规则）。这是回归集的**主体**：每轮 Review 命中的旧措辞都在此有正样本
     （旧口径被拦）+ 负样本（正确口径放行）。
  3. spec_consistency_probe 实扫三份 change——真实正文 0 命中。
  4. probe 规则覆盖断言——REGRESSION_FORBIDDEN_FIELDS 中每个历轮已废字段/命名 SHALL
     仍被 probe 的某条 FORBIDDEN/STRUCTURAL pattern 守护，防有人静默删规则后实扫仍 0
     命中、旧口径却重新进入方案（第二十轮 reviewer 明确担忧）。

用法：
    python3 regression_gate.py            # 跑全部门禁（合入 master 前必跑）
    python3 regression_gate.py --list     # 只打印回归集清单（历轮问题→守卫映射）

退出码：0 = 全绿可合入；1 = 任一门禁失败，不得合入。

维护约定：**每新增一轮 Review**，把该轮命中的旧口径同时补到
  - spec_consistency_probe 的 FORBIDDEN/STRUCTURAL + --self-test 正负样本，
  - 本文件 REGRESSION_ROUNDS（清单）与（如是字段级废弃）REGRESSION_FORBIDDEN_FIELDS，
再跑本门禁确认全绿，才允许合入。清单见 REGRESSION_GATE.md。
"""
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
CHANGES_DIR = SCRIPTS_DIR.parents[1]  # openspec/changes
REPO_ROOT = CHANGES_DIR.parents[1]     # repo root
PROBE = SCRIPTS_DIR / "spec_consistency_probe.py"

CHANGES = [
    "platform-graceful-restart",
    "agent-session-resume",
    "platform-concurrency-scaling",
]

# ── 历轮问题→守卫映射（回归集清单，仅文档/说明用，实际拦截在 probe + 下方字段清扫） ──
# 每条：round=轮次；problem=问题一句话；guard=当前由哪条守卫防复发。
REGRESSION_ROUNDS = [
    # 第四~八轮：早期 P0/P1 多为「架构缺失」（原子领取、fencing、两阶段水位、备份等），
    # 已在 spec/design 正文成为硬 Requirement，靠 openspec validate + 正文评审守护;
    # 其中沉淀为「旧口径不得复现」的，进 probe FORBIDDEN（见下）。
    ("4-8", "committed 水位=最后实际喂达 id（应为原始扫描水位 batch_scan_end）", "FORBIDDEN committed_batch_end / batch_scan_end 改名"),
    ("4-8", "prestart_failed 单列状态", "FORBIDDEN prestart_failed 归一 failed+failure_stage"),
    ("4-8", "task_runs.lease_expires_at 读不存在字段", "FORBIDDEN task_runs.lease_expires_at"),
    ("4-8", "winning_attempt_id / 获胜 attempt 旧命名", "FORBIDDEN winning_attempt_id / 获胜 attempt"),
    ("9", "orphaned 直接回 queued（双执行）", "STRUCTURAL orphaned→queued（segment）"),
    ("9", "无 session run 落 failed", "FORBIDDEN 无 session…落 failed"),
    ("10", "gate 已释放却写 claimed→orphaned（三元不一致）", "STRUCTURAL gate 已释放 claimed→orphaned"),
    ("10", "protocol_incompatible 已起 CLI 回 queued", "STRUCTURAL protocol_incompatible 已起 CLI→queued"),
    ("11", "recovery_blocked 出现在 attempt 层 / →superseded 出边", "STRUCTURAL attempt recovery_blocked / recovery_blocked→superseded"),
    ("11", "manual_recovery_token 冒充父级唯一约束", "STRUCTURAL UNIQUE(...manual_recovery_token...) 保证一父一子"),
    ("12", "通用 STRUCTURAL_ALLOW sentiment 词整段放行后门", "已删全局 allow + 每规则精确 allow + HISTORICAL_INVALID"),
    ("12", "NULL task 固定 full replay 可执行", "STRUCTURAL NULL task 可执行/固定 full replay"),
    ("12", "父 terminal 后向父流追加 manual_recovery", "STRUCTURAL 父流追加 manual_recovery"),
    ("13", "HISTORICAL_INVALID marker 任意位置绕过", "marker 严格作用域（段首+引号+无现行措辞）"),
    ("15", "自动 supersede 写序漏 child recovery_resumed", "STRUCTURAL superseded→child queued 缺 recovery_resumed"),
    ("15", "SSE event payload 用 recovery_source（应为 source）", "STRUCTURAL SSE payload recovery_source"),
    ("17", "running NULL 确认退出后 attempt 改 abandoned", "R17-1 / R19-P0-1 running NULL 恒 orphaned"),
    ("17", "orphaned blocked_reason 恒 process_not_confirmed_dead", "R17-2 orphaned 按来源子类分"),
    ("17", "final=NULL 例外引用运行期 source_status", "R17-3 引用持久列 terminal_source_status"),
    ("17", "null migration resolved 派生认 superseded_from", "R17-4 认 migration_from_execution_id"),
    ("17", "不同 token 输家无条件映射赢家 child", "R17-5 / R18-1 按 canonical_payload_hash 分流"),
    ("17", "人工补预算写新预算值/覆盖 remaining", "R17-6 / R18-4 只保留 grant_delta"),
    ("17", "reclaim 承接 superseded 父恒带 blocked_reason", "R17-7 superseded 父无 blocked_reason"),
    ("18", "protocol_incompatible abandoned 恒定局", "R18-3 按预算拆（未耗尽非定局/耗尽定局）"),
    ("18", "task 完成度取单个最新终态 execution", "R18-2 全因果叶子优先级聚合"),
    ("19", "tasks 把 running NULL 已确认清理写 abandoned（P0）", "R19-P0-1 running NULL 恒 orphaned（无 attempt 关键词亦拦）"),
    ("19", "recovery_blocked 父允许重新排队（P0）", "R19-P0-2 终态无出边、只建 child"),
    ("19", "probe 6 条 false-negative（斜杠增加/重置、裸 SHALL NOT 放行等）", "R18-1/2/4 扩匹配 + 6 条负样本自测"),
    ("20", "probe allow 被同句无关 SHALL NOT/grant_delta 绕过（P1-6）", "window=True 命中处前窗口绑定 + 3 条混合绕过自测"),
    ("20", "history_backlog_from_execution_id 旧独立列残留（P1-2）", "R20-1 归一入 continuation_from_execution_id"),
    ("20", "task 级 active 单值聚合旧口径（P1-2）", "R20-2 全因果叶子优先级聚合"),
    ("20", "resolved cache 当真相源/必写（P1-3）", "R20-3 因果键 EXISTS 唯一权威、cache 纯派生"),
    ("20", "terminal_source_status='unknown' 二选一（P1-4）", "R20-4 值域三值 + quarantine 隔离表"),
    ("21", "全局自增 id/BIGSERIAL/MAX(messages.id) 充当已提交水位/续传游标（P0-1，PG late-commit 越位）", "R21-1a per-execution event_seq(run_queue 行锁)/conversation message_seq(conversations 行锁)"),
    ("21", "断言「child 全局 id 必然大于父」跨 execution 可比假设（P0-1）", "R21-1b 父子各 per-execution event_seq 单调、不需跨 execution 可比"),
    ("21", "successor 续订携带父全局 Last-Event-ID（P0-1，人工/自动两口径未归一）", "R21-1c 统一切 child execution_id、从 child event_seq 起点回放"),
    ("21", "三分列 partial unique + 中心化 NOT EXISTS 保证并集唯一（P0-2，PG READ COMMITTED 跨列并发双 child）", "R21-2 execution_edges 边表 UNIQUE(parent_execution_id) 为并集唯一真相源"),
]

# ── 历轮废弃字段/命名清单：probe 覆盖断言（_probe_coverage）据此校验每个字段仍有守卫 ──
# 与 probe 互补：probe 负责「实扫拦截旧口径」，本清单负责「断言规则没被静默删除」。
# 新增一轮若废弃了某字段，在此登记，coverage 断言即要求 probe 补上对应 pattern。
REGRESSION_FORBIDDEN_FIELDS = [
    "history_backlog_from_execution_id",  # 第二十轮 P1-2：归一入 continuation_from_execution_id
    "winning_attempt_id",                 # 早期：改名 final_attempt_id
    "committed_batch_end",                # 第四轮：改名 batch_scan_end
    "prestart_failed",                    # 第六轮：归一 failed + failure_stage=prestart
    "task_runs.lease_expires_at",         # task_runs 无 lease 字段
]


def _resolve(argv):
    """把 argv[0] 解析成可执行文件绝对路径（Windows 下 openspec 是 .cmd shim，
    subprocess 无法直接 spawn 裸名，需 shutil.which 找到 .cmd/.exe 全路径）。"""
    exe = shutil.which(argv[0])
    if exe:
        return [exe] + argv[1:]
    return argv


def _run(desc, argv):
    """跑一条子命令，回显摘要，返回 (ok, rc)。"""
    print(f"▶ {desc}")
    proc = subprocess.run(_resolve(argv), cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8")
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    for ln in tail:
        print(f"    {ln}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-5:]
        for ln in err:
            print(f"    ! {ln}")
    ok = proc.returncode == 0
    print(f"    {'✅' if ok else '❌'} {desc}")
    return ok, proc.returncode


def _probe_coverage():
    """probe 规则覆盖断言：每个历轮已废字段/命名 SHALL 仍被 probe 的某条
    FORBIDDEN/STRUCTURAL pattern 守护。

    动机（第二十轮 reviewer 明确担忧）：真实正文 0 命中不代表 probe 没被削弱——
    若有人静默删掉某条规则，实扫仍会 0 命中，旧口径却能重新进入方案。故这里**不再重扫
    正文**（那与 probe real-scan 重复、且 allow 逊于 probe），改为断言 probe 自身仍
    编码了对每个历轮废弃字段的守卫，作为「防规则被静默删除」的独立 backstop。
    """
    print("▶ probe 规则覆盖断言（历轮废弃字段仍被 probe 守护）")
    # 复用 probe 模块的规则表，避免重复实现 allow 逻辑。
    sys.path.insert(0, str(SCRIPTS_DIR))
    import spec_consistency_probe as probe  # noqa: E402
    all_patterns = " || ".join(r["pattern"] for r in probe.FORBIDDEN + probe.STRUCTURAL)
    missing = [f for f in REGRESSION_FORBIDDEN_FIELDS
               if f not in all_patterns and f.replace(".", r"\.") not in all_patterns]
    if missing:
        for f in missing:
            print(f"    ❌ 历轮废弃字段 {f} 已无对应 probe 规则（规则疑被删除，回归风险）")
        print(f"    ❌ probe 规则覆盖（{len(missing)} 个废弃字段失去守卫）")
        return False
    print(f"    ✅ probe 规则覆盖（{len(REGRESSION_FORBIDDEN_FIELDS)} 个废弃字段均有守卫）")
    return True


def print_list():
    print("📋 历轮问题回归集清单（round → problem → guard）\n")
    cur = None
    for rnd, problem, guard in REGRESSION_ROUNDS:
        if rnd != cur:
            print(f"── 第 {rnd} 轮 ──")
            cur = rnd
        print(f"  • [{problem}]")
        print(f"      守卫: {guard}")
    print(f"\n共 {len(REGRESSION_ROUNDS)} 条回归项;probe 覆盖断言守护 {len(REGRESSION_FORBIDDEN_FIELDS)} 个废弃字段。")
    print("主体拦截在 spec_consistency_probe（FORBIDDEN + STRUCTURAL + --self-test）。")


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    if "--list" in argv:
        print_list()
        return 0

    print("🚦 regression_gate —— 历轮问题回归门禁（合入 master 前必跑）\n")
    results = []

    # 1. openspec validate --strict × 3
    for change in CHANGES:
        ok, _ = _run(f"openspec validate {change} --strict",
                     ["openspec", "validate", change, "--strict"])
        results.append((f"validate {change}", ok))

    # 2. probe --self-test
    ok, _ = _run("spec_consistency_probe --self-test",
                 [sys.executable, str(PROBE), "--self-test"])
    results.append(("probe self-test", ok))

    # 3. probe 实扫（0 命中）
    ok, _ = _run("spec_consistency_probe 实扫三份 change（期望 0 命中）",
                 [sys.executable, str(PROBE)])
    results.append(("probe real-scan", ok))

    # 4. probe 规则覆盖断言（防规则被静默删除）
    results.append(("probe coverage", _probe_coverage()))

    passed = sum(1 for _, ok in results if ok)
    print("\n📊 regression_gate 汇总")
    for name, ok in results:
        print(f"   {'✅' if ok else '❌'} {name}")
    print(f"   {passed}/{len(results)} 门禁通过")
    if passed == len(results):
        print("✅ 回归门禁全绿——允许合入 master")
        return 0
    print("❌ 回归门禁未全绿——禁止合入，先修复上述失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
