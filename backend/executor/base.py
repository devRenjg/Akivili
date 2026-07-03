"""执行引擎抽象：统一的流式事件结构 + 后端接口。

每个后端把底层（claude -p / codex exec / API）的输出，标准化成 ExecEvent 序列：
- text:  助手文本增量
- tool:  工具/命令活动（仅作日志展示）
- system:系统提示（启动、结束原因等）
- error: 错误
- done:  结束（带最终文本）
"""
from dataclasses import dataclass, field
from typing import Literal

EventType = Literal["text", "tool", "system", "error", "done"]


@dataclass
class ExecEvent:
    type: EventType
    text: str = ""
    meta: dict = field(default_factory=dict)


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

