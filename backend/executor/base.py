"""执行引擎抽象：统一的流式事件结构 + 后端接口。

每个后端把底层（claude -p / codex exec / API）的输出，标准化成 ExecEvent 序列：
- text:  助手文本增量
- thinking: 模型思考过程（可选，供日志详情展示）
- tool:  工具/命令调用（tool=工具名，tool_input=完整参数，如 Bash 的 command）
- tool_result: 工具执行结果（tool=工具名，tool_output=完整输出）
- system:系统提示（启动、结束原因等）
- error: 错误
- done:  结束（带最终文本）

tool/tool_input/tool_output 用于「日志详情」还原每条命令与运行时详情；
text/system/error 走 text 字段。所有字段可选，按 type 取用。
"""
from dataclasses import dataclass, field
from typing import Literal

EventType = Literal["text", "thinking", "tool", "tool_result", "system", "error", "done"]


@dataclass
class ExecEvent:
    type: EventType
    text: str = ""
    meta: dict = field(default_factory=dict)
    tool: str = ""                 # 工具名（Bash/Read/Write…），tool / tool_result 用
    tool_input: dict = field(default_factory=dict)   # 工具完整入参（含实际命令）
    tool_output: str = ""          # 工具完整输出（tool_result 用）


@dataclass
class ExecContext:
    """一次执行所需的全部输入。"""
    prompt: str               # 用户本轮指令
    system_prompt: str        # 组装好的系统提示（人格+记忆+Skills+任务上下文）
    project_dir: str          # 项目本地路径（工作目录 / --add-dir）
    model: str = ""           # 模型别名
    api_key: str = ""         # API 类型用
    base_url: str = ""        # API 类型用
    api_format: str = "openai"
    history: list = field(default_factory=list)  # [{role, content}] 会话历史回灌
    env_extra: dict = field(default_factory=dict)  # 注入子进程的额外环境变量（jian CLI 身份等）


class ExecutorBackend:
    """执行后端接口。run() 是异步生成器，逐个 yield ExecEvent。

    on_pid: 可选回调，子进程启动后回传 PID（用于 kill）。
    """
    async def run(self, ctx: ExecContext, on_pid=None):
        raise NotImplementedError
        yield  # pragma: no cover


class _StderrDrainer:
    """在独立线程里持续抽干 stderr 管道，防双管道死锁（run#243 事故根因）。

    CLI 子进程同时往 stdout + stderr 写。若主读取线程只读 stdout、等 stdout 读完（进程结束）
    才 `stderr.read()`，一旦子进程先把 stderr 管道缓冲区写满（Windows 默认仅 4-8KB），它会
    阻塞在写 stderr 上、不再写 stdout → 主线程永远读不到 stdout 下一行、也等不到进程退出 →
    双方互等死锁，直到平台 idle 超时误杀（codex `--json` 把大量日志打到 stderr 时必现）。
    解法：stderr 用独立线程并发抽干，两个管道各有读者，谁都不会把对方憋死。

    用法：
        drainer = _StderrDrainer(proc.stderr)   # 立即开始并发抽干
        for line in proc.stdout: ...            # 主线程安心读 stdout，不会被 stderr 憋死
        proc.wait()
        err = drainer.result()                   # join 线程后取完整 stderr 文本
    """
    def __init__(self, pipe):
        import threading
        self._box: list[str] = []
        self._pipe = pipe

        def _drain():
            try:
                for chunk in iter(lambda: pipe.read(4096), ""):
                    if not chunk:
                        break
                    self._box.append(chunk)
            except (OSError, ValueError):
                pass

        self._thread = threading.Thread(target=_drain, daemon=True)
        self._thread.start()

    def result(self, timeout: float = 5.0) -> str:
        """join 抽干线程并返回累计的 stderr 文本（超时兜底，不无限等）。"""
        self._thread.join(timeout=timeout)
        return "".join(self._box)


def build_cli_prompt(ctx: ExecContext) -> str:
    """CLI 后端（claude/codex）不吃独立的 history 参数，故把本轮指令 + 会话历史
    拼成一段完整 prompt 文本。**本轮指令放最前**（CLI 优先执行，避免被长历史淹没），
    历史作为背景附在后面。
    """
    if not ctx.history:
        return ctx.prompt
    lines = ["## 你本轮要做的事（最优先）", ctx.prompt, "",
             "## 本任务对话历史（背景参考）", ""]
    role_name = {"user": "用户", "assistant": "队友"}
    for m in ctx.history:
        who = role_name.get(m.get("role"), m.get("role", ""))
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"【{who}】{content}")
    return "\n".join(lines)

