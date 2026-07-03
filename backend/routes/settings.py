"""供应商配置接口：读取（脱敏）/ 保存 / 连通性测试 / 设默认。

安全约定：
- 读取列表时 api_key 脱敏返回；保存时若前端回传的是脱敏值（未改动），保留原始密钥。
- CLI 连通性检测一律列表传参、绝不 shell 拼接。
- 子进程检测放到线程池，避免阻塞事件循环。
"""
import asyncio
import shutil
import subprocess
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Depends

from auth import require_admin
from pydantic import BaseModel

from config import Provider, load_settings, mask_key, save_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])

# CLI 类型 → 默认探测的可执行文件名
CLI_EXECUTABLES = {"claude-cli": "claude", "codex-cli": "codex"}


def _is_masked(key: str) -> bool:
    """前端回传的脱敏密钥含 '*'，据此判断用户是否改动过。"""
    return "*" in key


@router.get("")
async def get_settings():
    s = load_settings()
    providers = []
    for p in s.providers:
        d = p.model_dump()
        d["api_key"] = mask_key(p.api_key)
        providers.append(d)
    return {"providers": providers, "default_provider_id": s.default_provider_id}


class SaveProvidersRequest(BaseModel):
    providers: list[Provider]
    default_provider_id: str = ""


@router.put("", dependencies=[Depends(require_admin)])
async def save_providers(req: SaveProvidersRequest):
    s = load_settings()
    existing = {p.id: p for p in s.providers}

    saved: list[Provider] = []
    for p in req.providers:
        if not p.id:
            p.id = uuid.uuid4().hex
        # 若 api_key 是脱敏值（未改动），保留原始密钥
        if _is_masked(p.api_key) and p.id in existing:
            p.api_key = existing[p.id].api_key
        saved.append(p)

    s.providers = saved
    valid_ids = {p.id for p in saved}
    s.default_provider_id = req.default_provider_id if req.default_provider_id in valid_ids else ""
    save_settings(s)
    return {"ok": True}


def _find_provider(provider_id: str) -> Provider:
    s = load_settings()
    for p in s.providers:
        if p.id == provider_id:
            return p
    raise HTTPException(404, "供应商不存在")


def _test_cli(provider: Provider) -> dict:
    exe = provider.executable or CLI_EXECUTABLES.get(provider.type, "")
    if not exe:
        return {"ok": False, "detail": "未知的 CLI 类型"}
    resolved = shutil.which(exe) if not provider.executable else provider.executable
    if not resolved:
        return {"ok": False, "detail": f"未找到可执行文件：{exe}"}
    try:
        # 列表传参，绝不 shell=True
        out = subprocess.run(
            [resolved, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""
        return {"ok": True, "detail": f"已检测到：{version or resolved}"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "detail": f"执行失败：{type(e).__name__}"}


async def _test_api(provider: Provider) -> dict:
    if not provider.api_key:
        return {"ok": False, "detail": "缺少 api_key"}
    base = provider.base_url.rstrip("/")
    # base_url 已含 /v1 时不重复拼（兼容 B 站内部网关等已带版本前缀的端点）
    has_v1 = base.endswith("/v1") or "/v1/" in base
    # 统一用 Authorization: Bearer（含 Anthropic 格式——内部网关认这个）
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            if provider.api_format == "anthropic":
                url = f"{base}/messages" if has_v1 else f"{base}/v1/messages"
                headers["anthropic-version"] = "2023-06-01"
            else:
                url = f"{base}/chat/completions" if has_v1 else f"{base}/v1/chat/completions"
            resp = await client.post(
                url, headers=headers,
                json={"model": provider.model, "max_tokens": 1,
                      "messages": [{"role": "user", "content": "hi"}]},
            )
        if resp.status_code < 400:
            return {"ok": True, "detail": f"连通成功（HTTP {resp.status_code}）"}
        # 401/403 等：密钥或模型问题，回传简短原因（不含敏感头）
        return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:160]}"}
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        return {"ok": False, "detail": f"连接失败：{type(e).__name__}"}


@router.post("/{provider_id}/test", dependencies=[Depends(require_admin)])
async def test_provider(provider_id: str):
    provider = _find_provider(provider_id)
    if provider.type == "api":
        return await _test_api(provider)
    # CLI 检测是阻塞子进程，卸载到线程
    return await asyncio.to_thread(_test_cli, provider)
