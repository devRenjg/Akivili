"""Agent 记忆接口：读取 / 覆盖写 / 追加。"""
from fastapi import APIRouter, HTTPException, Depends

from auth import require_admin
from pydantic import BaseModel

import memory as memory_mod

router = APIRouter(prefix="/api/memory", tags=["memory"])


class WriteMemoryRequest(BaseModel):
    content: str


class AppendMemoryRequest(BaseModel):
    text: str


@router.get("/{slug}")
async def read_memory(slug: str):
    try:
        return {"slug": slug, "content": memory_mod.read_memory(slug)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/{slug}", dependencies=[Depends(require_admin)])
async def write_memory(slug: str, req: WriteMemoryRequest):
    try:
        memory_mod.write_memory(slug, req.content)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{slug}/append", dependencies=[Depends(require_admin)])
async def append_memory(slug: str, req: AppendMemoryRequest):
    try:
        memory_mod.append_memory(slug, req.text)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))
