# -*- coding: utf-8 -*-
"""数据底座 S3.4 第一批自检 · aiosqlite↔ORM 跨路径一致性探针（隔离）。

S3.4 让 ORM 首次进入运行期，而运行期既有代码仍用 aiosqlite(get_connection)。
真实调用链（project_agents.py 等）是「aiosqlite 写+commit+close → sync_agent_memory
(ORM 读)」。本探针复刻该链，确认 WAL 下 ORM 池连接能读到 aiosqlite 已提交的写，
两条连接路径并存不串数据、不锁死。绝不碰真实库。

用法：py -3.12 TestReport/run_s34_crosspath_probe.py
"""
import asyncio
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
    db_path = os.path.join(tmp, "crosspath.db")
    cfg = {"db_path": db_path, "providers": [], "default_provider_id": ""}
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)
    return db_path


async def _run():
    import database
    import models
    import agent_memory_sync as ams

    await database.init_db()

    # —— 复刻真实链：aiosqlite 写 project + project_agent + skills，commit+close ——
    db = await database.get_connection()
    try:
        await db.execute("INSERT INTO projects (id, title, local_path) VALUES (5, '跨路径项目', '/p/x')")
        await db.execute("INSERT INTO project_agents (project_id, slug, name) VALUES (5, 'cross', 'Cross')")
        await db.execute("INSERT INTO skills (slug, name, description, body) VALUES ('csk','跨技能','desc','body首行')")
        await db.execute("INSERT INTO agent_skills (agent_slug, skill_slug) VALUES ('cross','csk')")
        await db.commit()
    finally:
        await db.close()

    # —— ORM 读（sync_agent_memory 内部走 ORM session）——
    async with models.get_session_factory()() as session:
        ws = await ams._workspace_body(session, "cross")
        sk = await ams._skills_body(session, "cross")

    check("ORM 读到 aiosqlite 已提交的项目", "跨路径项目" in ws, ws[:60])
    check("ORM 读到 aiosqlite 已提交的技能", "跨技能" in sk, sk[:60])

    # —— 反向：ORM engine 之后，aiosqlite 再写再读，确认两路径可交替 ——
    db2 = await database.get_connection()
    try:
        await db2.execute("INSERT INTO projects (id, title, local_path) VALUES (6, '第二项目', '/p/y')")
        await db2.execute("INSERT INTO project_agents (project_id, slug, name) VALUES (6, 'cross', 'Cross')")
        await db2.commit()
        cur = await db2.execute("SELECT COUNT(*) FROM project_agents WHERE slug='cross'")
        cnt_aiosqlite = (await cur.fetchone())[0]
    finally:
        await db2.close()

    async with models.get_session_factory()() as session:
        ws2 = await ams._workspace_body(session, "cross")
        # ORM 应看到 2 个项目
        orm_sees_two = ws2.count("→") == 2

    check("aiosqlite 二次写后自读一致", cnt_aiosqlite == 2, f"count={cnt_aiosqlite}")
    check("ORM 读到 aiosqlite 后续新增(2项目)", orm_sees_two, f"ws2={ws2!r}")

    # —— sync_agent_memory 全链路不抛错（真实入口，写记忆到临时 memory_dir）——
    import config
    # memory_dir 指向临时目录，避免污染
    ok = True
    try:
        # sync_agent_memory 内部 upsert_managed_section 需要 memory 目录，设到 tmp
        await ams.sync_agent_memory("cross")
    except Exception as e:
        ok = False
        check("sync_agent_memory 全链路无异常", False, f"{type(e).__name__}: {e}")
    if ok:
        check("sync_agent_memory 全链路无异常", True)

    await models.dispose_engine()


def main():
    tmp = tempfile.mkdtemp(prefix="crosspath_")
    _isolate(tmp)
    # memory_dir 指到 tmp，隔离记忆写入
    import config
    s = config.load_settings()
    # 直接改配置文件里的 memory_dir
    cfg_path = config.CONFIG_FILE
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["memory_dir"] = os.path.join(tmp, "memory")
    data["agent_library_dir"] = os.path.join(tmp, "agents")
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] aiosqlite↔ORM 跨路径并存一致，真实链路无异常")


if __name__ == "__main__":
    main()
