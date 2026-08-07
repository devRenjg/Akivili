"""Codex CLI 执行后端：codex exec --json。

本平台是本地可信工作台，Codex CLI 默认全权限执行，避免协同队列卡在审批/沙箱。
Windows 下 codex 沙箱会因 CreateProcessWithLogonW 失败而无法写文件，因此默认使用
--dangerously-bypass-approvals-and-sandbox。

codex 的 JSONL 事件 schema 与 claude 不同，做宽松解析：提取 msg/message/text/delta
等常见字段里的文本。子进程阻塞读放到线程，asyncio.Queue 桥接；cwd=项目目录。
"""
import asyncio
import json
import shutil
import subprocess
import threading

from .base import ExecutorBackend, ExecContext, ExecEvent, build_cli_prompt, _StderrDrainer


class CodexBackend(ExecutorBackend):
    async def run(self, ctx: ExecContext, on_pid=None, on_session=None):
        # on_session：S3 会在此从 thread.started 事件抓 thread_id 后回传；本步先接受参数不使用。
        exe = shutil.which("codex") or "codex"
        # codex exec 把 prompt 首段当作主任务，若把长系统提示放前面它会去复述人格而非执行。
        # 因此：本轮指令放最前（codex 优先执行），角色设定/历史作为背景附在后面。
        parts = [f"# 你的任务（请直接执行）\n{ctx.prompt}"]
        if ctx.history:
            hist = build_cli_prompt(ctx)  # 含历史 + 指令
            parts.append(f"# 对话背景\n{hist}")
        if ctx.system_prompt:
            parts.append(f"# 你的角色设定（背景参考）\n{ctx.system_prompt}")
        prompt = "\n\n---\n\n".join(parts)

        # agent-session-resume-minimal S3：resume 命中（ctx.cli_session_id 非空）→ 走 `exec resume`
        #   子命令续接上次 thread。实测（Papers/CLI-resume能力实测 2.3）：resume 子命令**不接受 --cd**
        #   （它从 session 自身恢复 cwd），OPTIONS 须放 <SESSION_ID> 之前。stream schema 与首轮一致，
        #   _parse_line 原样复用。首建（cli_session_id 空）→ 现状 `exec --json`，thread_id 由 CLI 自生、
        #   从 thread.started 事件抓取回传。
        resume = bool(ctx.cli_session_id)
        if resume:
            cmd = [
                exe, "exec", "resume",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
            ]
            if ctx.model:
                cmd += ["-m", ctx.model]
            cmd += [ctx.cli_session_id, "-"]   # <SESSION_ID> [PROMPT(-=stdin)]
        else:
            cmd = [
                exe, "exec", "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--cd", ctx.project_dir,
                "--add-dir", ctx.project_dir,
            ]
            if ctx.model:
                cmd += ["-m", ctx.model]
            cmd += ["-"]   # prompt 从 stdin 读入（避免命令行参数过长/被截断/转义问题）

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        # 子进程环境：确保内网网关不走代理，避免连不通。
        # 需绕代理的内网域名通过环境变量 AKIVILI_NO_PROXY_EXTRA 追加（逗号分隔）。
        import os as _os
        child_env = dict(_os.environ)
        _no = child_env.get("NO_PROXY", "")
        _extra_no = _os.environ.get("AKIVILI_NO_PROXY_EXTRA", "").strip()
        _no_parts = [p for p in (_no, _extra_no, "localhost", "127.0.0.1") if p]
        child_env["NO_PROXY"] = ",".join(_no_parts)
        child_env["no_proxy"] = child_env["NO_PROXY"]
        # 注入 jian CLI 身份 + cli 目录加进 PATH
        child_env.update(ctx.env_extra or {})
        _cli_dir = str(__import__("pathlib").Path(__file__).parent.parent / "cli")
        child_env["PATH"] = _cli_dir + _os.pathsep + child_env.get("PATH", "")

        def _reader():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=ctx.project_dir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=child_env,
                )
                # prompt 从 stdin 写入后关闭，触发 codex 开始执行
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            except OSError as e:
                loop.call_soon_threadsafe(queue.put_nowait, ExecEvent("error", f"无法启动 codex: {e}"))
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            if on_pid:
                loop.call_soon_threadsafe(on_pid, proc.pid)
            # 组 2 containment：把 CLI 子进程加入 worker 的 Job Object，worker 死则 OS 连带清理。
            # Job 未就绪（直连路径 / 初始化失败）时 contain 返回 False、静默降级，该进程仍由
            # kill_run 的 taskkill /T 兜底，功能不回退。
            try:
                from executor.containment import contain  # noqa: PLC0415
                contain(proc.pid)
            except Exception:  # noqa: BLE001 — containment 是增强，绝不阻断执行
                pass
            # stderr 并发抽干（防双管道死锁）：codex --json 会把大量日志打到 stderr，
            # 若不并发读、等 stdout 读完才读 stderr，stderr 缓冲写满会把子进程憋死在写 stderr、
            # 不再吐 stdout → 主线程读 stdout 死等到超时被误杀（run#243 事故根因）。
            stderr_drainer = _StderrDrainer(proc.stderr)
            _sid_seen = False   # thread_id 只回传一次（首个 thread.started）
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                # S3：从 thread.started 事件抓 codex 原生会话 id（thread_id），经 on_session 回传给
                # runner 写库（task_runs + run_queue，供下次 attempt --resume）。回调须在主 loop 线程调，
                # 用 call_soon_threadsafe（同 on_pid）。resume 时也会再吐 thread.started（同一 id，不 fork）。
                if on_session and not _sid_seen:
                    tid = _extract_thread_id(line)
                    if tid:
                        _sid_seen = True
                        loop.call_soon_threadsafe(on_session, tid)
                ev = _parse_line(line)
                if ev:
                    loop.call_soon_threadsafe(queue.put_nowait, ev)
            proc.wait()
            err = stderr_drainer.result()
            if proc.returncode != 0:
                loop.call_soon_threadsafe(queue.put_nowait,
                                          ExecEvent("error", (err or "").strip()[:500] or f"退出码 {proc.returncode}"))
            loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_reader, daemon=True).start()

        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        yield ExecEvent("done")


def codex_rollout_present(thread_id: str) -> bool:
    """S3.4：resume 前校验 codex thread 的 rollout 文件在位（对标 Multica
    gateCodexResumeToRolloutPresence）。rollout 存于 ~/.codex/sessions/YYYY/MM/DD/
    rollout-<ts>-<thread_id>.jsonl，文件名尾部即 thread_id。不在位说明 CLI 无法续接该 thread，
    应降级全量而非假装续上（否则 codex 会静默从头开始、丢上下文）。
    CODEX_HOME 环境变量可覆盖默认 ~/.codex。"""
    import os as _os
    import glob as _glob
    if not thread_id:
        return False
    home = _os.environ.get("CODEX_HOME") or _os.path.join(_os.path.expanduser("~"), ".codex")
    sessions_dir = _os.path.join(home, "sessions")
    if not _os.path.isdir(sessions_dir):
        return False
    # 文件名含 thread_id 即在位；递归匹配（按年月日分层）
    hits = _glob.glob(_os.path.join(sessions_dir, "**", f"*{thread_id}*.jsonl"), recursive=True)
    return bool(hits)


def _extract_thread_id(line: str) -> str | None:
    """S3：从 codex 事件行提取 thread_id（会话 id）。首轮 `thread.started` 事件带 thread_id
    （实测 Papers/CLI-resume能力实测 2.4）。非该事件/非 JSON → None。"""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("type") == "thread.started":
        tid = obj.get("thread_id")
        return tid if isinstance(tid, str) and tid else None
    return None


def _parse_line(line: str) -> ExecEvent | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # 非 JSON 行（如 "Reading additional input from stdin..."）当作系统提示
        return ExecEvent("system", line) if line else None
    t = obj.get("type", "")
    # codex 真实 schema：item.completed / item.updated 里带 item.type/text/message
    if t.startswith("item") and isinstance(obj.get("item"), dict):
        item = obj["item"]
        itype = item.get("type", "")
        if itype == "error":
            return ExecEvent("error", item.get("message", "")[:300])
        # 命令执行：保留完整命令 + 输出（供日志详情还原运行时详情）
        if itype in ("command_execution", "tool_call"):
            cmd = item.get("command") or item.get("text") or item.get("message") or ""
            cmd = cmd.strip() if isinstance(cmd, str) else ""
            out = item.get("output") or item.get("aggregated_output") or item.get("result") or ""
            out = out.strip() if isinstance(out, str) else ""
            name = "Bash" if itype == "command_execution" else (item.get("name") or "Tool")
            summary = f"{name}: {cmd[:120]}" + ("…" if len(cmd) > 120 else "") if cmd else f"调用工具：{name}"
            return ExecEvent("tool", summary, tool=name,
                             tool_input={"command": cmd} if cmd else {}, tool_output=out)
        # 文件改动：提取路径 + 完整 diff，作为工具活动
        if itype == "file_change":
            changes = item.get("changes", []) or []
            paths = [c.get("path", "") for c in changes if c.get("path")]
            if paths:
                return ExecEvent("tool", "文件改动：" + ", ".join(paths),
                                 tool="Edit", tool_input={"changes": changes})
            return None
        text = item.get("text") or item.get("message") or ""
        if isinstance(text, str) and text.strip():
            return ExecEvent("text", text.strip())
        return None
    if t == "error" or t == "turn.failed":
        msg = obj.get("message") or obj.get("error", {}).get("message", "")
        return ExecEvent("error", str(msg)[:300]) if msg else None
    # 真实 token 用量：codex turn.completed 事件带 usage.{input_tokens/cached_input_tokens/
    # output_tokens}（实测确认）。提取成 usage 事件供 runner 落库（token-drop 对比/成本观测）。
    if t == "turn.completed":
        u = obj.get("usage")
        if isinstance(u, dict):
            return ExecEvent("usage", meta={
                "input_tokens": u.get("input_tokens") or 0,
                "cached_input_tokens": u.get("cached_input_tokens") or 0,
                "output_tokens": u.get("output_tokens") or 0,
            })
        return None
    # 兜底：宽松提取常见文本字段
    for key in ("text", "delta", "content", "message"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return ExecEvent("text", v.strip())
    return None
