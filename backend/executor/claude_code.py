"""Claude Code 执行后端：claude -p --output-format stream-json。

stream-json 每行一个 JSON 事件。关心：
- type=assistant: message.content[].text  → 文本增量
- type=result:    result/subtype           → 结束
子进程阻塞读取放到线程，用 asyncio.Queue 桥接回 async 生成器；不 shell 拼接。
权限全放开（效率优先，仅可信内网）：--permission-mode bypassPermissions +
--dangerously-skip-permissions 双保险，--add-dir 指向项目目录。
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading

from .base import ExecutorBackend, ExecContext, ExecEvent, build_cli_prompt


class ClaudeCodeBackend(ExecutorBackend):
    async def run(self, ctx: ExecContext, on_pid=None):
        exe = shutil.which("claude") or "claude"
        cli_prompt = build_cli_prompt(ctx)
        cmd = [
            exe, "-p",                       # prompt 走 stdin（不作为命令行参数）
            "--output-format", "stream-json",
            "--verbose",
            "--add-dir", ctx.project_dir,
            "--permission-mode", "bypassPermissions",
            "--dangerously-skip-permissions",
            # 禁用内置编排/待办/子代理/工作流工具：否则 Leader 会用它们"模拟"派活/建卡而绕过 jian，
            # 导致成员并未被平台真正唤醒。协同必须走 jian（真实入队/建卡/落库）。
            # 名称对齐 claude 2.1.x 内置工具集：Task=子代理派生、Workflow=多代理编排、
            # TaskCreate/Update/List=待办系统、SendMessage=代理间消息。
            "--disallowed-tools",
            "Task,Workflow,SendMessage,TaskCreate,TaskUpdate,TaskGet,TaskList,TaskOutput,TaskStop,TodoWrite",
        ]
        if ctx.system_prompt:
            # 用 --system-prompt 替换 Claude 自带的"编码助手"默认身份（而非 append 追加在其后），
            # 让 Akivili 的人格+团队+协议成为最高权重身份——否则默认的"先探索代码库"本能会压过我们的指令。
            # 因替换掉了默认提示，补一句工具可用性说明，保证 Bash（跑 jian）/Read/Write 仍被正常使用。
            tool_hint = (
                "你在命令行环境中工作，可使用 Bash、Read、Write、Edit 等工具在当前目录读写文件、"
                "执行命令（包括 `jian` 平台命令）。收到任务后直接用工具动手完成。\n\n"
            )
            # 系统提示经**文件**传入（--system-prompt-file），不作命令行参数——
            # 否则超长系统提示会触发 Windows「命令行太长」而 claude 秒失败。
            _sp_fd, _sp_path = tempfile.mkstemp(prefix="akivili_sysprompt_", suffix=".txt")
            with os.fdopen(_sp_fd, "w", encoding="utf-8") as _f:
                _f.write(tool_hint + ctx.system_prompt)
            cmd += ["--system-prompt-file", _sp_path]
        else:
            _sp_path = None
        if ctx.model:
            cmd += ["--model", ctx.model]

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        # 子进程环境：注入 jian CLI 身份 + 把 cli 目录加进 PATH（Agent 可直接敲 jian）
        _os = os
        child_env = dict(_os.environ)
        child_env.update(ctx.env_extra or {})
        _cli_dir = str(__import__("pathlib").Path(__file__).parent.parent / "cli")
        child_env["PATH"] = _cli_dir + _os.pathsep + child_env.get("PATH", "")
        # 隔离宿主全局定制：把 CLAUDE_CONFIG_DIR 指向一个空目录，
        # 使子进程不加载 ~/.claude/CLAUDE.md、hooks、skills、自定义 agent（避免 BiliSC 等污染人格）。
        # 认证不在该目录（走 keychain/OAuth），故不受影响；且 stream-json 输出保持正常
        # （--safe-mode 会把 stream-json 降级成纯文本、导致解析不到事件，故不用它）。
        _iso_cfg = __import__("pathlib").Path(_os.environ.get("TEMP", "/tmp")) / "akivili_claude_cfg"
        try:
            _iso_cfg.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        child_env["CLAUDE_CONFIG_DIR"] = str(_iso_cfg)

        def _reader():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=ctx.project_dir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=child_env,
                )
            except OSError as e:
                loop.call_soon_threadsafe(queue.put_nowait,
                                          ExecEvent("error", f"无法启动 claude: {e}"))
                if _sp_path:
                    try:
                        os.unlink(_sp_path)
                    except OSError:
                        pass
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            if on_pid:
                loop.call_soon_threadsafe(on_pid, proc.pid)
            # prompt 通过 stdin 传入（避免超长 prompt 作为 Windows 命令行参数被截断）
            try:
                proc.stdin.write(cli_prompt)
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            # 本次 run 内维护 tool_use_id → 工具名 的映射：
            # Claude 的 tool_result 块只带 tool_use_id、不带工具名，靠它回填出「Bash」等标签。
            tool_names: dict[str, str] = {}
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                for ev in _parse_line(line, tool_names):
                    loop.call_soon_threadsafe(queue.put_nowait, ev)
            err = proc.stderr.read()
            proc.wait()
            # 清理临时系统提示文件
            if _sp_path:
                try:
                    os.unlink(_sp_path)
                except OSError:
                    pass
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


def _parse_line(line: str, tool_names: dict | None = None) -> list[ExecEvent]:
    """解析一行 stream-json，返回 0..N 个 ExecEvent（一条消息可含文本+多次工具调用）。

    - assistant 消息：content[] 里 text→text 事件、thinking→thinking 事件、
      tool_use→tool 事件（保留工具名 + 完整 input，含 Bash 的实际命令）。
    - user 消息：content[] 里 tool_result→tool_result 事件（保留工具执行输出）。
    - result：结束标记。

    tool_names: 跨行维护的 tool_use_id → 工具名 映射。tool_use 时登记，
    tool_result 时按其 tool_use_id 回填工具名（Claude 的 tool_result 只带 id、
    不带名字），使结果行也能显示「Bash」而非笼统的「结果」。
    """
    if tool_names is None:
        tool_names = {}
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    t = obj.get("type")
    events: list[ExecEvent] = []
    if t == "assistant":
        parts = obj.get("message", {}).get("content", [])
        for p in parts:
            ptype = p.get("type")
            if ptype == "text":
                txt = (p.get("text") or "").strip()
                if txt:
                    events.append(ExecEvent("text", txt))
            elif ptype == "thinking":
                think = (p.get("thinking") or p.get("text") or "").strip()
                if think:
                    events.append(ExecEvent("thinking", think))
            elif ptype == "tool_use":
                name = p.get("name", "") or "Tool"
                inp = p.get("input") if isinstance(p.get("input"), dict) else {}
                tid = p.get("id")
                if tid:
                    tool_names[tid] = name   # 登记 id→名，供后续 tool_result 回填
                events.append(ExecEvent(
                    "tool", _tool_summary(name, inp), tool=name, tool_input=inp))
    elif t == "user":
        # 工具执行结果回灌在 user 消息里（tool_result 块）
        parts = obj.get("message", {}).get("content", [])
        if isinstance(parts, list):
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    out = _tool_result_text(p.get("content"))
                    if out:
                        # 按 tool_use_id 回填工具名（拿不到则留空，前端回退「结果」）
                        name = tool_names.get(p.get("tool_use_id"), "")
                        events.append(ExecEvent("tool_result", "", tool=name, tool_output=out))
    elif t == "result":
        events.append(ExecEvent("system", "执行完成"))
    return events


def _tool_summary(name: str, inp: dict) -> str:
    """给工具调用生成一行摘要（列表精简展示用；完整信息在 tool_input 里）。"""
    for k in ("command", "file_path", "path", "pattern", "query", "prompt", "description", "url"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip().replace("\n", " ")
            return f"{name}: {s[:120]}" + ("…" if len(s) > 120 else "")
    return f"调用工具：{name}"


def _tool_result_text(content) -> str:
    """tool_result 的 content 可能是字符串或 [{type:text,text:..}] 列表，统一成文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(t for t in texts if t).strip()
    return ""
