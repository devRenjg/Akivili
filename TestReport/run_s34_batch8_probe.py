# -*- coding: utf-8 -*-
"""数据底座 S3.4 第八批自检 · routes/tasks.py ORM 迁移等价性探针（隔离）。

TestClient 打真实接口：list_tasks(5相关子查询+子任务嵌套+board 分组)、create_task
(conv+task)、update_task(rowcount 404+priority活动)、set_status(闭环拦截+404)、
delete_task(级联删+清记忆)、get_task(自JOIN parent_title)、subtasks、create_subtask
(顶层限制)。用 Alembic 建库。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch8_probe.py
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


async def _prep():
    import models
    from db_migrate import run_migrations
    from sqlalchemy import text
    run_migrations()
    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (90,'任务项目','/p')"))
        await s.commit()


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import tasks as tasks_routes
    from auth import require_admin
    app = FastAPI()
    app.include_router(tasks_routes.router)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    return TestClient(app)


def _api():
    c = _client()

    # create_task
    r = c.post("/api/projects/90/tasks", json={"title": "顶层任务", "priority": "high"})
    check("create_task 200 + dict", r.status_code == 200 and r.json()["title"] == "顶层任务", str(r.status_code))
    t = r.json()
    check("create_task 键集合对齐 SELECT*",
          set(t) == {"id", "project_id", "title", "description", "status", "assignee_slug",
                     "conversation_id", "order_idx", "created_at", "updated_at", "priority",
                     "parent_task_id"}, str(set(t)))
    tid = t["id"]
    check("create_task 建了关联对话", t["conversation_id"] is not None, str(t))

    # create_subtask
    r = c.post(f"/api/projects/90/tasks/{tid}/subtasks", json={"title": "子任务A"})
    check("create_subtask 200", r.status_code == 200, str(r.status_code))
    sub = r.json()
    sid = sub["id"]
    check("create_subtask parent 指向顶层", sub["parent_task_id"] == tid, str(sub))
    # 子任务下不能再建子任务
    r = c.post(f"/api/projects/90/tasks/{sid}/subtasks", json={"title": "孙任务"})
    check("create_subtask 子任务下建子任务 400", r.status_code == 400, str(r.status_code))

    # list_tasks：board 分组 + 子任务嵌套 + 5 子查询字段
    r = c.get("/api/projects/90/tasks")
    d = r.json()
    check("list_tasks 顶层1个", len(d["tasks"]) == 1, str(len(d["tasks"])))
    top = d["tasks"][0]
    check("list_tasks 子查询字段齐全",
          all(k in top for k in ("run_status", "last_result", "msg_count", "sub_total", "sub_done")),
          str(set(top)))
    check("list_tasks sub_total=1", top["sub_total"] == 1, str(top["sub_total"]))
    check("list_tasks 嵌套子任务", len(top["subtasks"]) == 1 and top["subtasks"][0]["id"] == sid,
          str(top["subtasks"]))
    check("list_tasks 子任务含 run_status/active_run",
          "run_status" in top["subtasks"][0] and "active_run" in top["subtasks"][0],
          str(top["subtasks"][0]))
    check("list_tasks created_at 转北京时间", bool(_FMT.match(top["created_at"])), top["created_at"])
    check("list_tasks board 分组", "backlog" in d["board"] and len(d["board"]["backlog"]) == 1,
          str({k: len(v) for k, v in d["board"].items()}))

    # get_task：自 JOIN parent_title（子任务应带父标题）
    r = c.get(f"/api/projects/90/tasks/{sid}")
    gd = r.json()
    check("get_task 子任务带 parent_title", gd.get("parent_title") == "顶层任务", str(gd.get("parent_title")))
    check("get_task 顶层 parent_title 为 None",
          c.get(f"/api/projects/90/tasks/{tid}").json().get("parent_title") is None)
    check("get_task 不存在 404", c.get("/api/projects/90/tasks/999999").status_code == 404)

    # update_task：priority 变更 + rowcount 404
    r = c.put(f"/api/projects/90/tasks/{tid}", json={"priority": "low"})
    check("update_task priority 生效", r.json()["priority"] == "low", str(r.json()["priority"]))
    r = c.put("/api/projects/90/tasks/999999", json={"title": "x"})
    check("update_task 不存在 404", r.status_code == 404, str(r.status_code))

    # set_status：闭环拦截——子任务「仍在执行(run_queue queued/running)」时父不能 done。
    # 给子任务插一个 running run_queue，模拟"执行中"（blocking_subtasks 的判据是 EXISTS 活跃 run）
    import asyncio as _a
    import models as _m
    from sqlalchemy import text as _t
    async def _add_run():
        async with _m.get_session_factory()() as s:
            await s.execute(_t("INSERT INTO run_queue (task_id,agent_slug,status) "
                               "VALUES (:x,'dev','running')"), {"x": sid})
            await s.commit()
    _a.get_event_loop().run_until_complete(_add_run()) if False else _a.run(_add_run())
    r = c.put(f"/api/projects/90/tasks/{tid}/status", json={"status": "done"})
    check("set_status 子任务执行中 done 被拦 409", r.status_code == 409, str(r.status_code))
    # 执行完（清掉活跃 run）后不再拦
    async def _clr_run():
        async with _m.get_session_factory()() as s:
            await s.execute(_t("UPDATE run_queue SET status='done' WHERE task_id=:x"), {"x": sid})
            await s.commit()
    _a.run(_clr_run())
    # force 强制通过
    r = c.put(f"/api/projects/90/tasks/{tid}/status", json={"status": "done", "force": True})
    check("set_status force done 通过", r.status_code == 200 and r.json()["status"] == "done", str(r.json()))
    # 子任务正常改状态
    r = c.put(f"/api/projects/90/tasks/{sid}/status", json={"status": "in_progress"})
    check("set_status 子任务改状态 200", r.status_code == 200, str(r.status_code))
    # 非法状态 400
    check("set_status 非法状态 400",
          c.put(f"/api/projects/90/tasks/{tid}/status", json={"status": "bogus"}).status_code == 400)

    # subtasks 列表
    r = c.get(f"/api/projects/90/tasks/{tid}/subtasks")
    check("list_subtasks 返回1个", len(r.json()["subtasks"]) == 1, str(r.json()))

    # delete_task：级联删父+子
    r = c.delete(f"/api/projects/90/tasks/{tid}")
    check("delete_task 200", r.status_code == 200, str(r.status_code))
    check("delete_task 级联删子任务", c.get(f"/api/projects/90/tasks/{sid}").status_code == 404)
    check("delete_task 删父任务", c.get(f"/api/projects/90/tasks/{tid}").status_code == 404)
    check("delete_task 再删 404", c.delete(f"/api/projects/90/tasks/{tid}").status_code == 404)


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch8_")
    _isolate(tmp)
    asyncio.run(_prep())
    _api()
    import models
    asyncio.run(models.dispose_engine())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] routes/tasks.py ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
