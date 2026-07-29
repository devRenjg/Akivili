"""PostgreSQL 就绪探测（数据底座 S5：PG 单引擎，无降级）。

启动脚本在拉起后端前调用本脚本，确认 PG 可连接后再放行——否则后端会在启动即
`alembic upgrade head`（main._startup）时以晦涩的驱动异常崩溃。本脚本用与后端**完全
同源**的连接参数（config._pg_sync_url(settings.db_url)，走同一套 AKIVILI_PG_* /
AKIVILI_DB_URL 环境变量），零参数漂移。

行为：
  - 在超时窗口内轮询直到连上（PG 开机自启/容器刚 start 时通常需要几秒才 accept 连接）；
  - 连上立即 exit 0；
  - 窗口耗尽仍连不上 → 打印清晰的排查指引 + exit 1（不放行注定崩溃的后端）。

窗口/间隔（秒）可用环境变量覆盖：
  AKIVILI_PG_WAIT_TIMEOUT（默认 30）、AKIVILI_PG_WAIT_INTERVAL（默认 1）。
"""
from __future__ import annotations

import os
import sys
import time

import psycopg

from config import migration_db_url


def _redact(url: str) -> str:
    """把连接串里的口令抹成 ***，供日志安全打印（绝不回显明文口令）。"""
    # postgresql+psycopg://user:pw@host:port/db  →  …:***@…
    at = url.rfind("@")
    if at == -1:
        return url
    head = url[:at]
    colon = head.rfind(":")
    scheme_sep = head.find("://")
    if colon > scheme_sep + 2:  # 冒号在 user 之后（即确有口令段），才抹
        head = head[:colon] + ":***"
    return head + url[at:]


def main() -> int:
    timeout = float(os.environ.get("AKIVILI_PG_WAIT_TIMEOUT", "30"))
    interval = float(os.environ.get("AKIVILI_PG_WAIT_INTERVAL", "1"))
    # migration_db_url() 是迁移侧单一真相源（= _pg_sync_url(settings.db_url)），恒为
    # postgresql+psycopg://…；psycopg.connect 用不带 +psycopg 的标准 libpq URL。
    libpq_url = migration_db_url().replace("postgresql+psycopg://", "postgresql://", 1)
    safe = _redact(libpq_url)

    print(f"[wait_for_pg] 探测 PostgreSQL 就绪：{safe}（超时 {timeout:.0f}s，间隔 {interval:.0f}s）")
    deadline = time.monotonic() + timeout
    last_err = ""
    attempt = 0
    while True:
        attempt += 1
        try:
            with psycopg.connect(libpq_url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            print(f"[wait_for_pg] PostgreSQL 就绪（第 {attempt} 次探测成功）。")
            return 0
        except Exception as e:  # noqa: BLE001 — 连不上的各类异常都归为「未就绪，重试」
            last_err = f"{type(e).__name__}: {e}"
            if time.monotonic() >= deadline:
                break
            time.sleep(interval)

    print(f"[wait_for_pg] ✗ {timeout:.0f}s 内无法连接 PostgreSQL。最后一次错误：\n  {last_err}",
          file=sys.stderr)
    print(
        "\n排查指引：\n"
        "  1) PG 服务是否已启动（本地 service / Docker 容器 / 远端实例）？\n"
        "  2) 连接参数是否正确：AKIVILI_DB_URL，或分段 AKIVILI_PG_HOST/PORT/DB/USER/PASSWORD？\n"
        "  3) 目标库与角色是否已创建、口令是否匹配？\n"
        "  4) 防火墙/端口（默认 5432）是否放通？\n"
        "S5 为 PostgreSQL 单引擎、无 SQLite 降级——PG 不可用时后端不会启动。",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
