"""执行调度：组装上下文 → 选后端 → 流式执行 → 收工写记忆。

每次执行（一次 @ 分派）独立调用，无 CLI session；靠记忆 + 会话历史回灌恢复上下文。
维护 run_id → PID 注册表，供 kill。
"""
import os
import signal

from config import load_settings
from database import get_connection
from memory import read_memory, append_memory
from executor.base import ExecContext
from executor.claude_code import ClaudeCodeBackend
from executor.codex import CodexBackend
from executor.api_llm import ApiLlmBackend

# run_id -> pid，用于 kill
_RUN_PIDS: dict[int, int] = {}
# 被主动 kill 的 run_id 集合，用于标记最终状态
_KILLED: set[int] = set()
# run_id -> 写记忆所需上下文（slug/task/prompt/起始消息id）。
# 让「收工写记忆」无论从正常收尾还是超时兜底（finalize_run）触发，都能拿到同一份上下文，
# 且谁先写谁 pop，天然幂等、不重复写。
_RUN_CTX: dict[int, dict] = {}

# 平台 CLI 使用说明（注入所有 Agent 的系统提示，教它用 jian 在平台上真正操作）
JIAN_CLI_USAGE = """## 平台操作：jian CLI（唯一有效方式）

你运行在 **Akivili 多 Agent 平台**里。你在平台上的一切真实动作，**只能**通过命令行的 `jian` 命令完成。当前任务与你的身份已注入环境，直接调用即可：

- `jian roster` —— 查看团队花名册（本项目里真实有哪些成员、各自职责/技能、@ 名字）。
- `jian comment "内容"` —— 在当前任务里发言（结论、汇报、追问）。发言里写 `@成员名` 会真正触发那个成员来接力。
  - 🔴 **多行 / 长内容（如自我介绍、方案、报告）必须先写进一个 .md 文件，再用 `jian comment --body-file <文件>` 发**，
    绝不要用 `jian comment "$(cat 文件)"` 或直接把大段多行文本塞进引号——那样在 Windows 下会被截断成只剩第一行，你的正文会大部分丢失。
- `jian status <backlog|in_progress|reviewing|done|blocked>` —— 改当前任务状态。
- `jian subtask --title "标题" [--owner <成员名>] [--assign] --body-file <文件>` —— 创建子任务卡片并挂到当前任务。
  - 不加 `--assign`：记录你自己的产出（Owner=你，卡片直接标记完成）。
  - 加 `--assign --owner <成员名>`：委派给该成员——建一张卡片指派给他并**自动触发他在自己卡片里完成**（适合"每人各自产出一份"的场景，给每个成员各建一张）。

🔴 **铁律（必须遵守，否则你的工作全部作废、用户完全看不到）**：
- **绝对不要使用你自己内置的任务/待办工具**（如 TaskCreate / TodoWrite / 内置 Agent 子代理派生 / TaskList 之类）。那些只存在于你本地的草稿里，**不会进入 Akivili 平台，用户看不到、也不算数**。真实建卡/发言/派活**只有 `jian` 一条路**。
- **绝对不要用 Bash / 脚本 / SQL 直接读写平台数据库或伪造数据**（不要去找 .db 文件、不要 INSERT/UPDATE tasks 表、不要自己模拟"成员"）。要让某个成员真正干活，**只能** `jian subtask --owner <成员名> --assign`，由平台真实唤醒他本人来执行——你替他写的内容不算他的产出。
- 需要知道团队成员时，**看本系统提示里的「你的团队」花名册即可**（已给全，无需 `jian roster`、更不用探索文件系统），只与花名册里真实列出的成员协作，**绝不凭想象编造成员**。
- 你的最终产出/结论**必须通过 `jian comment` 或 `jian subtask` 落到平台上**；光在终端打印或用内置工具，等于没做。
- 需要写长正文时，先把内容写进一个 .md 文件，再用 `--body-file` 传给 `jian subtask` 或 `jian comment`（避免命令行转义/多行截断问题）。
- 请用**中文**作答与操作。
"""


def register_pid(run_id: int, pid: int) -> None:
    _RUN_PIDS[run_id] = pid


def kill_run(run_id: int) -> bool:
    pid = _RUN_PIDS.get(run_id)
    if not pid:
        return False
    _KILLED.add(run_id)
    try:
        if os.name == "nt":
            # os.kill(SIGTERM) 只结束 claude/codex 父进程，其派生的子进程（真正干活的）会成孤儿继续跑。
            # 必须用 taskkill /T 杀整棵进程树，/F 强制，才能真正终止执行。
            import subprocess
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            # 负号 = 杀整个进程组（需子进程以 start_new_session 起；否则退回杀单进程）
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, OSError):
        return False
    finally:
        _RUN_PIDS.pop(run_id, None)


async def finalize_run(run_id: int, status: str) -> None:
    """主动把 run 落成终态、写收工记忆并清理注册表。
    供超时/取消路径调用——那时 execute_dispatch 生成器被取消，其内部收尾（_finish_run + 写记忆）跑不到，
    需外部补齐，否则 task_runs 永远卡在 running（孤儿记录）、且做完的任务不沉淀记忆。
    与 execute_dispatch 正常收尾共用 _persist_memory（谁先跑谁 pop 上下文），天然幂等、不重复写。"""
    await _finish_run(run_id, status)
    await _persist_memory(run_id)
    _RUN_PIDS.pop(run_id, None)
    _KILLED.discard(run_id)


# per-run「近期动态」受管段落保留的最大条数（滚动，防无限膨胀）
_RECENT_RUNS_MAX = 8


async def _persist_memory(run_id: int) -> None:
    """把该 run 记入 Agent 记忆的「近期动态」滚动段落。幂等：靠 _RUN_CTX.pop，写一次后上下文即消失。

    职责边界（重要）：
    - 这里只做**轻量流水**——最近做过啥，滚动保留最新 N 条，供开工快速回忆近期上下文。
    - **真正的经验/Know-how 学习**由 reflect.py 在**任务 done** 时触发（让角色复盘提炼），不在这里。
    取「真实产出」优先级：Agent 经 jian comment 落 messages 的发言 > 流式 stdout 兜底。
    """
    ctx = _RUN_CTX.pop(run_id, None)
    if not ctx:
        return
    from config import is_test_project
    if is_test_project(ctx.get("task_title", "")):
        return  # 测试项目不污染真实身份记忆
    slug = ctx["slug"]
    conv_id = ctx["conv_id"]
    since_msg_id = ctx.get("since_msg_id", 0)
    db = await get_connection()
    try:
        rows = await (await db.execute(
            """SELECT content FROM messages
               WHERE conversation_id=? AND id>? AND role='assistant' AND (author_slug=? OR author_slug='')
               ORDER BY id""", (conv_id, since_msg_id, slug))).fetchall()
    finally:
        await db.close()
    deliverable = "\n".join(r["content"] for r in rows if (r["content"] or "").strip()).strip()
    conclusion = deliverable or (ctx.get("stream_text") or "").strip()
    if not conclusion:
        return  # 什么都没产出，不记

    # 滚动更新「近期动态」受管段落：读现有条目 → 追加本条 → 只留最新 _RECENT_RUNS_MAX 条
    from memory import upsert_managed_section
    import re as _re
    key = "recent"
    mem = read_memory(slug)
    m = _re.search(r"<!-- akivili:managed:recent:start -->(.*?)<!-- akivili:managed:recent:end -->",
                   mem, _re.DOTALL)
    entries = _re.findall(r"(?m)^### .+?(?=\n### |\Z)", m.group(1), _re.DOTALL) if m else []
    entries = [e.strip() for e in entries if e.strip()]
    # 条目带任务归属标记（HTML 注释、渲染不可见），供任务删除时精准清理
    from memory import task_marker
    marker = task_marker(ctx["task_id"]) if ctx.get("task_id") else ""
    entry = (f"### {ctx.get('task_title','(无标题)')} {marker}\n"
             f"- 指令：{ctx.get('prompt','')[:100]}\n"
             f"- 我的产出：{conclusion[:300]}")
    entries.append(entry)
    entries = entries[-_RECENT_RUNS_MAX:]
    body = "## 🗒️ 近期做过的任务（最新在下，仅供回忆上下文）\n\n" + "\n\n".join(entries)
    upsert_managed_section(slug, key, body)


def _provider_by_id(pid_str: str):
    for p in load_settings().providers:
        if p.id == pid_str:
            return p
    return None


def _pick_backend(provider):
    if provider is None:
        return None
    if provider.type == "claude-cli":
        return ClaudeCodeBackend()
    if provider.type == "codex-cli":
        return CodexBackend()
    if provider.type == "api":
        return ApiLlmBackend()
    return None


async def build_context(agent_slug: str, persona: str, project_dir: str,
                        provider, prompt: str, history: list,
                        is_leader: bool = False, project_id: int = 0, task_id: int = 0) -> ExecContext:
    """组装系统提示：身份锚 + 人格 + 记忆 + Skills + 团队花名册 + 平台 CLI 说明 +（Leader）协作协议。

    - 身份锚放最前，压过 Claude 默认"编码助手先探索"的本能。
    - 团队花名册注入给**所有** Agent（让整个 Team 互相认识），Leader 额外注入统筹协议。
    - 注入 jian CLI 身份环境变量，使 Agent 能在平台上建子任务/发言/改状态。
    """
    mem = read_memory(agent_slug)
    skill_bodies = await _skill_bodies(agent_slug)

    sections = []
    # 0) 身份锚 + 反探索：放最前面，压过 Claude 默认"编码助手先探索代码库"的本能
    identity_anchor = (
        "# 首要须知（最高优先）\n"
        "你是 **Akivili 多 Agent 协作平台**里的一名 Agent，隶属于一个**项目团队**。"
        "你的身份、人格、职责、记忆、所属团队与队友，都已在**本系统提示里给全**。\n"
        "- **始终用简体中文思考与回复**（除非用户明确要求其它语言）。所有发言、汇报、建卡内容都用中文。\n"
        "- **不要去探索文件系统找“团队/成员/任务系统”**——项目工作目录为空是正常的，团队不在磁盘上、在下方花名册里。\n"
        "- **不要读代码库来“搞清楚这是什么项目”再自由发挥**——按你收到的人格与任务直接执行。\n"
        "- 你在平台上的一切真实动作只通过 `jian` 命令完成（详见下方说明）。"
    )
    sections.append(identity_anchor)
    if persona:
        sections.append(f"# 你的人格与职责\n{persona}")
    if mem:
        sections.append(f"# 你的记忆\n{mem}")
    if skill_bodies:
        sections.append("# 你的技能（Skills）\n" + "\n\n".join(skill_bodies))
    # 团队花名册：注入给**所有** Agent（不只负责人），让整个 Team 互相认识
    if project_id:
        from collab import build_roster
        sections.append(await build_roster(project_id, agent_slug))
    # 平台 CLI 说明（所有 Agent 都能用）
    sections.append(JIAN_CLI_USAGE)
    # 负责人：额外注入统筹协作协议
    if is_leader and project_id:
        from collab import TEAM_LEADER_PROTOCOL
        sections.append(TEAM_LEADER_PROTOCOL)
    system_prompt = "\n\n---\n\n".join(sections)

    from config import load_settings as _ls
    _s = _ls()
    env_extra = {
        "JIAN_API": f"http://127.0.0.1:{_s.port}",
        "JIAN_TASK_ID": str(task_id),
        "JIAN_AGENT_SLUG": agent_slug,
    }

    model = provider.model if provider else ""
    return ExecContext(
        prompt=prompt,
        system_prompt=system_prompt,
        project_dir=project_dir,
        model=model,
        api_key=getattr(provider, "api_key", "") if provider else "",
        base_url=getattr(provider, "base_url", "") if provider else "",
        api_format=getattr(provider, "api_format", "openai") if provider else "openai",
        history=history,
        env_extra=env_extra,
    )


async def _skill_bodies(agent_slug: str) -> list[str]:
    db = await get_connection()
    try:
        rows = await (await db.execute(
            """SELECT s.name, s.body FROM agent_skills a JOIN skills s ON s.slug=a.skill_slug
               WHERE a.agent_slug=?""", (agent_slug,))).fetchall()
        return [f"## {r['name']}\n{r['body']}" for r in rows if (r["body"] or "").strip()]
    finally:
        await db.close()


def get_backend_for(provider):
    return _pick_backend(provider)


async def run_oneshot(provider_id: str, system_prompt: str, prompt: str,
                      project_dir: str = ".", timeout_sec: int = 300) -> str:
    """一次性模型调用：给定人格(system_prompt)+指令(prompt)，返回纯文本结果。

    不建 run、不落 task_runs/messages、不碰任务会话——纯粹借某个 Agent 的模型「想一段话」。
    供反思/总结这类「让 Agent 用自己的脑子产出一段结论、但不作为平台动作」的场景。
    超时或异常返回空串（调用方据空串跳过，不影响主流程）。
    """
    from executor.base import ExecContext
    provider = _provider_by_id(provider_id)
    backend = _pick_backend(provider)
    if backend is None:
        return ""
    ctx = ExecContext(
        prompt=prompt,
        system_prompt=system_prompt,
        project_dir=project_dir or ".",
        model=provider.model if provider else "",
        api_key=getattr(provider, "api_key", "") if provider else "",
        base_url=getattr(provider, "base_url", "") if provider else "",
        api_format=getattr(provider, "api_format", "openai") if provider else "openai",
        history=[],
        env_extra={},
    )
    collected: list[str] = []

    async def _consume():
        async for ev in backend.run(ctx):
            if ev.type == "text":
                collected.append(ev.text)

    try:
        import asyncio as _aio
        await _aio.wait_for(_consume(), timeout=timeout_sec)
    except Exception:  # noqa: BLE001 — 反思失败不该影响任务完成主流程
        pass
    return "".join(collected).strip()


async def execute_dispatch(task: dict, agent: dict, prompt: str,
                           persist_user_msg: bool = True, user_name: str = ""):
    """完整执行闭环，异步生成器逐个 yield ExecEvent。

    task: tasks 行；agent: project_agents 行（含 slug/persona/provider 解析）。
    落库：user 消息、assistant 消息、run、run_logs；收工写记忆；维护 run 状态。

    persist_user_msg: 是否把 prompt 作为一条「用户消息」落库并进时间线。
      - True（默认）：thread 里人手输入的 @ 指令 —— 是真人说的话，应展示。
      - False：auto-dispatch / 协同唤醒时，prompt 是**机器合成的派活指令**
        （任务简报/成员指令），不是人说的；落成 user 消息会变成「以我的名义
        重复复述任务」的噪声，故不落库（仍作为本轮 prompt 喂给 Agent 执行）。
    user_name: 落 user 消息时记录的发送者名（登录用户名），供时间线按人显示。
    """
    from executor.base import ExecEvent

    conv_id = task["conversation_id"]
    slug = agent["slug"]
    provider = _provider_by_id(agent.get("provider_id_effective", ""))
    backend = _pick_backend(provider)

    db = await get_connection()
    try:
        if persist_user_msg:
            # 人手输入的指令：落成 user 消息（带发送者名），进时间线展示
            ucur = await db.execute(
                "INSERT INTO messages (conversation_id, role, content, author_name) VALUES (?,?,?,?)",
                (conv_id, "user", prompt, user_name or ""))
            user_msg_id = ucur.lastrowid   # 本轮起点：之后该 Agent 的发言才算本轮产出
            # 取会话历史（回灌，恢复上下文），不含刚插入的本轮
            rows = await (await db.execute(
                "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,))).fetchall()
            history = [{"role": r["role"], "content": r["content"]} for r in rows[:-1]]
        else:
            # 机器合成的派活指令：不落 user 消息（否则时间线会以「我」名义重复复述任务）。
            # 本轮起点取当前最大消息 id，之后该 Agent 的发言才算本轮产出。
            rows = await (await db.execute(
                "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,))).fetchall()
            history = [{"role": r["role"], "content": r["content"]} for r in rows]
            mrow = await (await db.execute(
                "SELECT COALESCE(MAX(id), 0) AS mid FROM messages WHERE conversation_id=?", (conv_id,))).fetchone()
            user_msg_id = mrow["mid"]
        # 建 run
        cur = await db.execute(
            "INSERT INTO task_runs (task_id, conversation_id, agent_slug, provider_id, status) VALUES (?,?,?,?, 'running')",
            (task["id"], conv_id, slug, provider.id if provider else ""))
        run_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()

    # 登记写记忆上下文：正常收尾与超时兜底(finalize_run)共用，谁先写谁 pop，幂等不重复
    _RUN_CTX[run_id] = {
        "slug": slug, "conv_id": conv_id, "since_msg_id": user_msg_id,
        "task_id": task.get("id", 0),
        "task_title": task.get("title", ""), "prompt": prompt, "stream_text": "",
    }

    yield ExecEvent("system", "", {"run_id": run_id})
    from activity import log_activity
    await log_activity(task["id"], "task_started", "agent", agent.get("name", slug),
                       {"run_id": run_id})

    if backend is None:
        await _finish_run(run_id, "failed")
        await log_activity(task["id"], "task_failed", "agent", agent.get("name", slug),
                           {"reason": "未接入有效模型"})
        await _log(run_id, "system", "该 Agent 未接入有效模型，请在团队里为其选择供应商")
        yield ExecEvent("error", "该 Agent 未接入有效模型，请先在团队里为其选择供应商")
        yield ExecEvent("done")
        return

    project_dir = task["project_dir"]
    persona = agent.get("persona", "")
    ctx = await build_context(slug, persona, project_dir, provider, prompt, history,
                              is_leader=bool(agent.get("is_leader_run")),
                              project_id=task.get("project_id", 0), task_id=task.get("id", 0))

    collected: list[str] = []
    had_error = False

    def _on_pid(pid):
        register_pid(run_id, pid)

    try:
        async for ev in backend.run(ctx, on_pid=_on_pid):
            if ev.type == "text":
                collected.append(ev.text)
                await _log(run_id, "stdout", ev.text)
            elif ev.type == "thinking":
                await _log(run_id, "thinking", ev.text)
            elif ev.type == "tool":
                # 完整命令/参数落库（tool_input），列表展示用摘要 content
                await _log(run_id, "tool", ev.text, tool=ev.tool, tool_input=ev.tool_input)
            elif ev.type == "tool_result":
                await _log(run_id, "tool_result", ev.text, tool=ev.tool, tool_output=ev.tool_output)
            elif ev.type == "system":
                await _log(run_id, "system", ev.text)
            elif ev.type == "error":
                had_error = True
                await _log(run_id, "stderr", ev.text)
            yield ev
    except Exception as e:  # noqa: BLE001 — 兜底，绝不让执行把整个请求带崩
        had_error = True
        await _log(run_id, "stderr", f"执行异常：{type(e).__name__}: {e}")
        yield ExecEvent("error", f"执行异常：{type(e).__name__}")

    final_text = "".join(collected).strip()
    # 若该 run 被 kill（标记在 _KILLED 中），状态为 killed
    if run_id in _KILLED:
        status = "killed"
        _KILLED.discard(run_id)
    elif had_error:
        status = "failed"
    else:
        status = "succeeded"

    # 落库助手消息（决定会话正文里展示什么）：按后端类型分流。
    #   - CLI 后端（claude/codex）：Agent 的**真实交付**走 `jian comment`/`jian subtask`（已单独落库、干净）。
    #     流式 stdout 是「边干边碎念」的过程文本（jian.bat 调用、设 PYTHONUTF8、编码显示问题……），
    #     只应进 run_logs 供日志详情排查，**不落成会话消息**，否则会把命令细节塞进正文污染阅读。
    #   - API 后端：无 jian 通道，stdout final_text **即** Agent 的唯一产出，必须落库展示。
    # 与 _persist_memory 的判别原则一致（jian comment 发言=真实产出 > stdout 兜底）。
    is_cli = bool(provider and provider.type in ("claude-cli", "codex-cli"))
    if final_text and not is_cli:
        await _save_assistant(conv_id, final_text, author_slug=slug)
    # 流式文本始终作为「收工写记忆」的兜底（_persist_memory 仍优先取 jian comment 发言）。
    if run_id in _RUN_CTX:
        _RUN_CTX[run_id]["stream_text"] = final_text
    await _finish_run(run_id, status)
    await _persist_memory(run_id)
    _RUN_PIDS.pop(run_id, None)
    act = {"succeeded": "task_completed", "failed": "task_failed", "killed": "task_failed"}[status]
    await log_activity(task["id"], act, "agent", agent.get("name", slug),
                       {"run_id": run_id, "summary": final_text[:120]})

    # CLI run 未产出 jian 交付的标记：CLI Agent 的真实产出必须走 jian comment/subtask 落库。
    # 若本轮成功结束、却没有任何该 Agent 的落库发言（只在 stdout 说了话、忘了 jian），
    # 正文里会没有它的痕迹 —— 我们**不拿 stdout 错误兜底**（目标是让 jian comment 100% 出现），
    # 而是打一条醒目活动标记，便于发现并追查（stdout 全文仍在 run_logs / 日志详情里）。
    if (is_cli and status == "succeeded" and final_text
            and not await _has_jian_deliverable(conv_id, slug, user_msg_id, task["id"], run_id)):
        await log_activity(task["id"], "commented", "system", "",
                           {"note": f"⚠️ {agent.get('name', slug)} 本轮只在终端输出、未通过 jian comment/subtask "
                                    f"提交交付，正文无其产出（完整过程见执行日志详情 run #{run_id}）。"})


async def _log(run_id: int, channel: str, content: str,
               tool: str = "", tool_input: dict | None = None, tool_output: str = ""):
    import json as _json
    ti = _json.dumps(tool_input, ensure_ascii=False) if tool_input else ""
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO run_logs (run_id, channel, content, tool, tool_input, tool_output) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, channel, content, tool, ti, tool_output))
        await db.commit()
    finally:
        await db.close()


async def _has_jian_deliverable(conv_id: int, slug: str, since_msg_id: int,
                                task_id: int, run_id: int) -> bool:
    """本轮该 Agent 是否用 jian 做过真实平台动作（发言 / 建卡 / 改状态）。

    覆盖 jian 的三条落库路径（agent_cli.py），任一命中即视为有交付：
    - `jian comment`：在本会话落一条 author_slug=<本人> 的 assistant 消息（不记活动）
      → 查本轮起点 since_msg_id 之后、作者为本人的消息。
    - `jian subtask`：记一条 action='commented'、actor_name=<本人 slug> 的活动（委派/自产出子任务）。
    - `jian status`：记一条 action='status_changed'、actor_name=<本人 slug> 的活动。
      → 查本 run 启动之后（created_at >= task_runs.started_at）该 slug 的上述活动。
    CLI 后端的 stdout 已不落会话消息，故消息检查不会把 stdout 误判成交付。
    """
    db = await get_connection()
    try:
        msg = await (await db.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND id>? AND role='assistant' "
            "AND author_slug=? LIMIT 1", (conv_id, since_msg_id, slug))).fetchone()
        if msg is not None:
            return True
        act = await (await db.execute(
            "SELECT 1 FROM activities a JOIN task_runs r ON r.id=? "
            "WHERE a.task_id=? AND a.actor_type='agent' AND a.actor_name=? "
            "AND a.action IN ('commented','status_changed') AND a.created_at >= r.started_at LIMIT 1",
            (run_id, task_id, slug))).fetchone()
        return act is not None
    finally:
        await db.close()


async def _save_assistant(conv_id: int, content: str, author_slug: str = ""):
    db = await get_connection()
    try:
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, author_slug) VALUES (?,?,?,?)",
            (conv_id, "assistant", content, author_slug))
        await db.commit()
    finally:
        await db.close()


async def _finish_run(run_id: int, status: str):
    db = await get_connection()
    try:
        await db.execute("UPDATE task_runs SET status=?, ended_at=datetime('now') WHERE id=?",
                         (status, run_id))
        await db.commit()
    finally:
        await db.close()

