"""Akivili 配置：多供应商（CLI / API）配置 + config.json 持久化。

参照 Qlipoth 的 config.py 模式，但配置面更宽：
- type=api      纯 LLM API（Deepseek / OpenAI / Anthropic / Ollama），含 api_format 双格式
- type=claude-cli  本地 Claude Code CLI 执行器（claude -p）
- type=codex-cli   本地 Codex CLI 执行器（codex exec）

api_key 仅存本地 config.json（已被 git 忽略），读取给前端时应脱敏。
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


class Settings(BaseSettings):
    db_path: str = str(Path(__file__).parent / "jianagency.db")
    agent_library_dir: str = r"C:\Code\Agents"   # Agent 模版库根目录
    memory_dir: str = r"C:\Code\JianAgency\memory"   # Agent 记忆目录（每个 slug.md 一份）
    skills_dir: str = r"C:\Code\JianAgency\skills"   # Skill 库目录（每个 slug.md 一个能力指令）
    host: str = "0.0.0.0"                     # 内网开放：绑所有网卡（仅内网可达；外网需防火墙）
    port: int = 8100
    providers: list[Provider] = []            # 多供应商列表
    default_provider_id: str = ""             # 默认供应商


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
