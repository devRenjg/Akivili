"""本地目录浏览：供新建/编辑项目时选择工作文件夹。

只读、只列目录（不列文件、不读内容）。本地工具，后端即用户本机，
但仍做基本防护：路径必须是已存在的目录，异常不外泄细节。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/fs", tags=["fs"])


def _list_drives() -> list[str]:
    """Windows 列出可用盘符；非 Windows 返回根。"""
    import string
    import os
    drives = []
    for letter in string.ascii_uppercase:
        d = f"{letter}:\\"
        if os.path.exists(d):
            drives.append(d)
    return drives or ["/"]


@router.get("/list")
async def list_dir(path: str = ""):
    """列出某目录下的子目录。path 为空时返回盘符/根（作为起点）。

    返回 parent（上一级，可为空）与 dirs（子目录绝对路径列表）。
    """
    # 起点：列盘符
    if not path:
        return {"path": "", "parent": "", "is_root": True,
                "dirs": [{"name": d, "path": d} for d in _list_drives()]}

    p = Path(path)
    try:
        if not p.is_dir():
            raise HTTPException(400, "不是有效目录")
        p = p.resolve()
    except OSError:
        raise HTTPException(400, "无法访问该路径")

    subdirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir() and not child.name.startswith("."):
                    subdirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # 无权限的项跳过
    except PermissionError:
        raise HTTPException(403, "无权限访问该目录")

    # 上一级：到达盘符根则回到盘符列表（parent 置空触发 is_root）
    parent = "" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "is_root": False, "dirs": subdirs}
