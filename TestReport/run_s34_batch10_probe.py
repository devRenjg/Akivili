# -*- coding: utf-8 -*-
"""数据底座 S3.4 第十批自检 · executor/runner.py ORM 迁移等价性探针（隔离）。

runner 是执行核心。这里直接调其数据层函数（都可独立调用），逐一验证与手写 SQL 等价：
_log/_save_assistant 落库、_finish_run/_finalize_if_running 的 now_expr+条件更新+rowcount 幂等、
_skill_bodies JOIN、_persist_memory 净交付筛选、_has_jian_deliverable(消息 OR 活动+JOIN
task_run 时间下界)、_has_trailing_stdout_after_deliverable(4 聚合 + 15s 容差判定)。
用 Alembic 建库。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_batch10_probe.py
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
        await s.execute(text("INSERT INTO projects (id,title,local_path) VALUES (77,'R','/r')"))
        await s.execute(text("INSERT INTO conversations (id,project_id) VALUES (77,77)"))
        await s.execute(text("INSERT INTO tasks (id,project_id,title,conversation_id,assignee_slug) "
                             "VALUES (770,77,'跑批任务',77,'dev')"))
        # 技能：dev 绑 2 个，一个有 body 一个空 body（应被过滤）
        await s.execute(text("INSERT INTO skills (slug,name,body) VALUES ('s1','技能一','正文一')"))
        await s.execute(text("INSERT INTO skills (slug,name,body) VALUES ('s2','技能二','   ')"))
        await s.execute(text("INSERT INTO agent_skills (agent_slug,skill_slug) VALUES ('dev','s1')"))
        await s.execute(text("INSERT INTO agent_skills (agent_slug,skill_slug) VALUES ('dev','s2')"))
        await s.commit()


async def _run():
    import models
    import executor.runner as runner
    from sqlalchemy import text

    SF = models.get_session_factory()

    # ---- _finish_run：now_expr 落 ended_at + status ----
    async with SF() as s:
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status) "
                             "VALUES (7000,770,'dev','running')"))
        await s.commit()
    await runner._finish_run(7000, "succeeded")
    async with SF() as s:
        row = (await s.execute(text("SELECT status,ended_at FROM task_runs WHERE id=7000"))).first()
    check("_finish_run status=succeeded", row.status == "succeeded", row.status)
    check("_finish_run ended_at 已落(now_expr)", bool(row.ended_at) and len(row.ended_at) >= 19, str(row.ended_at))

    # ---- _finalize_if_running：幂等，不覆盖已定终态 ----
    changed = await runner._finalize_if_running(7000, "killed")
    check("_finalize 已终态不改(rowcount 0)", changed is False, str(changed))
    async with SF() as s:
        st = (await s.execute(text("SELECT status FROM task_runs WHERE id=7000"))).scalar_one()
    check("_finalize 状态仍 succeeded", st == "succeeded", st)
    # 对 running 的 run 生效
    async with SF() as s:
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status) "
                             "VALUES (7001,770,'dev','running')"))
        await s.commit()
    changed2 = await runner._finalize_if_running(7001, "killed")
    check("_finalize running 补落(rowcount>0)", changed2 is True, str(changed2))
    async with SF() as s:
        st2 = (await s.execute(text("SELECT status,ended_at FROM task_runs WHERE id=7001"))).first()
    check("_finalize running→killed + ended_at", st2.status == "killed" and bool(st2.ended_at), str(st2))

    # ---- _log：run_logs 落库(tool_input JSON 序列化) ----
    await runner._log(7000, "stdout", "普通输出")
    await runner._log(7000, "tool", "摘要", tool="Bash", tool_input={"command": "ls -la"})
    async with SF() as s:
        logs = (await s.execute(text(
            "SELECT channel,content,tool,tool_input FROM run_logs WHERE run_id=7000 ORDER BY id"))).all()
    check("_log 落 2 条", len(logs) == 2, str(len(logs)))
    tool_log = next((x for x in logs if x.tool == "Bash"), None)
    check("_log tool_input JSON 序列化",
          tool_log and json.loads(tool_log.tool_input) == {"command": "ls -la"}, str(tool_log))

    # ---- _save_assistant ----
    await runner._save_assistant(77, "助手正文", author_slug="dev", run_id=7000)
    async with SF() as s:
        m = (await s.execute(text("SELECT role,content,author_slug,run_id FROM messages "
                                  "WHERE conversation_id=77 AND role='assistant'"))).first()
    check("_save_assistant 落库", m and m.content == "助手正文" and m.author_slug == "dev" and m.run_id == 7000,
          str(m))

    # ---- _skill_bodies：JOIN + 空 body 过滤 ----
    bodies = await runner._skill_bodies("dev")
    check("_skill_bodies 只返回有正文的技能(1条)", len(bodies) == 1, str(bodies))
    check("_skill_bodies 内容格式 '## 名\\n正文'", bodies and bodies[0] == "## 技能一\n正文一", str(bodies))

    # ---- _persist_memory：净交付筛选(since_msg_id 之后本人 assistant 消息) ----
    # 造 3 条消息：本人早于起点(不算) / 他人(不算) / 本人晚于起点(算)
    async with SF() as s:
        await s.execute(text("INSERT INTO messages (id,conversation_id,role,content,author_slug) "
                             "VALUES (500,77,'assistant','旧发言','dev')"))
        await s.execute(text("INSERT INTO messages (id,conversation_id,role,content,author_slug) "
                             "VALUES (501,77,'assistant','别人发言','other')"))
        await s.execute(text("INSERT INTO messages (id,conversation_id,role,content,author_slug) "
                             "VALUES (502,77,'assistant','净交付结论','dev')"))
        await s.commit()
    runner._RUN_CTX[7000] = {"slug": "dev", "conv_id": 77, "since_msg_id": 501,
                             "task_id": 770, "task_title": "跑批任务", "prompt": "干活", "stream_text": ""}
    await runner._persist_memory(7000)
    from memory import read_memory
    mem = read_memory("dev")
    check("_persist_memory 记入净交付", "净交付结论" in mem, "有" if "净交付结论" in mem else "无")
    check("_persist_memory 不含起点前发言", "旧发言" not in mem, "误含旧" if "旧发言" in mem else "ok")
    check("_persist_memory 不含他人发言", "别人发言" not in mem, "误含他人" if "别人发言" in mem else "ok")
    check("_persist_memory 幂等 pop(再调不重复)",
          runner._RUN_CTX.get(7000) is None, "ctx 应已 pop")

    # ---- _has_jian_deliverable ----
    # 消息路径：502 是 since=501 之后本人消息 → True
    d1 = await runner._has_jian_deliverable(77, "dev", 501, 770, 7000)
    check("_has_jian_deliverable 消息命中", d1 is True, str(d1))
    # since 抬到 502 之后、且无活动 → False
    d2 = await runner._has_jian_deliverable(77, "dev", 502, 770, 7000)
    check("_has_jian_deliverable 无交付返回 False", d2 is False, str(d2))
    # 活动路径：造一条 run 启动后的 commented 活动
    async with SF() as s:
        await s.execute(text("INSERT INTO activities (task_id,actor_type,actor_name,action,created_at) "
                             "VALUES (770,'agent','dev','commented',to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"))
        await s.commit()
    d3 = await runner._has_jian_deliverable(77, "dev", 502, 770, 7000)
    check("_has_jian_deliverable 活动 JOIN 命中", d3 is True, str(d3))

    # ---- _has_trailing_stdout_after_deliverable ----
    # 注意：last_msg 是「会话内该 slug 的最后交付」——不 run-scoped（与手写 SQL 一致）。
    # 故用全新会话 78 + 全新 slug，隔离前面步骤给 conv77/dev 造的 now 时间戳消息，避免污染。
    async with SF() as s:
        await s.execute(text("INSERT INTO conversations (id,project_id) VALUES (78,77)"))
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status,started_at) "
                             "VALUES (7500,770,'trail-dev','running',to_char((now() AT TIME ZONE 'UTC') + interval '-1 hour', 'YYYY-MM-DD HH24:MI:SS'))"))
        # 会话内该 slug 唯一交付消息：60 分钟前
        await s.execute(text("INSERT INTO messages (conversation_id,role,content,author_slug,created_at) "
                             "VALUES (78,'assistant','早交付','trail-dev',to_char((now() AT TIME ZONE 'UTC') + interval '-60 minutes', 'YYYY-MM-DD HH24:MI:SS'))"))
        # 长 stdout：现在（明显 >15s 晚于交付）；须 >40 字符方过 length(trim)>40 阈值
        await s.execute(text("INSERT INTO run_logs (run_id,channel,content,ts) VALUES "
                             "(7500,'stdout','这是一段很长的收尾分析结论用于触发长度阈值判定这里再补足够多的中文字符确保整体长度稳稳超过四十个字符的下限判定通过',to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"))
        await s.commit()
    t1 = await runner._has_trailing_stdout_after_deliverable(78, "trail-dev", 770, 7500)
    check("_has_trailing 收尾漏交付判 True", t1 is True, str(t1))
    # run 7501：有 stdout 但无任何交付 → False（交由 _has_jian_deliverable 那条负责）
    async with SF() as s:
        await s.execute(text("INSERT INTO task_runs (id,task_id,agent_slug,status,started_at) "
                             "VALUES (7501,770,'zzz','running',to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"))
        await s.execute(text("INSERT INTO run_logs (run_id,channel,content,ts) VALUES "
                             "(7501,'stdout','另一段足够长的输出内容超过四十字符阈值判定使用yyyyyyyyyy',to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"))
        await s.commit()
    t2 = await runner._has_trailing_stdout_after_deliverable(78, "zzz", 770, 7501)
    check("_has_trailing 无交付返回 False", t2 is False, str(t2))


def main():
    import asyncio
    tmp = tempfile.mkdtemp(prefix="batch10_")
    _isolate(tmp)
    asyncio.run(_seed())
    asyncio.run(_run())
    import models
    asyncio.run(models.dispose_engine())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] executor/runner.py ORM 迁移与手写 SQL 等价")


if __name__ == "__main__":
    main()
