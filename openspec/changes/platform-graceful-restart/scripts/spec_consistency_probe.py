#!/usr/bin/env python3
"""spec_consistency_probe —— 三份 change 全文静态一致性扫描（Review 第八轮 P1-F）。

在 CI / 本地对 platform-graceful-restart / agent-session-resume /
platform-concurrency-scaling 三份 change 的正文做禁止词扫描：确保旧模型
词汇只出现在明确「已删除 / 已替代 / 禁止」的白名单语境里，不被研发误当
成现行真相源引用。

用法：
    python3 spec_consistency_probe.py            # 扫描默认三份 change
    python3 spec_consistency_probe.py <root> ... # 指定文件/目录

退出码：0 = 干净；1 = 发现违规（非白名单语境命中禁止词）。
"""
import re
import sys
from pathlib import Path

# 每条规则：禁止的旧模型提法（正则）+ 人类可读原因。
# 命中行若含任一白名单标记词，视为「解释旧模型为何被删除」的合法语境，放行。
FORBIDDEN = [
    (r"(retryable|普通\s*retry|瞬时\s*retry)[^\n]{0,40}(走|转|建|→|->|直接)[^\n]{0,12}recovery child",
     "普通 retry 必须走同 execution 新 attempt，不得走 recovery child（recovery child 仅供 supersede/交棒）"),
    (r"retry[^\n]{0,20}回队[^\n]{0,20}owner[^\n]{0,6}不\s*retire",
     "retry 回队后 owner 必须退休（epoch+1）"),
    (r"无\s*session[^\n]{0,20}落\s*`?failed`?",
     "无 session 的 run 不得落 failed，应走 full_replay recovery child"),
    (r"NULL\s*conversation[^\n]{0,30}(退化|复用)[^\n]{0,10}task[^\n]{0,6}session",
     "NULL conversation run 不建/复用 agent_sessions，固定 full replay"),
    (r"(无\s*safe\s*ingestion|做不到\s*context-only)[^\n]{0,40}摘要[^\n]{0,20}(自动完成|正常业务\s*turn)",
     "无 safe ingestion 时应转人工，摘要不得冒充自动完成消费"),
    (r"winning_attempt_id",
     "attempt 指针已改名 final_attempt_id（final 非 winning）"),
    (r"获胜\s*attempt",
     "术语已统一为「定局 attempt」"),
    (r"task_runs\.lease_expires_at",
     "task_runs 无 lease 字段；lease = run_queue.claim_lease_until / worker_state.lease_expires_at"),
    (r"prestart_failed",
     "prestart_failed 已归一为 failed + failure_stage=prestart"),
    (r"committed_batch_end",
     "水位已改名 batch_scan_end"),
    (r"jian[^\n]{0,30}只\s*(按|校验|看)\s*generation",
     "jian 平台写须 attempt 级 fencing（generation+instance+attempt/execution/current pointer）"),
    (r"start_new_session[^\n]{0,30}(冒充|等价|替代)[^\n]{0,20}(suspended|launch\s*gate|启动闸门)",
     "start_new_session 不得冒充 CAS 前启动闸门"),
]

# 白名单标记词：命中行含这些词，说明是在说明「旧模型已被删除/禁止」，放行。
ALLOW_MARKERS = [
    "SHALL NOT", "不得", "禁止", "已删除", "已取消", "已替代", "替代",
    "不再", "非旧口径", "旧口径", "而非", "修正", "收紧", "归一",
    "改名", "已改", "改为", "不是", "不复用", "不建", "冒充",
    "两义并存", "自相矛盾", "旧模型", "旧的", "旧句",
    "不含", "契约违规", "视为违规", "无 lease", "无此字段",
]

DEFAULT_CHANGES = [
    "platform-graceful-restart",
    "agent-session-resume",
    "platform-concurrency-scaling",
]


def resolve_targets(argv):
    """把命令行参数解析成待扫描的 .md 文件列表。无参数时扫描默认三份 change。"""
    if argv:
        roots = [Path(a) for a in argv]
    else:
        # 本脚本位于 <repo>/openspec/changes/platform-graceful-restart/scripts/
        changes_dir = Path(__file__).resolve().parents[2]
        roots = [changes_dir / name for name in DEFAULT_CHANGES]

    files = []
    for root in roots:
        if not root.exists():
            print(f"⚠️  跳过不存在的路径: {root}", file=sys.stderr)
            continue
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(root.rglob("*.md")))
    return files


def scan_line(line):
    """返回该行命中的 (pattern, reason) 违规列表；白名单语境返回空。"""
    if any(marker in line for marker in ALLOW_MARKERS):
        return []
    hits = []
    for pattern, reason in FORBIDDEN:
        if re.search(pattern, line):
            hits.append((pattern, reason))
    return hits


def main(argv):
    # Windows 控制台默认 GBK，正文含 emoji/中文，统一切到 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    files = resolve_targets(argv)
    if not files:
        print("❌ 没有可扫描的文件", file=sys.stderr)
        return 1

    violations = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"⚠️  读取失败 {f}: {exc}", file=sys.stderr)
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, reason in scan_line(line):
                violations.append((f, lineno, reason, line.strip()))

    print("📊 spec_consistency_probe")
    print(f"   扫描文件数: {len(files)}")
    print(f"   违规命中数: {len(violations)}")

    if not violations:
        print("✅ 未发现非白名单语境的旧模型提法")
        return 0

    print("\n❌ 发现旧模型提法（非「已删除/禁止」白名单语境）:")
    for f, lineno, reason, snippet in violations:
        print(f"   {f}:{lineno}")
        print(f"     原因: {reason}")
        print(f"     行内: {snippet[:120]}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
