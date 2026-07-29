# -*- coding: utf-8 -*-
"""数据底座 S3.4 第九批自检 · routes/runs.py ORM 迁移等价性探针（隔离）。

TestClient 打读/指标接口 + 直接调数据层函数。重点验证新方言模式：
datetime('now',?) 相对时间窗口(rate_limit_metrics/agents_overview)、julianday SUM
时长聚合、get_lineage 的 LEFT JOIN task_run + run_events 分组、_run_summary/
transcript/lineage 的多表拼接。用 Alembic 建库。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch9_probe.py
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
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (95,'运行项目','/p')"))
        await s.execute(text("INSERT INTO project_agents (project_id,slug,name,enabled,is_leader) "
                             "VALUES (95,'dev','开发',1,1)"))
        await s.execute(text("INSERT INTO project_agents (project_id,slug,name,enabled) "
                             "VALUES (95,'idle-dev','闲置',1)"))
        await s.execute(text("INSERT INTO agent_profiles (slug,provider_id,nickname) "
                             "VALUES ('dev','pv','小开')"))
        await s.execute(text("INSERT INTO conversations (id,project_id) VALUES (95,95)"))
        await s.execute(text("INSERT INTO tasks (id,project_id,title,conversation_id,assignee_slug) "
                             "VALUES (950,95,'运行任务',95,'dev')"))
        await s.execute(text("INSERT INTO messages (conversation_id,role,content) "
                             "VALUES (95,'user','请开始')"))
        await s.execute(text("INSERT INTO messages (conversation_id,role,content) "
                             "VALUES (95,'assistant','好的产出')"))
        # 一个已结束 run（5秒时长）+ 一个 running run
        await s.execute(text(
            "INSERT INTO task_runs (id,task_id,agent_slug,status,provider_id,started_at,ended_at,fail_reason) "
            "VALUES (9500,950,'dev','succeeded','pv',to_char((now() AT TIME ZONE 'UTC') + interval '-5 seconds', 'YYYY-MM-DD HH24:MI:SS'),to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),'')"))
        await s.execute(text(
            "INSERT INTO task_runs (id,task_id,agent_slug,status,started_at) "
            "VALUES (9501,950,'dev','running',to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"))
        # run_logs：给 9500 一条 stdout + 一条 tool
        await s.execute(text(
            "INSERT INTO run_logs (run_id,channel,content) VALUES (9500,'stdout','开场发言内容')"))
        await s.execute(text(
            "INSERT INTO run_logs (run_id,channel,content,tool,tool_input) "
            "VALUES (9500,'tool','','Bash','{\"command\":\"ls -la\"}')"))
        # 一个失败 run（限流）
        await s.execute(text(
            "INSERT INTO task_runs (id,task_id,agent_slug,status,started_at,ended_at,fail_reason) "
            "VALUES (9502,950,'dev','failed',to_char((now() AT TIME ZONE 'UTC') + interval '-2 hours', 'YYYY-MM-DD HH24:MI:SS'),to_char((now() AT TIME ZONE 'UTC') + interval '-2 hours', 'YYYY-MM-DD HH24:MI:SS'),'rate_limited')"))
        # run_queue + run_events（供 lineage）
        await s.execute(text(
            "INSERT INTO run_queue (id,task_id,agent_slug,trigger,status,task_run_id) "
            "VALUES (9000,950,'dev','assign','done',9500)"))
        await s.execute(text(
            "INSERT INTO run_events (run_queue_id,task_id,agent_slug,event) "
            "VALUES (9000,950,'dev','claimed')"))
        await s.commit()


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import runs as runs_routes
    from auth import require_admin
    app = FastAPI()
    app.include_router(runs_routes.router)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    return TestClient(app)


def _api():
    c = _client()

    # get_messages：北京时间转换
    r = c.get("/api/tasks/950/messages")
    msgs = r.json()["messages"]
    check("get_messages 2 条", len(msgs) == 2, str(len(msgs)))
    check("get_messages created_at 北京格式", all(_FMT.match(m["created_at"]) for m in msgs), str(msgs))
    check("get_messages 不存在任务 404", c.get("/api/tasks/999999/messages").status_code == 404)

    # get_runs：SELECT* + summary
    r = c.get("/api/tasks/950/runs")
    runs = r.json()["runs"]
    check("get_runs 3 条", len(runs) == 3, str(len(runs)))
    r9500 = next(x for x in runs if x["id"] == 9500)
    check("get_runs summary 取首条文本", r9500["summary"] == "开场发言内容", r9500["summary"])
    check("get_runs 键集合(task_runs SELECT*)",
          {"id", "task_id", "agent_slug", "status", "provider_id", "pid",
           "started_at", "ended_at", "fail_reason", "conversation_id", "summary"} <= set(r9500),
          str(set(r9500)))

    # get_logs
    r = c.get("/api/runs/9500/logs")
    logs = r.json()["logs"]
    check("get_logs 2 条 + ts 北京", len(logs) == 2 and all(_FMT.match(x["ts"]) for x in logs), str(logs))

    # get_transcript：meta + items，tool_input 解析
    r = c.get("/api/runs/9500/transcript")
    tr = r.json()
    check("transcript meta.run_id=9500", tr["meta"]["run_id"] == 9500, str(tr["meta"]))
    tool_item = next((it for it in tr["items"] if it["tool"] == "Bash"), None)
    check("transcript tool_input 解析为对象",
          tool_item and tool_item["tool_input"].get("command") == "ls -la", str(tool_item))

    # get_lineage：LEFT JOIN task_run + run_events 分组
    r = c.get("/api/tasks/950/lineage")
    lin = r.json()
    check("lineage run_count=1", lin["run_count"] == 1, str(lin["run_count"]))
    ch = lin["chain"][0]
    check("lineage 关联 task_run(run_status=succeeded)", ch["run_status"] == "succeeded", str(ch))
    check("lineage duration≈5秒", ch["duration_seconds"] and 4 <= ch["duration_seconds"] <= 6,
          str(ch["duration_seconds"]))
    check("lineage events 分组(claimed)",
          len(ch["events"]) == 1 and ch["events"][0]["event"] == "claimed", str(ch["events"]))
    check("lineage total_run_seconds≈5", 4 <= lin["total_run_seconds"] <= 6, str(lin["total_run_seconds"]))

    # rate_limit_metrics：datetime('now',?) 窗口 + 归因分布
    r = c.get("/api/runs/rate-limit-metrics", params={"hours": 24})
    m = r.json()
    check("rate_limit 窗口内 failed>=1", m["failed_runs"] >= 1, str(m))
    check("rate_limit rate_limited_runs=1", m["rate_limited_runs"] == 1, str(m))
    check("rate_limit by_fail_reason 含 rate_limited",
          m["by_fail_reason"].get("rate_limited") == 1, str(m["by_fail_reason"]))

    # agents_overview：datetime('now',?) 窗口 + julianday SUM + running/idle
    r = c.get("/api/runs/agents-overview", params={"days": 30})
    ov = r.json()
    check("overview total_runs>=2(窗口内)", ov["stats"]["total_runs"] >= 2, str(ov["stats"]))
    check("overview total_run_seconds>0(julianday SUM)", ov["stats"]["total_run_seconds"] > 0,
          str(ov["stats"]["total_run_seconds"]))
    check("overview distinct_agents>=1", ov["stats"]["distinct_agents"] >= 1, str(ov["stats"]))
    check("overview running 含 9501", any(x["task_run_id"] == 9501 for x in ov["running"]),
          str(ov["running"]))
    # dev 在忙(有 running)，idle-dev 应在 idle
    idle_slugs = {x["agent_slug"] for x in ov["idle"]}
    check("overview idle 含 idle-dev", "idle-dev" in idle_slugs, str(idle_slugs))
    check("overview dev 不在 idle(正在跑)", "dev" not in idle_slugs, str(idle_slugs))


async def _direct():
    """直接调 _load_task_and_agent / _first_mentioned_slug。"""
    import routes.runs as runs
    task, agent = await runs._load_task_and_agent(950, "")
    check("_load_task_and_agent 取到 task+agent",
          task and agent and task["project_dir"] == "/p", str(task and task.get("project_dir")))
    check("_load_task_and_agent provider_effective",
          agent and agent["provider_id_effective"] == "pv", str(agent and agent.get("provider_id_effective")))
    # _first_mentioned_slug
    slug = await runs._first_mentioned_slug(95, "请 @开发 处理")
    check("_first_mentioned_slug 匹配到 dev", slug == "dev", slug)
    check("_first_mentioned_slug 无@返回空", (await runs._first_mentioned_slug(95, "没有艾特")) == "")


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch9_")
    _isolate(tmp)
    asyncio.run(_prep())
    _api()
    asyncio.run(_direct())
    import models
    asyncio.run(models.dispose_engine())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] routes/runs.py ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
