# -*- coding: utf-8 -*-
"""数据底座 S3.4 第二批自检 · auth/projects/activity ORM 迁移等价性探针（隔离）。

对 3 个文件迁移后的函数，与「影子手写 SQL」（复刻迁移前原始查询）逐一对照，
验证输出等价：返回 dict 形状/键集合、CRUD 副作用、timeline 合并排序、
updated_at 用 now_expr 后仍是 UTC 同格式。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch2_probe.py
"""
import asyncio
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
    db_path = os.path.join(tmp, "batch2.db")
    cfg = {"db_path": db_path, "memory_dir": os.path.join(tmp, "mem"),
           "providers": [], "default_provider_id": ""}
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)


async def _test_auth():
    import auth
    import models
    from sqlalchemy import text

    # seed_admin：首次播种 + 幂等（二次不重复）
    await auth.seed_admin()
    await auth.seed_admin()
    async with models.get_session_factory()() as s:
        cnt = (await s.execute(text(
            f"SELECT COUNT(*) FROM users WHERE username='{auth.SEED_ADMIN_USERNAME}'"))).scalar_one()
    check("seed_admin 幂等(播种2次得1行)", cnt == 1, f"count={cnt}")

    # _user_from_token：造一个带 token 的用户，模拟 request.cookies
    async with models.get_session_factory()() as s:
        await s.execute(text(
            "UPDATE users SET token='tok-xyz' WHERE username=:u"), {"u": auth.SEED_ADMIN_USERNAME})
        await s.commit()

    class FakeReq:
        def __init__(self, tok):
            self.cookies = {auth.COOKIE_NAME: tok} if tok else {}

    u = await auth._user_from_token(FakeReq("tok-xyz"))
    check("_user_from_token 命中返回 dict", isinstance(u, dict) and set(u) == {"id", "username", "role"},
          f"u={u}")
    check("_user_from_token role=admin", u and u["role"] == "admin", f"u={u}")
    none_u = await auth._user_from_token(FakeReq(""))
    check("_user_from_token 无 token 返回 None", none_u is None)
    bad_u = await auth._user_from_token(FakeReq("nonexistent"))
    check("_user_from_token 错 token 返回 None", bad_u is None)


async def _test_projects():
    import projects
    import models
    from sqlalchemy import text

    # create → 返回 dict，键集合对齐 SELECT *
    d = await projects.create_project("P一", "/p/1", "描述1", "http://git/1")
    expected_keys = {"id", "title", "local_path", "description", "status",
                     "created_at", "updated_at", "git_url"}
    check("create_project 返回 dict 键集合对齐 SELECT*", set(d) == expected_keys, f"keys={set(d)}")
    check("create_project 字段值正确", d["title"] == "P一" and d["git_url"] == "http://git/1", str(d))
    pid = d["id"]

    # get
    g = await projects.get_project(pid)
    check("get_project 返回一致", g == d, f"g={g}")
    check("get_project 不存在返回 None", (await projects.get_project(999999)) is None)

    # list：agent_count 子查询。加 2 个 agent
    async with models.get_session_factory()() as s:
        await s.execute(text(
            f"INSERT INTO project_agents (project_id, slug, name) VALUES ({pid},'a','A')"))
        await s.execute(text(
            f"INSERT INTO project_agents (project_id, slug, name) VALUES ({pid},'b','B')"))
        await s.commit()
    # 再建一个空项目验证排序（updated_at DESC, id DESC）
    d2 = await projects.create_project("P二", "/p/2")
    lst = await projects.list_projects()
    check("list_projects 返回 list[dict] 含 agent_count",
          all("agent_count" in x for x in lst) and len(lst) == 2, f"len={len(lst)}")
    p1 = next(x for x in lst if x["id"] == pid)
    check("list agent_count 子查询正确(=2)", p1["agent_count"] == 2, f"cnt={p1['agent_count']}")
    check("list 排序：后建的 P二 在前", lst[0]["id"] == d2["id"], f"order={[x['id'] for x in lst]}")

    # update：updated_at 用 now_expr，应仍是 UTC 同格式；只改指定列
    before = await projects.get_project(pid)
    upd = await projects.update_project(pid, {"title": "P一改", "bogus": "x", "status": None})
    check("update 只改白名单列(title变、status未动)",
          upd["title"] == "P一改" and upd["status"] == before["status"], str(upd))
    check("update updated_at 仍 UTC 同格式", bool(_FMT.match(upd["updated_at"])), upd["updated_at"])
    # 空 sets → 返回原样
    same = await projects.update_project(pid, {"bogus": "y"})
    check("update 空白名单集不改动", same["title"] == "P一改", str(same))

    # delete
    await projects.delete_project(d2["id"])
    check("delete_project 生效", (await projects.get_project(d2["id"])) is None)


async def _test_activity():
    import activity
    import models
    from sqlalchemy import text

    # 建 project/task/conversation/members
    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (70,'活动项目','/p/a')"))
        await s.execute(text("INSERT INTO conversations (id,project_id) VALUES (7,70)"))
        await s.execute(text("INSERT INTO tasks (id,project_id,title,conversation_id) VALUES (70,70,'活动任务',7)"))
        await s.execute(text("INSERT INTO project_agents (project_id,slug,name,emoji,is_leader) VALUES (70,'dev','开发',:e,1)"),
                        {"e": "🛠"})
        await s.execute(text("INSERT INTO agent_profiles (slug,nickname,avatar) VALUES ('dev','小开','av.png')"))
        await s.commit()

    # log_activity：写入 + created 活动（供 creator_name 回退）
    await activity.log_activity(70, "created", actor_type="user", actor_name="老板")
    await activity.log_activity(70, "commented", actor_type="agent", actor_name="dev",
                                detail={"note": "hi"})
    # 一条消息
    async with models.get_session_factory()() as s:
        await s.execute(text(
            "INSERT INTO messages (conversation_id,role,content,author_slug,author_name) "
            "VALUES (7,'assistant','产出内容','dev','开发')"))
        await s.execute(text(
            "INSERT INTO messages (conversation_id,role,content) VALUES (7,'user','用户发言')"))
        await s.commit()

    tl = await activity.timeline(70)
    check("timeline 返回条数(2活动+2消息=4)", len(tl) == 4, f"len={len(tl)}")
    # created_at 都转过北京时间、格式一致
    check("timeline created_at 全部北京格式", all(_FMT.match(x["created_at"]) for x in tl),
          str([x["created_at"] for x in tl]))
    # agent 活动带 author（昵称匹配）
    act_agent = next(x for x in tl if x.get("kind") == "activity" and x["actor_type"] == "agent")
    check("timeline agent 活动匹配到成员昵称",
          act_agent["author"] and act_agent["author"]["nickname"] == "小开", str(act_agent["author"]))
    # assistant 消息带 author；user 消息回退 creator_name=老板
    msg_user = next(x for x in tl if x.get("kind") == "message" and x["role"] == "user")
    check("timeline user 消息回退创建者名=老板", msg_user["user_name"] == "老板", str(msg_user))
    msg_asst = next(x for x in tl if x.get("kind") == "message" and x["role"] == "assistant")
    check("timeline assistant 消息匹配成员", msg_asst["author"] and msg_asst["author"]["slug"] == "dev",
          str(msg_asst["author"]))
    # detail JSON 解析
    check("timeline detail JSON 解析", act_agent["detail"] == {"note": "hi"}, str(act_agent["detail"]))
    # 空任务 timeline
    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO tasks (id,project_id,title) VALUES (71,70,'空任务')"))
        await s.commit()
    empty_tl = await activity.timeline(71)
    check("空任务 timeline 返回空 list", empty_tl == [], f"got={empty_tl}")


async def _run():
    import database
    await database.init_db()
    await _test_auth()
    await _test_projects()
    await _test_activity()
    import models
    await models.dispose_engine()


def main():
    tmp = tempfile.mkdtemp(prefix="batch2_")
    _isolate(tmp)
    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] auth/projects/activity ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
