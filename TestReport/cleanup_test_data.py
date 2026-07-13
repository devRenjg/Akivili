"""Akivili 测试数据清理工具（作用于真实 jianagency.db）。

用途：清除**历史手工验证**在真实库里留下的测试项目及其全部关联数据。
（run_qa_suite.py / run_concurrency_probe.py 用隔离临时库，本就自清，不需要这个。
 这个工具是给"没走隔离脚本、直接在真实库建的测试项目"兜底的。）

安全设计（对齐 akivili-testing-safety 规则）：
- 只认**测试前缀**项目：标题以 __test__ / __qa / __conc 开头。
- 真实项目**硬保护**：标题无测试前缀，或 local_path 落在受保护真实目录（Qlipoth/Agents 等）→ 一律跳过。
- 默认 **dry-run**（只列不删）；加 --yes 才真正删除。
- 删除前**自动备份**真实库；按**精确 project id** 级联删除，绝不用模糊 LIKE 批量删。
- 绝不触碰 memory/ 下的 .md（那是 Agent 跨项目共享的真实记忆）。

用法：
  py -3.12 TestReport/cleanup_test_data.py          # dry-run，列出将删什么
  py -3.12 TestReport/cleanup_test_data.py --yes     # 实际删除（先自动备份）
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"

# 测试项目标题前缀（只有匹配这些的才可能被删）
TEST_PREFIXES = ("__test__", "__qa", "__conc")
# 受保护的真实目录关键片段：local_path 命中任一 → 绝不删（即便标题带前缀也跳过并告警）
PROTECTED_PATH_HINTS = ("qlipoth", "\\code\\agents", "/code/agents")


def resolve_db_path() -> Path:
    sys.path.insert(0, str(BACKEND))
    import config  # noqa: PLC0415

    return Path(config.load_settings().db_path)


def is_test_title(title: str) -> bool:
    return any((title or "").startswith(p) for p in TEST_PREFIXES)


def is_protected_path(local_path: str) -> bool:
    lp = (local_path or "").lower()
    return any(h in lp for h in PROTECTED_PATH_HINTS)


def child_ids(db: sqlite3.Connection, pid: int) -> dict:
    def ids(sql, *a):
        return [r[0] for r in db.execute(sql, a).fetchall()]

    tids = ids("SELECT id FROM tasks WHERE project_id=?", pid)
    cids = ids("SELECT id FROM conversations WHERE project_id=?", pid)
    trids = []
    if tids:
        ph = ",".join("?" * len(tids))
        trids = ids(f"SELECT id FROM task_runs WHERE task_id IN ({ph})", *tids)
    return {"tasks": tids, "conversations": cids, "task_runs": trids}


def count_related(db: sqlite3.Connection, pid: int, ch: dict) -> dict:
    def ph(x):
        return ",".join("?" * len(x))

    def cnt(sql, *a):
        return db.execute(sql, a).fetchone()[0]

    tids, cids, trids = ch["tasks"], ch["conversations"], ch["task_runs"]
    return {
        "project_agents": cnt("SELECT COUNT(*) FROM project_agents WHERE project_id=?", pid),
        "tasks": len(tids),
        "conversations": len(cids),
        "task_runs": len(trids),
        "messages": cnt(f"SELECT COUNT(*) FROM messages WHERE conversation_id IN ({ph(cids)})", *cids) if cids else 0,
        "activities": cnt(f"SELECT COUNT(*) FROM activities WHERE task_id IN ({ph(tids)})", *tids) if tids else 0,
        "run_queue": cnt(f"SELECT COUNT(*) FROM run_queue WHERE task_id IN ({ph(tids)})", *tids) if tids else 0,
        "run_logs": cnt(f"SELECT COUNT(*) FROM run_logs WHERE run_id IN ({ph(trids)})", *trids) if trids else 0,
    }


def delete_project(db: sqlite3.Connection, pid: int, ch: dict) -> None:
    def ph(x):
        return ",".join("?" * len(x))

    tids, cids, trids = ch["tasks"], ch["conversations"], ch["task_runs"]
    c = db.cursor()
    if trids:
        c.execute(f"DELETE FROM run_logs WHERE run_id IN ({ph(trids)})", trids)
    if tids:
        c.execute(f"DELETE FROM run_queue WHERE task_id IN ({ph(tids)})", tids)
        c.execute(f"DELETE FROM task_runs WHERE task_id IN ({ph(tids)})", tids)
        c.execute(f"DELETE FROM activities WHERE task_id IN ({ph(tids)})", tids)
    if cids:
        c.execute(f"DELETE FROM messages WHERE conversation_id IN ({ph(cids)})", cids)
    c.execute("DELETE FROM conversations WHERE project_id=?", (pid,))
    c.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
    c.execute("DELETE FROM project_agents WHERE project_id=?", (pid,))
    c.execute("DELETE FROM projects WHERE id=?", (pid,))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="实际删除（否则仅 dry-run 列出）")
    ap.add_argument("--rmdir", action="store_true", help="同时删除测试项目绑定的空临时目录（仅当为空）")
    args = ap.parse_args()

    db_path = resolve_db_path()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        return 1

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id,title,local_path FROM projects ORDER BY id").fetchall()

    to_delete = []
    print(f"库：{db_path}\n扫描 {len(rows)} 个项目：")
    for r in rows:
        pid, title, lp = r["id"], r["title"], r["local_path"]
        if not is_test_title(title):
            print(f"  跳过 #{pid} {title!r}（非测试项目，保护）")
            continue
        if is_protected_path(lp):
            print(f"  ⚠️ 跳过 #{pid} {title!r}：标题像测试但路径在受保护真实目录 {lp!r}，人工确认")
            continue
        ch = child_ids(db, pid)
        cnt = count_related(db, pid, ch)
        to_delete.append((pid, title, lp, ch, cnt))
        print(f"  待删 #{pid} {title!r} path={lp!r} 关联={cnt}")

    if not to_delete:
        print("\n没有需要清理的测试项目。")
        db.close()
        return 0

    if not args.yes:
        print(f"\n[dry-run] 命中 {len(to_delete)} 个测试项目。加 --yes 实际删除（会先自动备份）。")
        db.close()
        return 0

    db.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(db_path.name + f".bak_{stamp}")
    shutil.copy2(db_path, backup)
    print(f"\n已备份：{backup}")

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    for pid, title, lp, ch, _ in to_delete:
        cur = db.execute("SELECT title FROM projects WHERE id=?", (pid,)).fetchone()
        assert cur and is_test_title(cur["title"]), f"二次校验失败，中止 #{pid}"
        delete_project(db, pid, ch)
        print(f"已删除 #{pid} {title!r}")
    db.commit()
    db.close()

    if args.rmdir:
        for _, title, lp, _, _ in to_delete:
            p = Path(lp) if lp else None
            if p and p.is_dir() and not any(p.iterdir()):
                try:
                    p.rmdir()
                    print(f"已删除空目录 {p}")
                except OSError as e:
                    print(f"目录未删 {p}：{e}")

    print(f"\n完成：清理 {len(to_delete)} 个测试项目。真实项目未触碰。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
