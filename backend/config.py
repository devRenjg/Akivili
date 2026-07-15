"""Akivili 配置：多供应商（CLI / API）配置 + config.json 持久化。

支持三类供应商：
- type=api      纯 LLM API（Deepseek / OpenAI / Anthropic / Ollama），含 api_format 双格式
- type=claude-cli  本地 Claude Code CLI 执行器（claude -p）
- type=codex-cli   本地 Codex CLI 执行器（codex exec）

api_key 仅存本地 config.json（已被 git 忽略），读取给前端时应脱敏。
目录类配置（Agent 库 / 记忆 / Skills）支持环境变量覆盖，默认相对项目根目录。
"""
import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings

# 配置文件路径：默认 backend/config.json；可用环境变量 AKIVILI_CONFIG 覆盖
# （隔离测试用：指向临时 config.json，跑独立 DB/端口，不碰真实 jianagency.db）
CONFIG_FILE = Path(os.environ.get("AKIVILI_CONFIG") or (Path(__file__).parent / "config.json"))

ProviderType = Literal["api", "claude-cli", "codex-cli"]


class Provider(BaseModel):
    id: str                                   # 稳定标识（前端生成或后端补全）
    name: str                                 # 显示名，如 "Deepseek 主力"
    type: ProviderType = "api"
    api_key: str = ""                         # 仅 api 类型用
    base_url: str = ""                        # 仅 api 类型用
    model: str = ""                           # 模型名 / CLI 模型别名
    api_format: Literal["openai", "anthropic"] = "openai"
    executable: str = ""                      # CLI 类型可选：自定义可执行文件路径，空=按 PATH 探测


_ROOT = Path(__file__).parent.parent   # 项目根目录（backend 的上一级）


class Settings(BaseSettings):
    db_path: str = str(Path(__file__).parent / "jianagency.db")
    # Agent 模版库根目录：默认项目内 agents/，可用环境变量 AKIVILI_AGENT_LIBRARY_DIR 指向外部库
    agent_library_dir: str = os.environ.get("AKIVILI_AGENT_LIBRARY_DIR", str(_ROOT / "agents"))
    memory_dir: str = os.environ.get("AKIVILI_MEMORY_DIR", str(_ROOT / "memory"))   # Agent 记忆目录（每个 slug.md 一份）
    skills_dir: str = os.environ.get("AKIVILI_SKILLS_DIR", str(_ROOT / "skills"))   # Skill 库目录（每个 slug.md 一个能力指令）
    host: str = "0.0.0.0"                     # 内网开放：绑所有网卡（仅内网可达；外网需防火墙）
    port: int = 8100
    providers: list[Provider] = []            # 多供应商列表
    default_provider_id: str = ""             # 默认供应商
    # 协同并发池：同时最多跑几个 Agent run。默认 3，可用环境变量 AKIVILI_MAX_CONCURRENCY 覆盖。
    # 多项目/多 Agent 规模化时上调（受单机 CPU/内存/CLI 进程数约束，非越大越好）。
    max_concurrency: int = int(os.environ.get("AKIVILI_MAX_CONCURRENCY", "3"))
    # run 真失败（执行异常，非状态分叉伪失败、非人工 kill、非超时无交付）后的自动重试次数上限。
    # 默认 2（共最多 3 次执行）。可用环境变量 AKIVILI_MAX_RETRY 覆盖。
    max_retry: int = int(os.environ.get("AKIVILI_MAX_RETRY", "2"))
    # 单任务累计 run 总量闸：一个任务生命周期内最多入队多少个 run（绝对失控的最后兜底）。
    # 默认 200（原 20，为长程项目放大）。可用环境变量 AKIVILI_MAX_RUNS_PER_TASK 覆盖。
    max_runs_per_task: int = int(os.environ.get("AKIVILI_MAX_RUNS_PER_TASK", "200"))
    # 循环闸：该任务「连续的 mention 链式自动 run」上限（防 Agent 互相 @ 死循环烧 token）。
    # 只要中途有 assign/collaborate/人工重派介入即清零，故正常长程项目不受限，仅掐断纯 @ 死循环。
    # 默认 8。可用环境变量 AKIVILI_MAX_MENTION_CHAIN 覆盖。
    max_mention_chain: int = int(os.environ.get("AKIVILI_MAX_MENTION_CHAIN", "8"))
    # 会话历史回灌双限（保证成员上下文可控、不撑爆、防 lost-in-the-middle 幻觉）：
    # 条数上限 + 字符预算上限，取更严者。可用环境变量 AKIVILI_HISTORY_MAX_MSGS / _CHARS 覆盖。
    history_max_msgs: int = int(os.environ.get("AKIVILI_HISTORY_MAX_MSGS", "20"))
    history_max_chars: int = int(os.environ.get("AKIVILI_HISTORY_MAX_CHARS", "12000"))
    # 运行期孤儿巡检：定期扫 task_runs 里卡 running 但最后日志已静默超阈值的孤儿，主动补落终态，
    # 不必等下次重启的启动回收。覆盖任何路径的泄漏（含进程被硬杀——那时进程内兜底跑不到）。
    # 巡检间隔（秒，默认 120）；静默阈值（秒，默认 1800=30分，须 ≥ 最长 idle 超时以免误伤慢但在跑的 run）。
    orphan_sweep_interval_sec: int = int(os.environ.get("AKIVILI_ORPHAN_SWEEP_INTERVAL", "120"))
    orphan_sweep_idle_sec: int = int(os.environ.get("AKIVILI_ORPHAN_SWEEP_IDLE", "1800"))


def load_settings() -> Settings:
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return Settings(**data)
    return Settings()


def save_settings(settings: Settings) -> None:
    CONFIG_FILE.write_text(
        json.dumps(settings.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mask_key(key: str) -> str:
    """密钥脱敏：保留首尾各 4 位。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


# 测试项目标题前缀：这类项目的数据（记忆/工作区段落）不写入 Agent 记忆，避免污染真实身份记忆
TEST_PROJECT_PREFIXES = ("__test__", "__qa", "__conc")


def is_test_project(title: str) -> bool:
    """项目标题是否为测试项目（用于把测试数据挡在 Agent 记忆之外）。"""
    return any((title or "").startswith(p) for p in TEST_PROJECT_PREFIXES)
