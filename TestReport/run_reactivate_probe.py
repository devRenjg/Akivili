"""Akivili 重跑即时回写探针（问题一）。

验证 routes/runs._reactivate_on_redispatch：重跑已收尾任务时，即时把该任务及其父任务
从 done/reviewing 回写 in_progress，不等轮询聚合。临时 config/DB，测完清理。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import setup_isolated_config, bootstrap_backend  # noqa: E402


class Probe:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(r[1] for r in self.results)


async def _mk(db, pid, title, status, parent=None):
    conv = (await db.execute("INSERT INTO conversations (project_id,title) VALUES (?,?)", (pid, title))).lastrowid
    cur = await db.execute(
        "INSERT INTO tasks (project_id,title,status,conversation_id,parent_task_id) VALUES (?,?,?,?,?)",
        (pid, title, status, conv, parent))
    await db.commit()
    return cur.lastrowid


async def _status(db, tid):
    r = await (await db.execute("SELECT status FROM tasks WHERE id=?", (tid,))).fetchone()
    return r["status"] if r else None


async def run_probe(paths):
    probe = Probe()
    await bootstrap_backend(paths)
    from database import get_connection
    from routes.runs import _reactivate_on_redispatch

    db = await get_connection()
    try:
        pid = (await db.execute(
            "INSERT INTO projects (title,local_path,description) VALUES (?,?,?)",
            ("回写探针项目", str(paths["project"]), "probe"))).lastrowid
        # 场景：父 reviewing，子 done（重跑子任务）
        parent = await _mk(db, pid, "父任务", "reviewing")
        sub = await _mk(db, pid, "子任务", "done", parent=parent)
        # 独立顶层任务 done（重跑自身）
        top = await _mk(db, pid, "独立任务", "done")
        # 首次执行的子任务 backlog（不该被回写）
        parent2 = await _mk(db, pid, "父任务2", "in_progress")
        fresh = await _mk(db, pid, "新子任务", "backlog", parent=parent2)
    finally:
        await db.close()

    # 重跑子任务（done）→ 子任务 + 父任务都回 in_progress
    await _reactivate_on_redispatch(sub, parent, "done")
    db = await get_connection()
    try:
        probe.check("重跑子任务 → 子任务回 in_progress", await _status(db, sub) == "in_progress",
                    f"sub={await _status(db, sub)}")
        probe.check("重跑子任务 → 父任务(reviewing)回 in_progress",
                    await _status(db, parent) == "in_progress", f"parent={await _status(db, parent)}")
    finally:
        await db.close()

    # 重跑独立顶层任务（done，无父）→ 自身回 in_progress
    await _reactivate_on_redispatch(top, None, "done")
    db = await get_connection()
    try:
        probe.check("重跑独立任务 → 自身回 in_progress", await _status(db, top) == "in_progress",
                    f"top={await _status(db, top)}")
    finally:
        await db.close()

    # 首次执行 backlog 子任务 → 不回写（status 非 done/reviewing），父任务(in_progress)也不动
    await _reactivate_on_redispatch(fresh, parent2, "backlog")
    db = await get_connection()
    try:
        probe.check("首次执行(backlog)不被误回写", await _status(db, fresh) == "backlog",
                    f"fresh={await _status(db, fresh)}")
        probe.check("父任务非 done/reviewing 时不动", await _status(db, parent2) == "in_progress",
                    f"parent2={await _status(db, parent2)}")
    finally:
        await db.close()

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili_react_"))
    try:
        paths = setup_isolated_config(tmp)
        probe = asyncio.run(run_probe(paths))
        n_ok = sum(1 for _, ok, _ in probe.results if ok)
        print("\n" + ("✅ 全部通过" if probe.ok else "❌ 存在失败项"))
        print(f"{n_ok}/{len(probe.results)} 通过")
        sys.exit(0 if probe.ok else 1)
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"[keep] {tmp}")


if __name__ == "__main__":
    main()
