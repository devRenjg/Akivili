"""只读探针：验证 `codex exec resume --json` 的流式事件格式是否与 `codex exec --json` 一致。

目的（Session Resume 评估第一步，消掉唯一未知数）：
    我们现有 backend/executor/codex.py::_parse_line 解析的是 `codex exec --json` 的 item.* schema。
    若 `codex exec resume <id> --json` 吐出同一套 schema，则 codex resume 线可走「命令前缀加 resume <id>」
    的轻路径，_parse_line 原样复用；否则才需回落到 app-server + JSON-RPC 重集成。

做法（纯只读，不碰 backend、不写 DB）：
    A. codex exec --json 跑第一轮 → dump run1.jsonl，收集 (顶层type, item.type) 组合 + 扒 session id
    B. codex exec resume <session_id> --json 跑第二轮 → dump run2.jsonl，同样收集
    C. 判定 schema 一致性 / resume 上下文延续 / session id 字段位置

判定标准（写死，不留模糊）：
    1. schema 一致 = run2 的 (顶层type,item.type) 组合 ⊆ run1 ∪ _parse_line 已知集
    2. resume 有效 = run2 文本事件里正确复述了第一轮的暗号
    3. session 字段 = 明确报出 id 在哪个 JSON 字段
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

# _parse_line 已知能处理的顶层 type / item.type（对比基准，见 codex.py:117-160）
KNOWN_TOP_TYPES = {"error", "turn.failed"}  # 加上任何以 "item" 开头的
KNOWN_ITEM_TYPES = {"error", "command_execution", "tool_call", "file_change"}  # 其余 item.type 走文本兜底

RUN1_SECRET = "紫水晶三七二一"   # 第一轮暗号：run2 若真 resume 应能复述
RUN1_PROMPT = f"这是一个测试。请只回复：第一轮OK，暗号是{RUN1_SECRET}。不要做任何其他操作。"
RUN2_PROMPT = "请只回复：第二轮OK，并原样复述我第一轮告诉你的暗号是什么。不要做任何其他操作。"


def _find_session_id(obj, path=""):
    """递归扒出任何形如 session/thread/conversation id 的字段，返回 [(字段路径, 值)]。"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else k
            kl = k.lower()
            if isinstance(v, str) and v and (
                kl in ("session_id", "thread_id", "conversation_id", "threadid", "sessionid")
                or (kl == "id" and path.lower() in ("thread", "session", "conversation"))
            ):
                hits.append((kp, v))
            hits.extend(_find_session_id(v, kp))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(_find_session_id(v, f"{path}[{i}]"))
    return hits


def _run_codex(cmd, prompt, dump_path, cwd):
    """跑一条 codex 命令，prompt 走 stdin，逐行 dump JSONL。返回 (所有解析出的行对象, 全文文本拼接)。"""
    print(f"    $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(
        cmd, input=prompt, cwd=cwd,
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
            # 收集所有文本供 resume 复述判定
            for v in _collect_text(o):
                texts.append(v)
    if proc.stderr.strip():
        with io.open(dump_path + ".stderr", "w", encoding="utf-8") as f:
            f.write(proc.stderr)
    return objs, "\n".join(texts), proc.returncode


def _collect_text(obj):
    """粗提所有字符串文本值（用于暗号复述判定，不判 schema）。"""
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


def _schema_combos(objs):
    """收集所有 (顶层type, item.type) 组合。"""
    combos = set()
    for o in objs:
        t = o.get("type", "") if isinstance(o, dict) else ""
        it = ""
        if isinstance(o, dict) and isinstance(o.get("item"), dict):
            it = o["item"].get("type", "")
        combos.add((t, it))
    return combos


def _combo_known(top, item):
    """该组合是否落在 _parse_line 已知可解析集内。"""
    if top.startswith("item"):
        return True  # item.* 一律进 item 分支；item.type 未知时走文本兜底，不报错
    if top in KNOWN_TOP_TYPES:
        return True
    return False  # 走最后的宽松文本兜底，可能丢事件——标为「需人工看」


def main():
    # 与 backend/executor/codex.py:21 一致，用 shutil.which 解析（Windows 会命中 codex.cmd）
    codex = shutil.which("codex")
    if not codex:
        print("❌ shutil.which('codex') 找不到 codex 可执行文件，请确认 PATH。")
        return 2
    print(f"    codex 可执行: {codex}", flush=True)
    workdir = tempfile.mkdtemp(prefix="codex_resume_probe_")
    report_dir = os.path.dirname(os.path.abspath(__file__))
    run1_dump = os.path.join(report_dir, "codex_run1.jsonl")
    run2_dump = os.path.join(report_dir, "codex_run2.jsonl")

    print("=" * 64)
    print("codex exec resume 格式探针（只读，不碰 backend）")
    print(f"  临时 workdir: {workdir}")
    print("=" * 64)

    # ---- A. 第一轮：codex exec --json ----
    print("\n[A] 第一轮 codex exec --json …")
    cmd1 = [codex, "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check", "--cd", workdir, "-"]
    objs1, text1, rc1 = _run_codex(cmd1, RUN1_PROMPT, run1_dump, workdir)
    print(f"    退出码={rc1}，解析出 {len(objs1)} 行 JSON")

    combos1 = _schema_combos(objs1)
    sids = []
    for o in objs1:
        sids.extend(_find_session_id(o))
    sids = list(dict.fromkeys(sids))  # 去重保序

    print("\n[B] 第一轮观察：")
    print("    顶层 type / item.type 组合：")
    for top, item in sorted(combos1):
        print(f"      - top={top!r:24} item.type={item!r}")
    print("    session/thread id 候选字段：")
    if sids:
        for path, val in sids:
            print(f"      - {path} = {val}")
    else:
        print("      （未在 JSON 字段里找到 session/thread id —— 见下方兜底扫描）")

    if not sids:
        print("\n    ⚠️ 结构化字段没找到 id，尝试全文正则扫 UUID …")
        import re
        uuid_re = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
        found = set()
        for o in objs1:
            for s in _collect_text(o):
                found.update(uuid_re.findall(s))
        for u in found:
            print(f"      - 全文 UUID: {u}")
        sids = [("<full-text-uuid>", u) for u in found]

    if not sids:
        print("\n❌ 拿不到 session id，无法继续 resume。请看 codex_run1.jsonl 原始输出。")
        return 2

    session_id = sids[0][1]
    print(f"\n    → 采用 session_id = {session_id}（字段 {sids[0][0]}）")

    # ---- C. 第二轮：codex exec resume <id> --json ----
    print(f"\n[C] 第二轮 codex exec resume {session_id} --json …")
    # 注意：`exec resume` 不接受 --cd（它从 session 自身恢复 cwd）。OPTIONS 放 SESSION_ID 之前。
    cmd2 = [codex, "exec", "resume",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            session_id, "-"]
    objs2, text2, rc2 = _run_codex(cmd2, RUN2_PROMPT, run2_dump, workdir)
    print(f"    退出码={rc2}，解析出 {len(objs2)} 行 JSON")

    combos2 = _schema_combos(objs2)
    # 「新差异」判据：run2 的组合既不在 run1 基准里、也不在 _parse_line 已知集里。
    # run1 是线上 _parse_line 已验证能正常处理的基准——run1 也出现的组合，无论是否走文本兜底，
    # 都是 codex exec 既有行为（如 thread.started/turn.* 这类无文本负载的骨架事件，_parse_line
    # 返回 None 是设计内的正常丢弃，非解析失败），不算 resume 引入的新差异。
    def _is_new(top, item):
        return (top, item) not in combos1 and not _combo_known(top, item)
    print("    第二轮 顶层 type / item.type 组合：")
    for top, item in sorted(combos2):
        if _is_new(top, item):
            mark = "  ← ⚠️ 新差异（run1 无、且非 _parse_line 已知）"
        elif (top, item) in combos1:
            mark = "  （run1 基准已有）"
        else:
            mark = ""
        print(f"      - top={top!r:24} item.type={item!r}{mark}")

    # ---- D. 判定 ----
    print("\n" + "=" * 64)
    print("判定结果")
    print("=" * 64)

    # 守卫：run2 必须真跑起来（rc==0 且有 JSON 行），否则 schema「⊆空集」是假阳性
    run2_ran = rc2 == 0 and len(objs2) > 0
    # 只有 run1 基准和 _parse_line 已知集都不覆盖的组合，才是 resume 引入的真差异
    unknown = [(t, i) for (t, i) in combos2 if _is_new(t, i)]
    schema_ok = run2_ran and len(unknown) == 0
    if not run2_ran:
        print(f"\n[1] schema 一致: ❓ 无法评估 —— run2 未正常产出（rc={rc2}, 行数={len(objs2)}）")
        print("    见 codex_run2.jsonl.stderr")
    else:
        print(f"\n[1] schema 与首轮一致（_parse_line 可原样解析 run2）: {'✅ 是' if schema_ok else '❌ 否'}")
    if unknown:
        print("    以下组合 run1 无、且非 _parse_line 已知 —— resume 引入的真差异，需人工确认：")
        for t, i in unknown:
            print(f"      - top={t!r} item.type={i!r}")

    resume_ok = RUN1_SECRET in text2
    print(f"\n[2] resume 真正延续上下文（run2 复述了第一轮暗号 {RUN1_SECRET!r}）: {'✅ 是' if resume_ok else '❌ 否'}")
    if not resume_ok:
        snippet = text2[:300].replace("\n", " ")
        print(f"    run2 文本片段：{snippet}")

    print(f"\n[3] session id 字段位置: {sids[0][0]}  （codex 侧「抓 session_id」从这里取）")

    print(f"\n原始输出已 dump：\n  {run1_dump}\n  {run2_dump}")
    print("\n" + "=" * 64)
    if not run2_ran:
        print("结论：run2 未跑起来，命令行本身有误 —— 见 stderr，修正后重跑。")
    elif schema_ok and resume_ok:
        print("结论：codex resume 走【轻路径】—— 现有 _parse_line 原样复用，命令前缀加 resume <id> 即可。")
    elif resume_ok and not schema_ok:
        print("结论：resume 可用但 schema 有差异 —— 需微调 _parse_line，仍无需 app-server。")
    else:
        print("结论：需进一步排查（见上方 ❌ 项），可能回落 app-server 方案。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    # 终端是 GBK，中文 print 会乱码 —— 把全部输出写到 UTF-8 报告文件，再用 Read 工具看
    _report = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_resume_probe_report.txt")
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
