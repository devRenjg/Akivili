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


def _default_pg_url() -> str:
    """未显式给 AKIVILI_DB_URL 时的默认 PostgreSQL 连接串（数据底座 S5：PG 单引擎）。

    按分段环境变量拼装，全部有本地开发默认值；口令单独走 AKIVILI_PG_PASSWORD——
    不把口令硬编码进源码（默认值 'akivili_dev_pw' 仅本地开发用，生产务必用环境变量覆盖）。
    driver 恒为 asyncpg（运行期异步）；迁移期由 _pg_sync_url() 转 psycopg。
    """
    host = os.environ.get("AKIVILI_PG_HOST", "localhost")
    port = os.environ.get("AKIVILI_PG_PORT", "5432")
    db = os.environ.get("AKIVILI_PG_DB", "akivili")
    user = os.environ.get("AKIVILI_PG_USER", "akivili")
    pw = os.environ.get("AKIVILI_PG_PASSWORD", "akivili_dev_pw")
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"


class Settings(BaseSettings):
    # 数据底座 S5：**PostgreSQL 单引擎**。SQLite 已退役（无降级、无 fallback）。
    # 运行期数据库 URL：
    #   - 优先取环境变量 AKIVILI_DB_URL（运维切库/指不同 PG 实例用，不落 config.json 避免口令入文件）；
    #   - 未设时用 _default_pg_url() 按 PG 分段环境变量拼默认串（口令走 AKIVILI_PG_PASSWORD）。
    # 恒为 postgresql+asyncpg://…（运行期异步驱动）。
    db_url: str = os.environ.get("AKIVILI_DB_URL", "") or _default_pg_url()
    # 旧 SQLite 文件路径。S5 运行期已不用（PG 单引擎），运行期/测试 seed 均不再引用。
    # 唯一使用者是 migrate_sqlite_to_pg.py（一次性割接工具，读旧库做**搬迁源**天然需要）——
    # 保留此字段直到真实数据 cutover 完成、割接工具退役。
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
    # 数据底座 S5b：PG 连接池调优（对标 Multica pgx 池的显式容量+存活探测，但按本平台单进程
    # MAX_CONCURRENCY 规模取值，不照搬其 MaxConns=25）。全部可用环境变量覆盖：
    #   - db_pool_size：常驻连接数。默认 5，够 MAX_CONCURRENCY=3 的并发查询+孤儿巡检+ping 富余。
    #   - db_max_overflow：峰值临时溢出连接数（超出 pool_size 的突发）。默认 5。
    #   - db_pool_timeout_sec：池满时等空闲连接的最长秒数，超时抛错而非无限阻塞。默认 30。
    #   - db_pool_recycle_sec：连接最长存活秒数，到期回收重建（防 PG 侧 idle 超时踢连接）。默认 1800。
    #   - db_pool_pre_ping：借出连接前 SELECT 1 探活，失效则丢弃重连（扛 PG 重启/网络抖动）。默认 True。
    db_pool_size: int = int(os.environ.get("AKIVILI_DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.environ.get("AKIVILI_DB_MAX_OVERFLOW", "5"))
    db_pool_timeout_sec: int = int(os.environ.get("AKIVILI_DB_POOL_TIMEOUT", "30"))
    db_pool_recycle_sec: int = int(os.environ.get("AKIVILI_DB_POOL_RECYCLE", "1800"))
    db_pool_pre_ping: bool = os.environ.get("AKIVILI_DB_POOL_PRE_PING", "1") != "0"


def load_settings() -> Settings:
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return Settings(**data)
    return Settings()


def _pg_sync_url(url: str) -> str:
    """把运行期 PG URL（postgresql+asyncpg://…）转成迁移期同步 psycopg URL。

    Alembic 走同步引擎，asyncpg 是纯异步驱动跑不了；psycopg v3 是同步驱动。
    统一把任意 driver 段替换成 +psycopg：
      postgresql+asyncpg://…  → postgresql+psycopg://…
      postgresql://…          → postgresql+psycopg://…
    """
    scheme, sep, rest = url.partition("://")
    base = scheme.split("+", 1)[0]   # 取 driver 前的方言名，如 "postgresql"
    return f"{base}+psycopg{sep}{rest}"


def migration_db_url() -> str:
    """迁移期（Alembic，同步）数据库 URL —— db_migrate.py 与 migrations/env.py 的单一真相源。

    数据底座 S5：PG 单引擎。运行期 db_url（asyncpg）转成同步 psycopg URL 供 Alembic。
    AKIVILI_DB_URL 环境变量已在 Settings.db_url 里最高优先，故此处直接取 settings.db_url。
    """
    return _pg_sync_url(load_settings().db_url)


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
