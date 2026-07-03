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

from .base import ExecutorBackend, ExecContext, ExecEvent, build_cli_prompt


class CodexBackend(ExecutorBackend):
    async def run(self, ctx: ExecContext, on_pid=None):
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
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                ev = _parse_line(line)
                if ev:
                    loop.call_soon_threadsafe(queue.put_nowait, ev)
            err = proc.stderr.read()
            proc.wait()
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
        # 文件改动：提取路径显示为工具活动，让用户看到改了什么
        if itype == "file_change":
            paths = [c.get("path", "") for c in item.get("changes", []) if c.get("path")]
            if paths:
                return ExecEvent("tool", "文件改动：" + ", ".join(paths))
            return None
        text = item.get("text") or item.get("message") or ""
        if isinstance(text, str) and text.strip():
            kind = "tool" if itype in ("command_execution", "tool_call") else "text"
            return ExecEvent(kind, text.strip())
        return None
    if t == "error" or t == "turn.failed":
        msg = obj.get("message") or obj.get("error", {}).get("message", "")
        return ExecEvent("error", str(msg)[:300]) if msg else None
    # 兜底：宽松提取常见文本字段
    for key in ("text", "delta", "content", "message"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return ExecEvent("text", v.strip())
    return None
