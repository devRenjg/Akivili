# -*- coding: utf-8 -*-
"""数据底座 S3.4 第六批自检 · routes.agents/reflect ORM 迁移等价性探针（隔离）。

验证 routes.agents 全部端点（list_templates 双相关子查询+过滤+排序、divisions
GROUP BY、get_template SELECT*+skills、template_projects、division 增改删、
create_talent 的 INSERT+upsert+INSERT OR IGNORE+取 id）+ reflect._participants/
_task_context 的动态 IN 查询与 dict 契约。用 Alembic 建库。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch6_probe.py
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


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _isolate(tmp):
    cfg = {
        "db_path": os.path.join(tmp, "batch6.db"),
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


async def _seed():
    import models
    from sqlalchemy import text
    async with models.get_session_factory()() as s:
        # 2 模版(不同 division) + 1 skill + 项目/成员/完成任务(验证子查询计数)
        await s.execute(text("INSERT INTO agent_templates (id,slug,name,division,description) "
                             "VALUES (40,'t-a','模版A','工程','做后端ABC')"))
        await s.execute(text("INSERT INTO agent_templates (id,slug,name,division,description) "
                             "VALUES (41,'t-b','模版B','设计','搞设计')"))
        await s.execute(text("INSERT INTO skills (slug,name,description) VALUES ('sk','技能S','描述S')"))
        await s.execute(text("INSERT INTO agent_skills (agent_slug,skill_slug) VALUES ('t-a','sk')"))
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (60,'P1','/p1')"))
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (61,'P2','/p2')"))
        await s.execute(text("INSERT INTO project_agents (project_id,slug,name) VALUES (60,'t-a','A')"))
        # t-a 完成一个 done 任务 + succeeded run → solved_tasks=1, project_count=1
        await s.execute(text("INSERT INTO tasks (id,project_id,title,status) VALUES (600,60,'完成','done')"))
        await s.execute(text("INSERT INTO task_runs (task_id,agent_slug,status) VALUES (600,'t-a','succeeded')"))
        await s.commit()


async def _test_agents():
    import routes.agents as ag
    import models
    from sqlalchemy import text

    # list_templates：双子查询 + 排序（t-a 有项目/完成任务，应排前）
    r = await ag.list_templates()
    tpls = r["templates"]
    check("list_templates 返回 2 模版", r["count"] == 2, str(r["count"]))
    ta = next(t for t in tpls if t["slug"] == "t-a")
    check("list project_count 子查询=1", ta["project_count"] == 1, str(ta))
    check("list solved_tasks 子查询=1", ta["solved_tasks"] == 1, str(ta))
    check("list 排序 t-a 在前(项目数高)", tpls[0]["slug"] == "t-a", [t["slug"] for t in tpls])
    check("list 键含 origin/nickname/avatar",
          {"origin", "nickname", "avatar"} <= set(ta), str(set(ta)))
    # 过滤：division
    r2 = await ag.list_templates(division="设计")
    check("list division 过滤", r2["count"] == 1 and r2["templates"][0]["slug"] == "t-b", str(r2))
    # 过滤：q（描述含 ABC）
    r3 = await ag.list_templates(q="ABC")
    check("list q 过滤命中 t-a", r3["count"] == 1 and r3["templates"][0]["slug"] == "t-a", str(r3))

    # divisions GROUP BY
    r = await ag.list_divisions()
    divs = {d["division"]: d["n"] for d in r["divisions"]}
    check("divisions GROUP BY 计数", divs.get("工程") == 1 and divs.get("设计") == 1, str(divs))

    # get_template：SELECT* 键集合 + skills join
    r = await ag.get_template(40)
    check("get_template SELECT* 键含 body/tags/origin",
          {"body", "tags", "origin", "slug"} <= set(r), str(set(r)))
    check("get_template skills join(1条)",
          len(r["skills"]) == 1 and r["skills"][0]["slug"] == "sk", str(r["skills"]))
    from fastapi import HTTPException
    try:
        await ag.get_template(999999); check("get_template 不存在 404", False)
    except HTTPException as e:
        check("get_template 不存在 404", e.status_code == 404, str(e.status_code))

    # template_projects：已加入 P1、可加入 P2
    r = await ag.template_projects(40)
    joined_ids = {p["id"] for p in r["joined"]}
    joinable_ids = {p["id"] for p in r["joinable"]}
    check("template_projects joined=P1", joined_ids == {60}, str(joined_ids))
    check("template_projects joinable=P2", joinable_ids == {61}, str(joinable_ids))

    # set_talent_division
    r = await ag.set_talent_division(41, ag.SetDivisionRequest(division="产品"))
    check("set_talent_division 生效", r["division"] == "产品", str(r))
    # rename_division：工程→研发
    r = await ag.rename_division(ag.RenameDivisionRequest(old_name="工程", new_name="研发"))
    check("rename_division affected=1", r["affected"] == 1, str(r))
    # delete_division：产品→''
    r = await ag.delete_division("产品")
    check("delete_division affected=1", r["affected"] == 1, str(r))

    # create_talent：INSERT template(manual) + upsert profile + INSERT OR IGNORE skills + 取 id
    r = await ag.create_talent(ag.CreateTalentRequest(
        name="手动人才", division="研发", nickname="手动昵称", provider_id="prov1",
        skill_slugs=["sk", "sk", "sk2"]))
    check("create_talent 返回 id/slug", r["id"] and r["slug"].startswith("manual-"), str(r))
    new_slug = r["slug"]
    async with models.get_session_factory()() as s:
        # origin=manual
        o = (await s.execute(text("SELECT origin FROM agent_templates WHERE slug=:x"),
                             {"x": new_slug})).scalar_one()
        check("create_talent origin=manual", o == "manual", o)
        # profile 写入
        prof = (await s.execute(text("SELECT nickname,provider_id FROM agent_profiles WHERE slug=:x"),
                                {"x": new_slug})).first()
        check("create_talent profile 写入", prof.nickname == "手动昵称" and prof.provider_id == "prov1", str(prof))
        # skills：INSERT OR IGNORE 去重(sk,sk2 → 2 条)
        skcnt = (await s.execute(text("SELECT COUNT(*) FROM agent_skills WHERE agent_slug=:x"),
                                 {"x": new_slug})).scalar_one()
        check("create_talent skills 绑定(去重2条)", skcnt == 2, f"cnt={skcnt}")
    # 昵称重复 409
    try:
        await ag.create_talent(ag.CreateTalentRequest(name="另一个", nickname="手动昵称"))
        check("create_talent 昵称重复 409", False)
    except HTTPException as e:
        check("create_talent 昵称重复 409", e.status_code == 409, str(e.status_code))


async def _test_reflect():
    import reflect
    import models
    from sqlalchemy import text

    # 造：项目+成员(带 provider_id，否则 _participants 过滤掉)+任务+run+消息
    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (70,'反思项目','/p')"))
        await s.execute(text("INSERT INTO project_agents (project_id,slug,name,persona) "
                             "VALUES (70,'dev','开发','人格D')"))
        await s.execute(text("INSERT INTO agent_profiles (slug,provider_id,nickname) "
                             "VALUES ('dev','prov-r','小D')"))
        await s.execute(text("INSERT INTO conversations (id,project_id) VALUES (77,70)"))
        await s.execute(text("INSERT INTO tasks (id,project_id,title,description,conversation_id,status) "
                             "VALUES (700,70,'反思任务','任务描述X',77,'done')"))
        await s.execute(text("INSERT INTO task_runs (task_id,agent_slug,status) VALUES (700,'dev','succeeded')"))
        await s.execute(text("INSERT INTO messages (conversation_id,role,content,author_slug) "
                             "VALUES (77,'assistant','我的产出内容','dev')"))
        await s.commit()

    # _participants：应返回 dev（有 run + provider_id）
    parts = await reflect._participants(700)
    check("_participants 返回 1 成员", len(parts) == 1, str(parts))
    if parts:
        m = parts[0]
        check("_participants dict 键契约(slug/name/persona/provider_id/nickname)",
              set(m) == {"slug", "name", "persona", "provider_id", "nickname"}, str(set(m)))
        check("_participants 值正确", m["slug"] == "dev" and m["provider_id"] == "prov-r", str(m))

    # _task_context：含任务标题/描述/产出
    ctx = await reflect._task_context(700, "dev")
    check("_task_context 含任务标题", "反思任务" in ctx, ctx[:50])
    check("_task_context 含描述", "任务描述X" in ctx, ctx[:80])
    check("_task_context 含产出", "我的产出内容" in ctx, ctx[-50:])
    # 空任务 context
    empty = await reflect._task_context(999999, "dev")
    check("_task_context 不存在任务返回空", empty == "", repr(empty))


async def _run():
    import models
    from db_migrate import run_migrations
    run_migrations()
    await _seed()
    await _test_agents()
    await _test_reflect()
    await models.dispose_engine()


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch6_")
    _isolate(tmp)
    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] routes.agents/reflect ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
