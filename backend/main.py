"""Akivili 后端入口。

- CORS：显式白名单（前端 3100），不使用 wildcard+credentials。
- 启动时建库。
- host 默认 127.0.0.1（本地工具，默认不对外暴露）；reload 由 JIANAGENCY_RELOAD 环境变量控制。
"""
import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import load_settings
from database import init_db
from routes import settings as settings_routes
from routes import agents as agents_routes
from routes import projects as projects_routes
from routes import project_agents as project_agents_routes
from routes import memory as memory_routes
from routes import fs as fs_routes
from routes import skills as skills_routes
from routes import agent_config as agent_config_routes
from routes import tasks as tasks_routes
from routes import runs as runs_routes
from routes import auth as auth_routes
from routes import icons as icons_routes
from routes import agent_cli as agent_cli_routes
import agents as agents_mod
import memory as memory_mod
import skills as skills_mod
import auth as auth_mod
import collab as collab_mod

app = FastAPI(title="Akivili", version="0.4.0")

# 显式白名单：避免 wildcard + credentials 的浏览器拒绝陷阱。
# 内网访问地址通过环境变量 AKIVILI_EXTRA_ORIGINS 追加（逗号分隔），例：
#   AKIVILI_EXTRA_ORIGINS=http://<your-lan-ip>:3100
ALLOWED_ORIGINS = [
    "http://localhost:3100",
    "http://127.0.0.1:3100",
]
_extra = os.environ.get("AKIVILI_EXTRA_ORIGINS", "").strip()
if _extra:
    ALLOWED_ORIGINS += [o.strip() for o in _extra.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _static_cache_headers(request, call_next):
    """前端静态资源缓存策略，避免 build 后旧 index.html 引用已删除的 hash chunk 导致 404 卡死：
    - /assets/* 文件名自带内容 hash，改动即换名 → 可长期强缓存（immutable）。
    - index.html（HTML 入口）→ no-cache，浏览器每次校验，build 后自动拿到最新入口。
    """
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

app.include_router(settings_routes.router)
app.include_router(agents_routes.router)
app.include_router(projects_routes.router)
app.include_router(project_agents_routes.router)
app.include_router(memory_routes.router)
app.include_router(fs_routes.router)
app.include_router(skills_routes.router)
app.include_router(agent_config_routes.router)
app.include_router(tasks_routes.router)
app.include_router(runs_routes.router)
app.include_router(auth_routes.router)
app.include_router(icons_routes.router)
app.include_router(agent_cli_routes.router)


@app.on_event("startup")
async def _startup():
    await init_db()
    await auth_mod.seed_admin()
    memory_mod.ensure_memory_dir()
    skills_mod.ensure_skills_dir()
    # 库为空则自动扫描一次，让用户开箱即见 Agent
    if await agents_mod.count_templates() == 0:
        await agents_mod.rescan()
    if await skills_mod.count_skills() == 0:
        await skills_mod.rescan()
    collab_mod.start_loop()   # 启动多 Agent 协同后台循环


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "jianagency"}


# 托管前端构建产物（生产/内网部署：前端 API 同源，一个端口 8100，无需 vite）
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_DIST = _Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # 未知 /api/* 返回 404 JSON，不落入 SPA 兜底（避免 API 客户端误判成功）
        if full_path.startswith("api/") or full_path == "api":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # 非 API 路径：静态资源命中则返回文件，否则回 index.html（SPA 路由）
        f = _DIST / full_path
        if full_path and f.is_file():
            return FileResponse(str(f))
        return FileResponse(str(_DIST / "index.html"))


if __name__ == "__main__":
    s = load_settings()
    # 默认开启热加载：改后端代码即自动重启，免手动重启。
    # 生产/需要关闭时设 JIANAGENCY_RELOAD=0（或 false/no）。
    reload = os.environ.get("JIANAGENCY_RELOAD", "1").lower() not in ("0", "false", "no")
    uvicorn.run("main:app", host=s.host, port=s.port, reload=reload)
