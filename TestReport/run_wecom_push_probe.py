"""Akivili 企微群机器人推送探针。

验证「任务卡片一键推送企微」核心逻辑：
  1. build_task_markdown 结构：标题(#) + 副标题 + 正文 + 「详情请点击」链接。
  2. 字节安全截断：超长正文/整体按 UTF-8 字节截到 ≤4096（不切坏多字节汉字）。
  3. send_markdown 结果归一化：errcode=0→ok；非0→带 errmsg 的失败；网络异常→ok=False 不抛。
  4. 空链接/空副标题的降级（不渲染对应行）。

send_markdown 用 monkeypatch 假 httpx.AsyncClient，绝不发真实网络请求、不碰真实 webhook。
纯模块单测，不依赖 DB/app。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import wecom  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


class _FakeResp:
    def __init__(self, data: dict):
        self._data = data
        self.content = b"x"

    def json(self):
        return self._data


class _FakeClient:
    """假 httpx.AsyncClient：记录 POST 的 payload，返回预设响应。"""
    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.captured = {"url": url, "json": json}
        return _FakeResp(_FakeClient._resp)


async def scenario(p: Probe) -> None:
    # ── 1) markdown 结构 ────────────────────────────────────────────────────────
    c = wecom.build_task_markdown("周会纪要", "正文内容",
                                  "http://10.23.28.27:8100/projects/26/tasks/162",
                                  subtitle="建议对接如下")
    p.check("标题渲染为 # 一级标题", c.startswith("# 周会纪要"), c.split(chr(10))[0])
    p.check("副标题渲染", "建议对接如下" in c)
    p.check("正文渲染", "正文内容" in c)
    p.check("详情链接渲染为 markdown 链接",
            "详情请点击" in c and "](http://10.23.28.27:8100/projects/26/tasks/162)" in c)

    # ── 2) 空链接/空副标题降级 ──────────────────────────────────────────────────
    c2 = wecom.build_task_markdown("仅标题", "只有正文", link="", subtitle="")
    p.check("空链接不渲染「详情请点击」行", "详情请点击" not in c2)
    p.check("空副标题不多插空段", c2.count("仅标题") == 1 and "只有正文" in c2)

    # ── 3) 字节安全截断（≤4096，不切坏汉字）─────────────────────────────────────
    big = wecom.build_task_markdown("T", "中" * 5000, "http://x/1")
    nbytes = len(big.encode("utf-8"))
    p.check("超长内容截到 ≤4096 字节", nbytes <= 4096, f"字节={nbytes}")
    # 能整体 UTF-8 解码（没切坏多字节序列）
    try:
        big.encode("utf-8").decode("utf-8")
        decodable = True
    except UnicodeDecodeError:
        decodable = False
    p.check("截断不切坏多字节汉字（可整体解码）", decodable)

    # ── 4) send_markdown 结果归一化（monkeypatch httpx）────────────────────────
    import httpx  # noqa: PLC0415
    orig = httpx.AsyncClient
    httpx.AsyncClient = _FakeClient
    try:
        # 成功：errcode=0
        _FakeClient._resp = {"errcode": 0, "errmsg": "ok"}
        r = await wecom.send_markdown("http://fake/webhook", "hello")
        p.check("errcode=0 → ok=True", r["ok"] is True, str(r))
        p.check("POST 体为 markdown msgtype",
                _FakeClient.captured["json"]["msgtype"] == "markdown"
                and _FakeClient.captured["json"]["markdown"]["content"] == "hello")
        # 失败：非 0 errcode（如 webhook 失效）
        _FakeClient._resp = {"errcode": 93000, "errmsg": "invalid webhook url"}
        r2 = await wecom.send_markdown("http://fake/webhook", "hi")
        p.check("非0 errcode → ok=False 带 errmsg",
                r2["ok"] is False and "invalid webhook url" in r2["error"], str(r2))
    finally:
        httpx.AsyncClient = orig

    # ── 5) 网络异常归一化（不抛，ok=False）──────────────────────────────────────
    class _BoomClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise httpx.ConnectError("boom")
    httpx.AsyncClient = _BoomClient
    try:
        r3 = await wecom.send_markdown("http://fake/webhook", "x")
        p.check("网络异常 → ok=False 不抛", r3["ok"] is False, str(r3))
    finally:
        httpx.AsyncClient = orig


async def main() -> int:
    p = Probe()
    await scenario(p)
    total = len(p.results)
    passed = sum(1 for r in p.results if r[1])
    print(f"\n=== wecom push probe: {passed}/{total} ===")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
