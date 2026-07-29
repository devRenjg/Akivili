# -*- coding: utf-8 -*-
"""数据底座 S3.4 第三批自检 · skills/routes.skills/routes.auth ORM 迁移等价性探针（隔离）。

验证迁移后行为与迁移前手写 SQL 等价：skills.rescan 的 upsert-by-slug、count；
routes.skills 的 list(搜索+download_count 子查询+排序)/get/download(计数+downloadable
拦截)/download_logs；routes.auth 的 login(校验+token+last_seen)/logout/me。
用 FastAPI TestClient 打真实接口。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch3_probe.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

PASS, FAIL = [], []
_FMT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _isolate(tmp):
    skills_dir = os.path.join(tmp, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    # 造 3 个 skill 文件：2 单文件(1 可下载 1 禁下载) + 用于搜索过滤
    with open(os.path.join(skills_dir, "alpha-skill.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: Alpha技能\ndescription: 关键词ABC\n---\n\n正文A")
    with open(os.path.join(skills_dir, "beta-skill.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: Beta技能\ndescription: 无关\ndownloadable: false\n---\n\n正文B")
    from run_qa_suite import isolated_pg_db_url  # noqa: PLC0415
    cfg = {
        "db_url": isolated_pg_db_url(),   # S5：PG 隔离库（替代 sqlite db_path）
        "skills_dir": skills_dir,
        "memory_dir": os.path.join(tmp, "mem"),
        "agent_library_dir": os.path.join(tmp, "agents"),
        "providers": [], "default_provider_id": "",
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)
    return skills_dir


async def _direct_checks():
    """直接调用层：skills.rescan / count_skills。"""
    import skills as skills_mod
    import auth as auth_mod
    import models
    from db_migrate import run_migrations

    run_migrations()   # 建库唯一走 Alembic（S3.6 已下线 init_db 建表）
    await auth_mod.seed_admin()  # 播种管理员(admin/changeme)，供 login 接口测试

    # rescan：首次全 insert
    r1 = await skills_mod.rescan()
    check("rescan 首次 inserted=2", r1["inserted"] == 2 and r1["updated"] == 0, str(r1))
    cnt = await skills_mod.count_skills()
    check("count_skills=2", cnt == 2, f"cnt={cnt}")
    # rescan 二次：全 update（幂等，不重复插）
    r2 = await skills_mod.rescan()
    check("rescan 二次 updated=2 inserted=0", r2["updated"] == 2 and r2["inserted"] == 0, str(r2))
    cnt2 = await skills_mod.count_skills()
    check("count_skills 仍=2(幂等)", cnt2 == 2, f"cnt={cnt2}")
    await models.dispose_engine()


def _api_checks():
    """接口层：TestClient 打 routes.skills / routes.auth。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import skills as skills_routes
    from routes import auth as auth_routes
    import auth as auth_mod

    app = FastAPI()
    app.include_router(skills_routes.router)
    app.include_router(auth_routes.router)
    client = TestClient(app)

    # —— skills 列表：download_count 子查询、搜索过滤、排序 ——
    r = client.get("/api/skills")
    check("GET /api/skills 200", r.status_code == 200, str(r.status_code))
    data = r.json()
    check("list 返回 2 个 skill 带 download_count",
          data["count"] == 2 and all("download_count" in s for s in data["skills"]),
          str(data))
    # 搜索过滤：q=ABC 只命中 alpha
    r = client.get("/api/skills", params={"q": "ABC"})
    d = r.json()
    check("list 搜索 q=ABC 命中1条(Alpha)",
          d["count"] == 1 and d["skills"][0]["name"] == "Alpha技能", str(d))

    alpha = next(s for s in data["skills"] if s["name"] == "Alpha技能")
    beta = next(s for s in data["skills"] if s["name"] == "Beta技能")

    # —— get 详情：SELECT * → dict 键集合 ——
    r = client.get(f"/api/skills/{alpha['id']}")
    dd = r.json()
    check("GET /api/skills/{id} 键集合对齐 SELECT*",
          set(dd) == {"id", "slug", "name", "description", "source_path",
                      "body", "imported_at", "is_dir", "downloadable"}, str(set(dd)))

    # —— download：可下载的 alpha 成功 + 计数+1；禁下载的 beta 403 ——
    r = client.get(f"/api/skills/{alpha['id']}/download")
    check("download 可下载 skill 200", r.status_code == 200, str(r.status_code))
    r = client.get(f"/api/skills/{beta['id']}/download")
    check("download 禁下载 skill 403", r.status_code == 403, str(r.status_code))
    # 列表里 alpha 的 download_count 现在应=1
    r = client.get("/api/skills")
    a2 = next(s for s in r.json()["skills"] if s["name"] == "Alpha技能")
    check("download 后 download_count=1", a2["download_count"] == 1, str(a2))

    # —— login/me/logout（用播种管理员：AKIVILI_ADMIN_USER/PASSWORD 默认 admin/changeme）——
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    check("login 正确凭据 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    lj = r.json()
    check("login 返回 user{id,username,role}",
          set(lj["user"]) == {"id", "username", "role"} and lj["user"]["role"] == "admin", str(lj))
    # cookie 已种，me 应返回该用户
    r = client.get("/api/auth/me")
    check("me 带 cookie 返回登录用户", r.json()["user"] and r.json()["user"]["username"] == "admin",
          str(r.json()))
    # 错误密码 401
    r2 = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    check("login 错误密码 401", r2.status_code == 401, str(r2.status_code))
    # logout 后 me 返回 None
    client.post("/api/auth/logout")
    r = client.get("/api/auth/me")
    check("logout 后 me 返回 None", r.json()["user"] is None, str(r.json()))

    # —— download_logs（需管理员，先重新登录）——
    client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    r = client.get(f"/api/skills/{alpha['id']}/downloads")
    check("download_logs 200(管理员)", r.status_code == 200, str(r.status_code))
    lj = r.json()
    check("download_logs total=1 且 logs 有 ip/ts",
          lj["total"] == 1 and len(lj["logs"]) == 1 and set(lj["logs"][0]) == {"ip", "ts"},
          str(lj))


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch3_")
    _isolate(tmp)
    asyncio.run(_direct_checks())
    _api_checks()

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] skills/routes.skills/routes.auth ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
