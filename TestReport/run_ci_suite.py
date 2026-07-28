# -*- coding: utf-8 -*-
"""CI 聚合入口：一键跑全部隔离回归 probe + QA 主套件，任一失败即非零退出。

被 GitHub Actions（.github/workflows/ci.yml）和本地共用——probe 清单只在此维护一处，
不在 yaml 里重复。每个 probe 已自带失败退出码（sys.exit / SystemExit），本 runner
按子进程退出码判定成败，并抓打印的「N/N」计数汇总。

用法：
  python TestReport/run_ci_suite.py          # 全量门禁（CI 默认）
  python TestReport/run_ci_suite.py --list   # 只列清单不跑
  python TestReport/run_ci_suite.py --exclude-slow   # 跳过并发/压力类长跑 probe

隔离安全：所有列入的 probe 都在临时 config/DB/workspace 下跑、monkeypatch
runner.execute_dispatch，不碰真实 jianagency.db、不调真实 LLM/CLI。需真实 CLI 供应商的
run_collab_scenario.py / run_codex_cli_smoke.py **不在门禁内**（依赖外部，人工按需单跑）。
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"

# —— 门禁清单：40 项（39 隔离 probe + QA 主套件）。注释标注覆盖域，与 TestReport/README.md 对齐。——
# 排除（真实 CLI，非隔离桩）：run_collab_scenario.py / run_codex_cli_smoke.py
GATE = [
    # 主套件
    "run_qa_suite.py",                    # 平台主回归：登录/鉴权/脱敏/路径穿越/CRUD/看板/任务/Agent 配置
    # 数据底座 S1/S2
    "run_wal_concurrency_probe.py",       # S1：WAL/busy_timeout + 并发写零 locked + 连接收口
    "run_migration_probe.py",             # S2/S3.6：Alembic 建库逐字节对齐 + stamp 幂等 + 002 数据规整
    # 数据底座 S3：ORM 地基
    "run_orm_schema_parity_probe.py",     # 18 ORM 模型逐字段对齐 001 基线
    "run_orm_engine_probe.py",            # async engine/session 调优对齐 S1
    "run_dialect_helper_probe.py",        # now_expr/方言 helper 编译与等价
    # 数据底座 S3.4：逐文件迁移等价性
    "run_s34_memory_sync_probe.py",       # 批1 agent_memory_sync
    "run_s34_batch2_probe.py",            # 批2 auth/projects/activity
    "run_s34_batch3_probe.py",            # 批3 skills/routes.skills/routes.auth
    "run_s34_batch4_probe.py",            # 批4 agents/agent_config + upsert
    "run_s34_batch5_probe.py",            # 批5 agent_cli/project_agents
    "run_s34_batch6_probe.py",            # 批6 routes.agents/reflect
    "run_s34_batch7_probe.py",            # 批7 progress
    "run_s34_batch8_probe.py",            # 批8 routes/tasks
    "run_s34_batch9_probe.py",            # 批9 routes/runs（julianday/窗口方言点）
    "run_s34_batch10_probe.py",           # 批10 executor/runner
    "run_s34_batch11_probe.py",           # 批11 collab（_claim_one/孤儿回收/idle sweep）
    "run_s34_crosspath_probe.py",         # ORM↔aiosqlite 跨路径共存
    # 平台核心行为回归
    "run_lineage_probe.py",               # 端到端链路下钻
    "run_scheduling_probe.py",            # 调度/优先级/退避
    "run_scheduling_events_probe.py",     # run_events 调度流水
    "run_rate_limit_probe.py",            # 限流/429 归因
    "run_orphan_reclaim_probe.py",        # 启动孤儿回收
    "run_orphan_leak_probe.py",           # 孤儿泄漏兜底
    "run_reactivate_probe.py",            # 重派状态流转
    "run_task_gates_probe.py",            # 任务状态闸
    "run_subtask_autocomplete_probe.py",  # 子任务全完成→父推进
    "run_mention_chain_reset_probe.py",   # @链循环闸 + 产出重置
    "run_mention_prompt_probe.py",        # @解析与 prompt 构造
    "run_reflect_probe.py",               # 复盘沉淀
    "run_reflect_participants_probe.py",  # 复盘参与者
    "run_reflect_observability_probe.py", # 复盘可观测性
    "run_stdout_display_probe.py",        # CLI/API 产出落库分流
    "run_agents_overview_probe.py",       # Agent 总览聚合
    "run_timeout_and_qa_probe.py",        # 超时保成果 + QA 收尾 prompt
    "run_memory_hygiene_probe.py",        # 记忆卫生
    "run_skill_downloadable_probe.py",    # 技能可下载标记
    "run_stale_pid_kill_probe.py",        # 陈旧 pid 防误杀
    "run_pipe_deadlock_probe.py",         # 管道死锁兜底
    "run_concurrency_probe.py",           # 并发池（较慢）
]

# 长跑/压力类：--exclude-slow 时跳过（快速门用）
SLOW = {"run_concurrency_probe.py", "run_wal_concurrency_probe.py"}

_COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_PASSFAIL_RE = re.compile(r"PASS\s*=\s*(\d+)\s+FAIL\s*=\s*(\d+)")


def _extract_counts(output: str):
    """从 probe 打印里抓「passed/total」计数：优先 PASS=N FAIL=M，其次末尾 N/N。"""
    pf = None
    for m in _PASSFAIL_RE.finditer(output):
        pf = (int(m.group(1)), int(m.group(1)) + int(m.group(2)))
    if pf:
        return pf
    nn = None
    for m in _COUNT_RE.finditer(output):
        nn = (int(m.group(1)), int(m.group(2)))
    return nn  # 可能 None（个别 probe 不打计数，只靠退出码）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列门禁清单，不执行")
    ap.add_argument("--exclude-slow", action="store_true", help="跳过并发/压力类长跑 probe")
    args = ap.parse_args()

    gate = [p for p in GATE if not (args.exclude_slow and p in SLOW)]

    if args.list:
        print(f"CI 门禁 probe 清单（{len(gate)} 项）：")
        for p in gate:
            tag = " [slow]" if p in SLOW else ""
            print(f"  - {p}{tag}")
        print("\n排除（真实 CLI，非门禁）：run_collab_scenario.py, run_codex_cli_smoke.py")
        return 0

    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    results = []   # (name, ok, passed, total, secs)
    t0 = time.perf_counter()
    print(f"=== CI 回归门禁：{len(gate)} 项（cwd={BACKEND}）===\n")

    for name in gate:
        script = HERE / name
        if not script.exists():
            print(f"[MISS] {name} — 脚本不存在")
            results.append((name, False, 0, 0, 0.0))
            continue
        s = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(BACKEND), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        secs = time.perf_counter() - s
        counts = _extract_counts((proc.stdout or "") + "\n" + (proc.stderr or ""))
        ok = proc.returncode == 0
        passed, total = counts if counts else (None, None)
        results.append((name, ok, passed, total, secs))
        badge = "PASS" if ok else "FAIL"
        cnt = f"{passed}/{total}" if counts else "(无计数)"
        print(f"[{badge}] {name:<38} {cnt:>10}  {secs:5.1f}s")
        if not ok:
            # 失败时打印尾部输出便于定位
            tail = "\n".join((proc.stdout or "").splitlines()[-15:])
            errtail = "\n".join((proc.stderr or "").splitlines()[-10:])
            print(f"  ---- stdout 尾 ----\n{tail}")
            if errtail.strip():
                print(f"  ---- stderr 尾 ----\n{errtail}")

    total_secs = time.perf_counter() - t0
    failed = [r for r in results if not r[1]]
    total_pass = sum(r[2] or 0 for r in results)
    print("\n" + "=" * 64)
    print(f"门禁 {len(gate)} 项 · 通过 {len(gate) - len(failed)} · 失败 {len(failed)} · "
          f"断言累计 {total_pass} PASS · 用时 {total_secs:.1f}s")
    if failed:
        print("\n失败项：")
        for n, _, p, t, _s in failed:
            print(f"  [X] {n}  ({p}/{t})" if t else f"  [X] {n}")
        return 1
    print("[OK] CI 回归门禁全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
