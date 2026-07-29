# -*- coding: utf-8 -*-
"""数据底座 S3.4 第五批自检 · agent_cli/project_agents ORM 迁移等价性探针（隔离）。

验证 project_agents 全 CRUD（含 solved_tasks 相关子查询、SELECT * 键集合、rowcount
404、set_leader 唯一负责人、自建 slug 拼接）与 agent_cli 的数据层 helper
（_task_project/_running_run_id/_resolve_member/_display_name）+ set_status 状态机
（子任务 reviewing→done、顶层 done→reviewing 降级）。用 Alembic 建库(001)。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch5_probe.py
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


async def _seed():
    """建 project + template + 一个已导入成员，供 CRUD/子查询测试。"""
    import models
    from sqlalchemy import text
    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (50,'PA项目','/p/pa')"))
        await s.execute(text(
            "INSERT INTO agent_templates (id,slug,name,emoji,color,body) "
            "VALUES (30,'tpl-dev','模版开发','🛠','blue','模版人格')"))
        await s.commit()


async def _test_project_agents_direct():
    """直接调用 project_agents 的路由函数（避开 require_admin 依赖注入）。"""
    import routes.project_agents as pa
    import models

    # import_agent：从模版导入
    r = await pa.import_agent(50, pa.ImportAgentRequest(template_id=30))
    check("import_agent 返回 dict 键集合对齐 SELECT*",
          set(r) >= set(pa._PA_COLS), f"keys={set(r)}")
    check("import_agent 复制模版字段", r["slug"] == "tpl-dev" and r["persona"] == "模版人格", str(r))
    imported_id = r["id"]

    # create_agent：自建，slug=custom-<pid>-<safe>-<aid>
    r2 = await pa.create_agent(50, pa.CreateAgentRequest(name="自建 Agent", emoji="🤝"))
    check("create_agent slug 拼接正确",
          re.match(rf"^custom-50-.+-{r2['id']}$", r2["slug"]) is not None, r2["slug"])

    # list：solved_tasks 子查询（此刻无完成任务 → 0），排序，nickname/avatar 补列
    lst = await pa.list_project_agents(50)
    agents = lst["agents"]
    check("list 返回 2 成员含 solved_tasks/nickname/avatar",
          len(agents) == 2 and all("solved_tasks" in a and "nickname" in a and "avatar" in a for a in agents),
          f"len={len(agents)}")
    check("list solved_tasks 初始为 0", all(a["solved_tasks"] == 0 for a in agents), str([a["solved_tasks"] for a in agents]))

    # 造一个 succeeded run + done task 验证 solved_tasks 会计数
    from sqlalchemy import text
    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO tasks (id,project_id,title,status) VALUES (500,50,'完成任务','done')"))
        await s.execute(text(
            "INSERT INTO task_runs (task_id,agent_slug,status) VALUES (500,'tpl-dev','succeeded')"))
        await s.commit()
    lst2 = await pa.list_project_agents(50)
    dev = next(a for a in lst2["agents"] if a["slug"] == "tpl-dev")
    check("list solved_tasks 计数生效(=1)", dev["solved_tasks"] == 1, f"solved={dev['solved_tasks']}")

    # update_agent：动态 SET + 存在
    r3 = await pa.update_agent(50, imported_id, pa.UpdateAgentRequest(name="改名开发", enabled=0))
    check("update_agent 改名+enabled 生效", r3["name"] == "改名开发" and r3["enabled"] == 0, str(r3))
    # update 不存在 → 404
    from fastapi import HTTPException
    try:
        await pa.update_agent(50, 999999, pa.UpdateAgentRequest(name="x"))
        check("update_agent 不存在 404", False, "未抛异常")
    except HTTPException as e:
        check("update_agent 不存在 404", e.status_code == 404, str(e.status_code))

    # set_leader：唯一负责人（先设 imported，再设 create → imported 应被清 0）
    await pa.set_leader(50, imported_id)
    await pa.set_leader(50, r2["id"])
    lst3 = await pa.list_project_agents(50)
    leaders = [a["id"] for a in lst3["agents"] if a["is_leader"]]
    check("set_leader 唯一负责人", leaders == [r2["id"]], f"leaders={leaders}")

    # remove_agent：删除 + rowcount；再删 404
    await pa.remove_agent(50, imported_id)
    lst4 = await pa.list_project_agents(50)
    check("remove_agent 删除生效", all(a["id"] != imported_id for a in lst4["agents"]),
          f"ids={[a['id'] for a in lst4['agents']]}")
    try:
        await pa.remove_agent(50, imported_id)
        check("remove_agent 再删 404", False, "未抛异常")
    except HTTPException as e:
        check("remove_agent 再删 404", e.status_code == 404, str(e.status_code))


async def _test_agent_cli_helpers():
    """agent_cli 数据层 helper + set_status 状态机（不触发 collab 副作用的部分）。"""
    import routes.agent_cli as cli
    import models
    from sqlalchemy import text

    # 造成员 + 昵称
    async with models.get_session_factory()() as s:
        await s.execute(text(
            "INSERT INTO project_agents (project_id,slug,name) VALUES (50,'dev-x','开发X')"))
        await s.execute(text(
            "INSERT INTO agent_profiles (slug,nickname) VALUES ('dev-x','小X')"))
        # 顶层任务 + 子任务
        await s.execute(text("INSERT INTO tasks (id,project_id,title,status) VALUES (600,50,'顶层',' in_progress')"))
        await s.execute(text(
            "INSERT INTO tasks (id,project_id,title,status,parent_task_id) VALUES (601,50,'子任务','in_progress',600)"))
        # 一个 running run 供 _running_run_id
        await s.execute(text(
            "INSERT INTO task_runs (id,task_id,agent_slug,status) VALUES (700,600,'dev-x','running')"))
        await s.commit()

    # _task_project
    tp = await cli._task_project(600)
    check("_task_project 返回 {id,project_id,title}",
          tp and tp["project_id"] == 50 and tp["title"] == "顶层", str(tp))
    check("_task_project 不存在返回 None", (await cli._task_project(999999)) is None)

    # _running_run_id
    rid = await cli._running_run_id(600, "dev-x")
    check("_running_run_id 命中 running run", rid == 700, f"rid={rid}")
    check("_running_run_id 无匹配返回 None", (await cli._running_run_id(600, "nobody")) is None)

    # _resolve_member：slug/名字/昵称 都能解析
    check("_resolve_member 按 slug", (await cli._resolve_member(50, "dev-x")) == "dev-x")
    check("_resolve_member 按名字", (await cli._resolve_member(50, "开发X")) == "dev-x")
    check("_resolve_member 按昵称", (await cli._resolve_member(50, "小X")) == "dev-x")
    check("_resolve_member 解析不到返回空", (await cli._resolve_member(50, "查无此人")) == "")

    # _display_name：有昵称→「昵称（角色名）」
    dn = await cli._display_name(50, "dev-x")
    check("_display_name 昵称（角色名）", dn == "小X（开发X）", dn)
    check("_display_name 查不到回退 slug", (await cli._display_name(50, "ghost")) == "ghost")

    # set_status：顶层 done → 降级 reviewing
    r = await cli.set_status(cli.StatusReq(task_id=600, agent_slug="dev-x", status="done"))
    check("set_status 顶层 done→reviewing 降级", r["status"] == "reviewing" and "note" in r, str(r))
    # 子任务 reviewing → 归一 done（会触发 on_execution_complete，此处只验状态返回）
    r2 = await cli.set_status(cli.StatusReq(task_id=601, agent_slug="dev-x", status="reviewing"))
    check("set_status 子任务 reviewing→done 归一", r2["status"] == "done", str(r2))
    # 非法状态 400
    from fastapi import HTTPException
    try:
        await cli.set_status(cli.StatusReq(task_id=600, agent_slug="dev-x", status="bogus"))
        check("set_status 非法状态 400", False, "未抛异常")
    except HTTPException as e:
        check("set_status 非法状态 400", e.status_code == 400, str(e.status_code))


async def _run():
    import models
    from db_migrate import run_migrations
    run_migrations()   # Alembic 001 建库（生产同路径）
    await _seed()
    await _test_project_agents_direct()
    await _test_agent_cli_helpers()
    await models.dispose_engine()


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch5_")
    _isolate(tmp)
    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] agent_cli/project_agents ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
