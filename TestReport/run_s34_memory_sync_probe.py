# -*- coding: utf-8 -*-
"""数据底座 S3.4 第一批自检 · agent_memory_sync ORM 迁移等价性探针（隔离）。

验证迁移后的 _workspace_body / _skills_body（ORM）与迁移前的手写 SQL **输出逐字节
一致**：同样的 JOIN 语义、同样的 ORDER BY、同样的按列取值、同样的 is_test_project
过滤。用「影子手写 SQL」（复刻迁移前的原始查询）作为黄金对照，绝不碰真实库。

用法：py -3.12 TestReport/run_s34_memory_sync_probe.py
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
    db_path = os.path.join(tmp, "s34_probe.db")
    cfg = {"db_path": db_path, "providers": [], "default_provider_id": ""}
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    import config
    config.CONFIG_FILE = Path(cfg_path)
    return db_path


# 迁移前的原始手写 SQL（影子黄金对照，逐字复刻迁移前代码）
_OLD_WS_SQL = """SELECT p.title, p.local_path
   FROM project_agents pa JOIN projects p ON p.id = pa.project_id
   WHERE pa.slug = ? ORDER BY p.title"""
_OLD_SK_SQL = """SELECT s.name, s.description, s.body
   FROM agent_skills a JOIN skills s ON s.slug = a.skill_slug
   WHERE a.agent_slug = ? ORDER BY s.name"""


async def _old_workspace_body(db, slug):
    """迁移前逻辑的忠实复刻（用手写 SQL）。"""
    from config import is_test_project
    cur = await db.execute(_OLD_WS_SQL, (slug,))
    rows = await cur.fetchall()
    rows = [r for r in rows if not is_test_project(r["title"])]
    if not rows:
        return ""
    lines = ["## 🗂️ 工作区（系统维护，请遵守）", "",
             "你在以下项目中工作。**只能在对应项目的本地路径内操作文件，不得越界到其他目录**："]
    for r in rows:
        lines.append(f"- **{r['title']}** → `{r['local_path']}`")
    lines.append("")
    lines.append("开始任何任务前，先确认当前任务属于哪个项目，并把操作限定在该项目路径内。")
    return "\n".join(lines)


async def _old_skills_body(db, slug):
    cur = await db.execute(_OLD_SK_SQL, (slug,))
    rows = await cur.fetchall()
    if not rows:
        return ""
    lines = ["## 🧩 可用 Skills（系统维护）", "",
             "你已被赋予以下 Skill。遇到对应场景时，**主动调用对应 Skill 的能力指令**来完成工作："]
    for r in rows:
        desc = r["description"] or "（无描述）"
        lines.append(f"- **{r['name']}**：{desc}")
        first = next((ln.strip() for ln in (r["body"] or "").splitlines() if ln.strip()), "")
        if first:
            lines.append(f"  - 使用要领：{first}")
    lines.append("")
    lines.append("判断要做的事匹配某个 Skill 时，按其能力指令执行；不确定是否适用时，优先参考 Skill 描述。")
    return "\n".join(lines)


async def _seed(db):
    """造数据：覆盖多项目、排序、测试项目过滤、空描述/空body、无技能等边界。"""
    # projects：故意乱序插入，验证 ORDER BY p.title 生效
    await db.execute("INSERT INTO projects (id, title, local_path) VALUES (10, 'Zeta项目', '/p/zeta')")
    await db.execute("INSERT INTO projects (id, title, local_path) VALUES (11, 'Alpha项目', '/p/alpha')")
    await db.execute("INSERT INTO projects (id, title, local_path) VALUES (12, '__test__自动化', '/p/qa')")  # 测试项目(__test__前缀)，应被过滤
    # project_agents：agent 'a1' 在 3 个项目
    for pid in (10, 11, 12):
        await db.execute(
            "INSERT INTO project_agents (project_id, slug, name) VALUES (?, 'a1', 'A1')", (pid,))
    # skills：乱序 + 空描述/空body 边界
    await db.execute("INSERT INTO skills (slug, name, description, body) VALUES ('sk-z','Zeta技能','描述Z','正文首行Z\\n第二行')")
    await db.execute("INSERT INTO skills (slug, name, description, body) VALUES ('sk-a','Alpha技能','','')")
    await db.execute("INSERT INTO agent_skills (agent_slug, skill_slug) VALUES ('a1','sk-z')")
    await db.execute("INSERT INTO agent_skills (agent_slug, skill_slug) VALUES ('a1','sk-a')")
    await db.commit()


async def _run():
    import database
    import models
    import agent_memory_sync as ams
    from db_migrate import run_migrations

    run_migrations()   # 建库唯一走 Alembic（S3.6 已下线 init_db 建表）

    # 判定 is_test_project 确实认得 'QA-自动化测试'（否则过滤断言无意义）
    from config import is_test_project
    check("is_test_project 识别测试项目名", is_test_project("__test__自动化"),
          "若不识别则过滤对照失效，需换测试项目名")

    db = await database.get_connection()
    try:
        await _seed(db)
        old_ws = await _old_workspace_body(db, "a1")
        old_sk = await _old_skills_body(db, "a1")
        # 无项目/无技能的 slug
        old_ws_empty = await _old_workspace_body(db, "nobody")
        old_sk_empty = await _old_skills_body(db, "nobody")
    finally:
        await db.close()

    async with models.get_session_factory()() as session:
        new_ws = await ams._workspace_body(session, "a1")
        new_sk = await ams._skills_body(session, "a1")
        new_ws_empty = await ams._workspace_body(session, "nobody")
        new_sk_empty = await ams._skills_body(session, "nobody")

    check("工作区段落 ORM==手写(逐字节)", new_ws == old_ws,
          f"len(new)={len(new_ws)} len(old)={len(old_ws)}")
    check("Skills段落 ORM==手写(逐字节)", new_sk == old_sk,
          f"len(new)={len(new_sk)} len(old)={len(old_sk)}")
    check("空工作区 ORM==手写(均空串)", new_ws_empty == old_ws == old_ws_empty or new_ws_empty == old_ws_empty,
          f"new={new_ws_empty!r} old={old_ws_empty!r}")
    check("空Skills ORM==手写(均空串)", new_sk_empty == old_sk_empty,
          f"new={new_sk_empty!r} old={old_sk_empty!r}")

    # 内容正确性抽查：测试项目被过滤、排序正确
    check("测试项目已过滤(不含 __test__自动化)", "__test__自动化" not in new_ws, new_ws[:80])
    check("工作区排序 Alpha 在 Zeta 前",
          new_ws.index("Alpha项目") < new_ws.index("Zeta项目"))
    check("Skills排序 Alpha技能 在 Zeta技能 前",
          new_sk.index("Alpha技能") < new_sk.index("Zeta技能"))
    check("空描述回退为（无描述）", "（无描述）" in new_sk, new_sk)

    await models.dispose_engine()


def main():
    tmp = tempfile.mkdtemp(prefix="s34_mem_")
    _isolate(tmp)
    asyncio.run(_run())

    print("\n" + "=" * 60)
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  [X] {n} -- {d}")
        sys.exit(1)
    print("[OK] agent_memory_sync ORM 迁移与手写 SQL 输出逐字节等价")


if __name__ == "__main__":
    main()
