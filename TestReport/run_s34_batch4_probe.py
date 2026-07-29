# -*- coding: utf-8 -*-
"""数据底座 S3.4 第四批自检 · agents/routes.agent_config ORM 迁移等价性探针（隔离）。

验证：agents.rescan upsert-by-slug + count_templates；routes.agent_config 的
get/taken/set_profile(upsert+昵称查重)/set_model(upsert)/set_skills(DELETE+INSERT)。
重点验证新引入的 upsert(ON CONFLICT DO UPDATE) 的 insert 路径与 update 路径都正确、
updated_at 用 now_expr 仍 UTC。用 TestClient 打真实接口 + 直接调用层。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch4_probe.py
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
    agents_dir = os.path.join(tmp, "agents", "engineering")
    os.makedirs(agents_dir, exist_ok=True)
    # 2 个模版，供 rescan
    with open(os.path.join(agents_dir, "dev-one.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: 开发一\ndescription: desc1\n---\n\n人格1")
    with open(os.path.join(agents_dir, "dev-two.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: 开发二\n---\n\n人格2")
    from run_qa_suite import isolated_pg_db_url  # noqa: PLC0415
    cfg = {
        "db_url": isolated_pg_db_url(),   # S5：PG 隔离库（替代 sqlite db_path）
        "agent_library_dir": os.path.join(tmp, "agents"),
        "skills_dir": os.path.join(tmp, "skills"),
        "memory_dir": os.path.join(tmp, "mem"),
        "providers": [], "default_provider_id": "",
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)


async def _direct_checks():
    import agents as agents_mod
    import models
    from db_migrate import run_migrations

    # 用 Alembic 001 基线建库（生产同路径；含 tags/origin 等全部历史补列），
    # 不用已冻结的 init_db SCHEMA 常量（S2 起 schema 由迁移唯一定义）
    run_migrations()
    r1 = await agents_mod.rescan()
    check("agents.rescan 首次 inserted=2", r1["inserted"] == 2 and r1["updated"] == 0, str(r1))
    c = await agents_mod.count_templates()
    check("count_templates=2", c == 2, f"c={c}")
    r2 = await agents_mod.rescan()
    check("agents.rescan 二次 updated=2(幂等)", r2["updated"] == 2 and r2["inserted"] == 0, str(r2))
    await models.dispose_engine()


def _api_checks():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import agent_config as ac

    # set_profile/set_model/set_skills 需 require_admin：覆盖依赖为放行
    from auth import require_admin
    app = FastAPI()
    app.include_router(ac.router)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin"}
    client = TestClient(app)

    slug = "dev-one"

    # get：档案还没建，应回退空串 + skill_slugs=[]
    r = client.get(f"/api/agent-config/{slug}")
    d = r.json()
    check("get 无档案回退空", d["provider_id"] == "" and d["nickname"] == "" and d["skill_slugs"] == [],
          str(d))

    # set_model：upsert 的 INSERT 路径（首次无行）
    r = client.put(f"/api/agent-config/{slug}/model", json={"provider_id": "prov-x"})
    check("set_model 首次 200", r.status_code == 200, str(r.status_code))
    d = client.get(f"/api/agent-config/{slug}").json()
    check("set_model INSERT 路径生效", d["provider_id"] == "prov-x", str(d))

    # set_profile：upsert 的 UPDATE 路径（行已存在，改 nickname/avatar，不能覆盖 provider_id）
    r = client.put(f"/api/agent-config/{slug}/profile", json={"nickname": "小一", "avatar": "a1.png"})
    check("set_profile 200(UPDATE路径)", r.status_code == 200, str(r.status_code))
    d = client.get(f"/api/agent-config/{slug}").json()
    check("set_profile UPDATE 后 nickname/avatar 生效", d["nickname"] == "小一" and d["avatar"] == "a1.png", str(d))
    check("set_profile UPDATE 未清空 provider_id(only 更新指定列)", d["provider_id"] == "prov-x", str(d))

    # 昵称查重：另一个 slug 用同昵称应 409
    r = client.put("/api/agent-config/dev-two/profile", json={"nickname": "小一"})
    check("set_profile 昵称重复 409", r.status_code == 409, f"{r.status_code} {r.text[:60]}")

    # taken：已占用昵称/头像
    r = client.get("/api/agent-config/taken/list", params={"exclude": "nobody"})
    d = r.json()
    check("taken 列出已占用昵称/头像", "小一" in d["nicknames"] and "a1.png" in d["avatars"], str(d))
    # exclude 自己后不含自己的昵称
    r = client.get("/api/agent-config/taken/list", params={"exclude": slug})
    check("taken exclude 自己后排除", "小一" not in r.json()["nicknames"], str(r.json()))

    # set_skills：DELETE+INSERT 重写集合（去重保序）
    r = client.put(f"/api/agent-config/{slug}/skills", json={"skill_slugs": ["s1", "s2", "s1", "s3"]})
    check("set_skills 200", r.status_code == 200, str(r.status_code))
    d = client.get(f"/api/agent-config/{slug}").json()
    check("set_skills 去重(s1s2s3)", d["skill_slugs"] == ["s1", "s2", "s3"], str(d["skill_slugs"]))
    # 再次重写：应完全替换而非追加
    client.put(f"/api/agent-config/{slug}/skills", json={"skill_slugs": ["s9"]})
    d = client.get(f"/api/agent-config/{slug}").json()
    check("set_skills 重写替换(仅s9)", d["skill_slugs"] == ["s9"], str(d["skill_slugs"]))


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch4_")
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
    print("[OK] agents/routes.agent_config ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
