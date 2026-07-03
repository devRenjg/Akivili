"""头像图标：列出 icon 文件夹里的图 + 提供图片文件。路径穿越防护。"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/icons", tags=["icons"])

_ICON_DIR = Path(__file__).parent.parent.parent / "icon"
_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


@router.get("")
async def list_icons():
    """列出 icon 文件夹里的图片文件名。"""
    _ICON_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(f.name for f in _ICON_DIR.iterdir()
                   if f.is_file() and f.suffix.lower() in _EXTS)
    return {"icons": names}


@router.get("/{name}")
async def get_icon(name: str):
    """返回某个图标文件（防路径穿越）。"""
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    target = (_ICON_DIR / name).resolve()
    root = _ICON_DIR.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "图标不存在")
    return FileResponse(str(target))
