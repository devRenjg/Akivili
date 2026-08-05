"""只读探针：验证 claude `-p --session-id <uuid>` 预分配 + `--resume <uuid>` 的行为。

目的（Session Resume 评估第二步）：
    codex 的 session id 由 CLI 生成、需从 thread.started 事件抓取；claude 则支持我们**预分配**
    `--session-id <uuid>` 传入。本探针验证 claude 侧三件事：
      1. `-p --output-format stream-json --session-id <uuid>` 首轮能否接受预分配 id（不报错）
      2. 同一 CLAUDE_CONFIG_DIR 下 `--resume <uuid>` 能否真正续上下文（复现 backend 的隔离 config dir）
      3. resume 后 stream-json 的 type schema 是否与首轮一致（backend/executor/claude_code.py::_parse_line
         能否原样解析）；并观察 resume 后 session_id 是否保持不变（不 fork）

关键复现点（claude 独有、codex 无）：
    backend/executor/claude_code.py:76 把子进程 CLAUDE_CONFIG_DIR 指向隔离目录（%TEMP%/akivili_claude_cfg）。
    会话记录（供 resume）就存在该目录，故两轮必须用**同一个** CLAUDE_CONFIG_DIR，否则 resume 必然 miss。
    本探针显式固定一个临时 config dir 跑两轮，检验「隔离目录 + resume」可行性。

判定标准（写死）：
    1. schema 一致 = run2 的 type 集合 ⊆ run1 的 type 集合
    2. resume 有效 = run2 文本里复述了首轮暗号
    3. session 稳定 = run2 的 session_id 仍等于预分配 UUID（不 fork）

纯只读：起 claude、dump stream-json、比对。不碰 backend、不写平台 DB。产物落 TestReport/claude_run{1,2}.jsonl。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

# 预分配 UUID（合法 v4 形态即可，claude 只校验 UUID 格式）
PREASSIGNED_UUID = "a1b2c3d4-0000-4000-8000-abcdef123456"
RUN1_SECRET = "青龙七号玄武"
RUN1_PROMPT = f"这是测试。请记住暗号：{RUN1_SECRET}。只回复：收到{RUN1_SECRET}。不要做其他操作。"
RUN2_PROMPT = "我刚才告诉你的暗号是什么？只回复暗号本身，不要做其他操作。"


def _run_claude(cmd, prompt, dump_path, cwd, config_dir):
    """跑一条 claude 命令，prompt 走 stdin，逐行 dump stream-json。返回 (行对象list, 全文, rc)。"""
    print(f"    $ {' '.join(cmd)}", flush=True)
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir  # 复现 backend 的隔离 config dir，两轮必须一致
    proc = subprocess.run(
        cmd, input=prompt, cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    objs, texts = [], []
    with io.open(dump_path, "w", encoding="utf-8") as f:
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            f.write(line + "\n")
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            objs.append(o)
            texts.extend(_collect_text(o))
    if proc.stderr.strip():
        with io.open(dump_path + ".stderr", "w", encoding="utf-8") as f:
            f.write(proc.stderr)
    return objs, "\n".join(texts), proc.returncode


def _collect_text(obj):
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_collect_text(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_collect_text(v))
    elif isinstance(obj, str):
        out.append(obj)
    return out


def _types(objs):
    """收集顶层 type 集合（claude schema 判定单看顶层 type，见 claude_code.py:166-201）。"""
    return {o.get("type", "") for o in objs if isinstance(o, dict)}


def _session_ids(objs):
    ids = []
    for o in objs:
        if isinstance(o, dict) and isinstance(o.get("session_id"), str):
            ids.append(o["session_id"])
    return list(dict.fromkeys(ids))


def main():
    claude = shutil.which("claude")
    if not claude:
        print("❌ shutil.which('claude') 找不到 claude 可执行文件。")
        return 2
    print(f"    claude 可执行: {claude}", flush=True)
    workdir = tempfile.mkdtemp(prefix="claude_resume_probe_work_")
    config_dir = tempfile.mkdtemp(prefix="claude_resume_probe_cfg_")
    report_dir = os.path.dirname(os.path.abspath(__file__))
    run1_dump = os.path.join(report_dir, "claude_run1.jsonl")
    run2_dump = os.path.join(report_dir, "claude_run2.jsonl")

    print("=" * 64)
    print("claude --session-id 预分配 + --resume 探针（只读，不碰 backend）")
    print(f"  workdir     : {workdir}")
    print(f"  config_dir  : {config_dir}  （两轮共用，复现 backend 隔离目录）")
    print(f"  预分配 UUID : {PREASSIGNED_UUID}")
    print("=" * 64)

    base_flags = [
        "-p", "--output-format", "stream-json", "--verbose",
        "--add-dir", workdir,
        "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions",
    ]

    # ---- A. 首轮：预分配 session-id ----
    print("\n[A] 首轮 claude -p --session-id <uuid> …")
    cmd1 = [claude] + base_flags + ["--session-id", PREASSIGNED_UUID]
    objs1, text1, rc1 = _run_claude(cmd1, RUN1_PROMPT, run1_dump, workdir, config_dir)
    print(f"    退出码={rc1}，解析出 {len(objs1)} 行 JSON")
    types1 = _types(objs1)
    sids1 = _session_ids(objs1)
    print(f"    首轮 type 集合: {sorted(types1)}")
    print(f"    首轮 session_id: {sids1}")

    if rc1 != 0 or not objs1:
        print(f"\n❌ 首轮未正常产出（rc={rc1}），见 {run1_dump}.stderr")
        return 2

    preassign_ok = PREASSIGNED_UUID in sids1
    print(f"    预分配是否生效（回显 UUID == 传入）: {'✅' if preassign_ok else '❌'}")

    # ---- B. 第二轮：resume 同一 uuid、同一 config dir ----
    print(f"\n[B] 第二轮 claude -p --resume {PREASSIGNED_UUID} …")
    cmd2 = [claude] + base_flags + ["--resume", PREASSIGNED_UUID]
    objs2, text2, rc2 = _run_claude(cmd2, RUN2_PROMPT, run2_dump, workdir, config_dir)
    print(f"    退出码={rc2}，解析出 {len(objs2)} 行 JSON")
    types2 = _types(objs2)
    sids2 = _session_ids(objs2)
    print(f"    第二轮 type 集合: {sorted(types2)}")
    print(f"    第二轮 session_id: {sids2}")

    # ---- C. 判定 ----
    print("\n" + "=" * 64)
    print("判定结果")
    print("=" * 64)

    run2_ran = rc2 == 0 and len(objs2) > 0
    new_types = types2 - types1
    schema_ok = run2_ran and not new_types
    if not run2_ran:
        print(f"\n[1] schema 一致: ❓ 无法评估 —— run2 未正常产出（rc={rc2}, 行数={len(objs2)}）")
    else:
        print(f"\n[1] schema 与首轮一致（_parse_line 可原样解析 run2）: {'✅ 是' if schema_ok else '❌ 否'}")
        if new_types:
            print(f"    run2 出现首轮没有的 type（真差异，需人工确认）: {sorted(new_types)}")

    resume_ok = RUN1_SECRET in text2
    print(f"\n[2] resume 真正延续上下文（run2 复述暗号 {RUN1_SECRET!r}）: {'✅ 是' if resume_ok else '❌ 否'}")
    if not resume_ok:
        print(f"    run2 文本片段：{text2[:300].strip()}")

    session_stable = run2_ran and sids2 == [PREASSIGNED_UUID]
    print(f"\n[3] session_id resume 后保持不变（不 fork）: {'✅ 是' if session_stable else '❌ 否，见 run2 session_id'}")

    print(f"\n[4] 预分配 session-id 生效: {'✅ 是' if preassign_ok else '❌ 否'}")

    print(f"\n原始输出已 dump：\n  {run1_dump}\n  {run2_dump}")
    print("\n" + "=" * 64)
    if schema_ok and resume_ok and session_stable and preassign_ok:
        print("结论：claude resume 走【最优路径】—— 预分配 UUID（省抓取）+ --resume 原样复用 _parse_line，")
        print("      session_id 稳定不 fork（无需覆盖逻辑）。唯一前置：两轮同一 CLAUDE_CONFIG_DIR。")
    elif resume_ok:
        print("结论：resume 可用，但某项判定未过（见上方 ❌）—— 细看差异，多半仍无需重集成。")
    else:
        print("结论：需进一步排查（见上方 ❌ 项）。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    _report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_resume_probe_report.txt")
    _f = io.open(_report, "w", encoding="utf-8")
    _orig = sys.stdout
    sys.stdout = _f
    try:
        rc = main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=_f)
        rc = 3
    finally:
        sys.stdout = _orig
        _f.close()
    print(f"report -> {_report} (rc={rc})")
    sys.exit(rc)
