"""企微群机器人推送：把任务卡片内容一键发到企业微信群。

接入方式：**群机器人 webhook**（最简单，无需企微管理员建应用）。在企微群里添加「群机器人」
拿到 webhook URL，配到 config.json 的 `wecom_webhook_url`（敏感凭证，gitignore 已排除）。

消息用 markdown 类型：`{"msgtype":"markdown","markdown":{"content": "..."}}`。
企微限制：markdown content ≤ 4096 字节（UTF-8），超限企微会整条拒收，故本模块负责安全截断。
"""
import httpx

# 企微 markdown content 上限 4096 字节；留余量给「详情链接」尾巴，正文摘要截到这个字节数。
_WECOM_MAX_BYTES = 4096
_BODY_BUDGET_BYTES = 3200   # 正文摘要预算，其余留给标题/链接/装饰


def _clip_bytes(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节安全截断（不切坏多字节字符），超限尾部加省略号。"""
    b = text.encode("utf-8")
    if len(b) <= max_bytes:
        return text
    # 从 max_bytes 处往回退，直到能整体解码（避免切断一个汉字的多字节序列）
    cut = b[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8") + "…"
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "…"


def build_task_markdown(title: str, body: str, link: str = "",
                        subtitle: str = "") -> str:
    """把任务卡片拼成企微 markdown content。

    结构：# 标题 / （可选副标题一行）/ 正文摘要（截断保底）/ > 详情请点击：链接。
    正文按字节预算截断，整体再对 4096 上限做二次保底，确保企微不拒收。
    """
    parts = [f"# {title.strip()}"]
    if subtitle.strip():
        parts.append(subtitle.strip())
    body = (body or "").strip()
    if body:
        parts.append(_clip_bytes(body, _BODY_BUDGET_BYTES))
    if link.strip():
        # 企微 markdown 支持 [文字](链接)
        parts.append(f"> 详情请点击：[{link.strip()}]({link.strip()})")
    content = "\n\n".join(parts)
    return _clip_bytes(content, _WECOM_MAX_BYTES)


async def send_markdown(webhook_url: str, content: str, timeout: float = 10.0) -> dict:
    """向企微群机器人 webhook 发一条 markdown 消息。

    返回 {"ok": bool, "errcode": int, "error": str}。
    企微成功返回 {"errcode":0,"errmsg":"ok"}；非 0 即失败（如 webhook 失效 93000、限频 45009）。
    网络/超时异常也归一化成 ok=False，绝不抛给上层路由（由调用方转成友好提示）。
    """
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(webhook_url, json=payload)
        data = resp.json() if resp.content else {}
        errcode = int(data.get("errcode", -1))
        if errcode == 0:
            return {"ok": True, "errcode": 0, "error": ""}
        return {"ok": False, "errcode": errcode,
                "error": data.get("errmsg", f"企微返回 errcode={errcode}")}
    except httpx.HTTPError as e:
        return {"ok": False, "errcode": -1, "error": f"网络错误：{type(e).__name__}"}
    except Exception as e:  # noqa: BLE001 — 归一化，绝不冒泡
        return {"ok": False, "errcode": -1, "error": f"{type(e).__name__}: {e}"}
