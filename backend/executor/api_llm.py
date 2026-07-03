"""纯 API 执行后端：httpx 流式对话（OpenAI / Anthropic 双格式）。

复用 settings 里验证过的逻辑：统一 Authorization: Bearer；base_url 含 /v1 不重复拼。
纯对话（不操作文件），用于配 api 类型供应商的 Agent。无子进程，故无 PID/kill。
"""
import json

import httpx

from .base import ExecutorBackend, ExecContext, ExecEvent


class ApiLlmBackend(ExecutorBackend):
    async def run(self, ctx: ExecContext, on_pid=None):
        if not ctx.api_key:
            yield ExecEvent("error", "该供应商缺少 api_key")
            yield ExecEvent("done")
            return

        base = ctx.base_url.rstrip("/")
        has_v1 = base.endswith("/v1") or "/v1/" in base
        headers = {"Authorization": f"Bearer {ctx.api_key}", "Content-Type": "application/json"}

        # 组装消息：system + 历史 + 本轮
        messages = []
        if ctx.api_format != "anthropic" and ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        messages += ctx.history
        messages.append({"role": "user", "content": ctx.prompt})

        if ctx.api_format == "anthropic":
            url = f"{base}/messages" if has_v1 else f"{base}/v1/messages"
            headers["anthropic-version"] = "2023-06-01"
            payload = {"model": ctx.model, "max_tokens": 4096, "stream": True,
                       "messages": messages}
            if ctx.system_prompt:
                payload["system"] = ctx.system_prompt
        else:
            url = f"{base}/chat/completions" if has_v1 else f"{base}/v1/chat/completions"
            payload = {"model": ctx.model, "max_tokens": 4096, "stream": True,
                       "messages": messages}

        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        yield ExecEvent("error", f"HTTP {resp.status_code}: {body[:300]}")
                        yield ExecEvent("done")
                        return
                    async for line in resp.aiter_lines():
                        ev = _parse_sse(line, ctx.api_format)
                        if ev:
                            yield ev
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            yield ExecEvent("error", f"请求失败：{type(e).__name__}")
        yield ExecEvent("done")


def _parse_sse(line: str, fmt: str) -> ExecEvent | None:
    if not line or not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    if fmt == "anthropic":
        if obj.get("type") == "content_block_delta":
            t = obj.get("delta", {}).get("text", "")
            return ExecEvent("text", t) if t else None
        return None
    # openai
    try:
        delta = obj["choices"][0].get("delta", {})
        t = delta.get("content", "")
        return ExecEvent("text", t) if t else None
    except (KeyError, IndexError):
        return None
