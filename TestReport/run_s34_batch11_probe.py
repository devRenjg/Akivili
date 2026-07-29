# -*- coding: utf-8 -*-
"""数据底座 S3.4 第十一批自检 · collab.py ORM 迁移等价性探针（隔离）。

collab 是协同调度核心，最大且并发敏感。重点验证与手写 SQL 等价：
_claim_one(优先级 CASE + FIFO + next_retry_at 退避过滤 + SELECT-then-UPDATE 领取)、
enqueue_run(pending 去重 + lastrowid)、_mention_chain_len(链式计数/产出重置)、
reclaim_orphan_runs(两层回收 + done/reviewing→succeeded vs killed 相关子查询)、
sweep_orphan_task_runs(julianday idle 计算 + 相关子查询 MAX(ts))、
_load_task_agent(SELECT t.*/SELECT * dict 契约)、_member_names/resolve_agent_displays/
build_roster/get_leader_slug/_run_produced_deliverable。用 Alembic 建库。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch11_probe.py
"""
import json
import os
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
    from run_qa_suite import isolated_pg_db_url  # noqa: PLC0415
    cfg = {
        "db_url": isolated_pg_db_url(),   # S5：PG 隔离库（替代 sqlite db_path）
        "agent_library_dir": os.path.join(tmp, "agents"),
        "skills_dir": os.path.join(tmp, "skills"),
        "memory_dir": os.path.join(tmp, "mem"),
        "providers": [], "default_provider_id": "",
    }
    os.makedirs(cfg["memory_dir"], exist_ok=True)
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)


async def _seed():
    import models
    from db_migrate import run_migrations
    from sqlalchemy import text
    run_migrations()
    async with models.get_session_factory()() as s:
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (60,'协同项目','/c')"))
        # 成员：leader + 两名成员，其中 dev 有昵称
        await s.execute(text("INSERT INTO project_agents (id,project_id,slug,name,emoji,persona,enabled,is_leader) "
                             "VALUES (1,60,'lead','队长','🧭','负责统筹\n第二句职责',1,1)"))
        await s.execute(text("INSERT INTO project_agents (id,project_id,slug,name,persona,enabled,is_leader) "
                             "VALUES (2,60,'dev','开发','写代码',1,0)"))
        await s.execute(text("INSERT INTO project_agents (id,project_id,slug,name,persona,enabled,is_leader) "
                             "VALUES (3,60,'qa','测试','做测试',1,0)"))
        await s.execute(text("INSERT INTO agent_profiles (slug,provider_id,nickname) VALUES ('dev','pvX','小开')"))
        # 技能：dev 绑一个
        await s.execute(text("INSERT INTO skills (slug,name,body) VALUES ('sk','技能A','正文')"))
        await s.execute(text("INSERT INTO agent_skills (agent_slug,skill_slug) VALUES ('dev','sk')"))
        await s.execute(text("INSERT INTO conversations (id,project_id) VALUES (60,60)"))
        # 任务：high/medium/none 各一，供 _claim_one 优先级验证
        await s.execute(text("INSERT INTO tasks (id,project_id,title,description,conversation_id,priority,assignee_slug) "
                             "VALUES (600,60,'低优任务','desc-low',60,'none','dev')"))
        await s.execute(text("INSERT INTO tasks (id,project_id,title,conversation_id,priority) "
                             "VALUES (601,60,'高优任务',60,'high')"))
        await s.execute(text("INSERT INTO tasks (id,project_id,title,conversation_id,priority) "
                             "VALUES (602,60,'中优任务',60,'medium')"))
        await s.commit()


async def _run():
    import models
    import collab
    from sqlalchemy import text
    SF = models.get_session_factory()

    # ---- 读类 helper ----
    names = await collab._member_names(60, exclude_slug="lead")
    check("_member_names 排除 leader + 昵称优先", names == ["小开", "测试"], str(names))
    disp = await collab.resolve_agent_displays(["dev", "qa", "ghost"])
    check("resolve_displays dev=昵称（角色）", disp.get("dev") == "小开（开发）", str(disp.get("dev")))
    check("resolve_displays qa=角色名", disp.get("qa") == "测试", str(disp.get("qa")))
    check("resolve_displays 查不到回退自身", disp.get("ghost") == "ghost", str(disp.get("ghost")))
    leader = await collab.get_leader_slug(60)
    check("get_leader_slug", leader == "lead", leader)
    roster = await collab.build_roster(60, viewer_slug="dev")
    check("build_roster 含团队名", "协同项目" in roster, "")
    check("build_roster 标出自己", "← 就是你自己" in roster, "")
    check("build_roster dev 技能内联", "技能A" in roster, "")

    # ---- enqueue_run：pending 去重 + lastrowid ----
    rid1 = await collab.enqueue_run(600, "dev", "P1", trigger="assign")
    check("enqueue_run 返回自增 id", isinstance(rid1, int) and rid1 > 0, str(rid1))
    rid_dup = await collab.enqueue_run(600, "dev", "P1dup", trigger="assign")
    check("enqueue_run pending 去重返回 None", rid_dup is None, str(rid_dup))
    # run_event enqueued 落库
    async with SF() as s:
        ev = (await s.execute(text("SELECT event FROM run_events WHERE run_queue_id=:i"),
                              {"i": rid1})).scalars().all()
    check("enqueue_run 记 enqueued 事件", "enqueued" in ev, str(ev))

    # ---- _claim_one：优先级 CASE + FIFO ----
    # 给 601(high)、602(medium) 各入队；加上已有 600(none) 的 rid1
    r_high = await collab.enqueue_run(601, "qa", "PH", trigger="assign")
    r_med = await collab.enqueue_run(602, "qa", "PM", trigger="assign")
    claimed1 = await collab._claim_one()
    check("_claim_one 先领 high", claimed1 and claimed1["id"] == r_high, str(claimed1 and claimed1["id"]))
    check("_claim_one item dict 契约(task_id/agent_slug/prompt)",
          claimed1 and claimed1["task_id"] == 601 and claimed1["agent_slug"] == "qa"
          and claimed1["prompt"] == "PH", str(claimed1))
    # 领取后置 running
    async with SF() as s:
        st = (await s.execute(text("SELECT status FROM run_queue WHERE id=:i"),
                              {"i": r_high})).scalar_one()
    check("_claim_one 领取后置 running", st == "running", st)
    claimed2 = await collab._claim_one()
    check("_claim_one 次领 medium", claimed2 and claimed2["id"] == r_med, str(claimed2 and claimed2["id"]))
    claimed3 = await collab._claim_one()
    check("_claim_one 再领 none(FIFO 到 600)", claimed3 and claimed3["id"] == rid1,
          str(claimed3 and claimed3["id"]))
    check("_claim_one 队空返回 None", (await collab._claim_one()) is None)

    # ---- _claim_one：next_retry_at 退避过滤 ----
    async with SF() as s:
        # 两行用**不同 agent_slug**（lead/qa），避免撞 uq_run_queue_active（同 task+agent 至多一条
        # 活跃 run，迁移 003）——本子测只关心 next_retry_at 退避过滤，与去重正交，换 slug 不改语义。
        # 一个未来退避的 queued 行（不应被领取）
        await s.execute(text("INSERT INTO run_queue (id,task_id,agent_slug,status,next_retry_at) "
                             "VALUES (900,600,'lead','queued',to_char((now() AT TIME ZONE 'UTC') + interval '+1 hour', 'YYYY-MM-DD HH24:MI:SS'))"))
        # 一个已到点的 queued 行（应被领取）
        await s.execute(text("INSERT INTO run_queue (id,task_id,agent_slug,status,next_retry_at) "
                             "VALUES (901,600,'qa','queued',to_char((now() AT TIME ZONE 'UTC') + interval '-1 minute', 'YYYY-MM-DD HH24:MI:SS'))"))
        await s.commit()
    c = await collab._claim_one()
    check("_claim_one 跳过退避窗口内、领到已到点行", c and c["id"] == 901, str(c and c["id"]))
    check("_claim_one 退避未到不领(队现空)", (await collab._claim_one()) is None)

    # ---- _load_task_agent：dict 契约 ----
    task, agent = await collab._load_task_agent(600, "dev")
    check("_load_task_agent task project_dir", task and task["project_dir"] == "/c", str(task and task.get("project_dir")))
    check("_load_task_agent task 全列", task and task["title"] == "低优任务" and task["description"] == "desc-low",
          str(task and (task.get("title"), task.get("description"))))
    check("_load_task_agent agent provider_effective",
          agent and agent["provider_id_effective"] == "pvX", str(agent and agent.get("provider_id_effective")))
    check("_load_task_agent agent slug/persona", agent and agent["slug"] == "dev" and agent["persona"] == "写代码",
          str(agent and (agent.get("slug"), agent.get("persona"))))

    # ---- _run_produced_deliverable ----
    d0 = await collab._run_produced_deliverable(600, "dev")
    check("_run_produced_deliverable 无交付 False", d0 is False, str(d0))
    async with SF() as s:
        await s.execute(text("INSERT INTO messages (conversation_id,role,content,author_slug) "
                             "VALUES (60,'assistant','我的产出','dev')"))
        await s.commit()
    d1 = await collab._run_produced_deliverable(600, "dev")
    check("_run_produced_deliverable 消息命中 True", d1 is True, str(d1))


async def _run_orphans():
    """reclaim_orphan_runs + sweep_orphan_task_runs 需干净初态，单独造数。"""
    import models
    import collab
    from sqlalchemy import text
    SF = models.get_session_factory()

    # ---- reclaim_orphan_runs：两层 + done/reviewing→succeeded vs killed ----
    # reclaim 的真实语义是「start_loop 之前调用，此刻所有 running 皆为无主孤儿」。
    # 前面 _run 的 _claim_one 把若干 run_queue 行置 running 了；这里清空这些遗留 running，
    # 还原「干净启动」初态，才能精确断言本次造的 3 条孤儿被回收。
    async with SF() as s:
        await s.execute(text("DELETE FROM run_queue WHERE status='running'"))
        await s.execute(text("DELETE FROM task_runs WHERE status='running'"))
        # 任务：一个 done、一个 in_progress
        await s.execute(text("INSERT INTO tasks (id,project_id,title,conversation_id,status) "
                             "VALUES (700,60,'已完成',60,'done')"))
        await s.execute(text("INSERT INTO tasks (id,project_id,title,conversation_id,status) "
                             "VALUES (701,60,'进行中',60,'in_progress')"))
        # run_queue 孤儿
        await s.execute(text("INSERT INTO run_queue (id,task_id,agent_slug,status) VALUES (800,700,'dev','running')"))
        # task_runs 孤儿：700 任务下(应→succeeded)、701 任务下(应→killed)
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status) VALUES (810,700,'dev','running')"))
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status) VALUES (811,701,'dev','running')"))
        await s.commit()
    n = await collab.reclaim_orphan_runs()
    check("reclaim 回收计数=3(1 queue+2 run)", n == 3, str(n))
    async with SF() as s:
        rq = (await s.execute(text("SELECT status FROM run_queue WHERE id=800"))).scalar_one()
        r810 = (await s.execute(text("SELECT status,ended_at FROM task_runs WHERE id=810"))).first()
        r811 = (await s.execute(text("SELECT status,ended_at FROM task_runs WHERE id=811"))).first()
    check("reclaim run_queue→failed", rq == "failed", rq)
    check("reclaim 已完成任务的 run→succeeded", r810.status == "succeeded" and bool(r810.ended_at), str(r810))
    check("reclaim 未收尾任务的 run→killed", r811.status == "killed" and bool(r811.ended_at), str(r811))

    # ---- sweep_orphan_task_runs：julianday idle 计算 ----
    async with SF() as s:
        # 静默 1 小时的 running（started_at 1h 前、无日志）→ 应被扫到
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status,started_at) "
                             "VALUES (820,701,'dev','running',to_char((now() AT TIME ZONE 'UTC') + interval '-1 hour', 'YYYY-MM-DD HH24:MI:SS'))"))
        # 刚活动的 running（现在有日志）→ 不该被扫到
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status,started_at) "
                             "VALUES (821,701,'dev','running',to_char((now() AT TIME ZONE 'UTC') + interval '-1 hour', 'YYYY-MM-DD HH24:MI:SS'))"))
        await s.execute(text("INSERT INTO run_logs (run_id,channel,content,ts) "
                             "VALUES (821,'stdout','刚刚还在输出',to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"))
        await s.commit()
    swept = await collab.sweep_orphan_task_runs(idle_sec=1800)   # 30 分钟阈值
    check("sweep 回收 1 条(仅静默超阈的 820)", swept == 1, str(swept))
    async with SF() as s:
        s820 = (await s.execute(text("SELECT status FROM task_runs WHERE id=820"))).scalar_one()
        s821 = (await s.execute(text("SELECT status FROM task_runs WHERE id=821"))).scalar_one()
    check("sweep 静默孤儿→killed(701 未收尾)", s820 == "killed", s820)
    check("sweep 活跃 run 不动(仍 running)", s821 == "running", s821)


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch11_")
    _isolate(tmp)
    asyncio.run(_seed())
    asyncio.run(_run())
    asyncio.run(_run_orphans())
    import models
    asyncio.run(models.dispose_engine())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] collab.py ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
