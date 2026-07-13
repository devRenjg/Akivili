"""Akivili orphan-run reclaim probe.

Verifies collab.reclaim_orphan_runs() in isolation:
  1. run_queue rows stuck at 'running' (orphans left by kill+restart) are reclaimed to 'failed'.
  2. Non-running rows (queued/done/failed) are left untouched.
  3. A 'task_failed' activity note is logged for each affected task (deduped per task).
  4. The call is idempotent — a second run reclaims nothing.

Context: run_queue lifecycle is driven by collab's in-memory state (_running set +
_process_one coroutine). After a process restart that state is gone, so any leftover
'running' row can never be finalized and progress.py mis-reads it as "task still executing".
reclaim_orphan_runs() runs at startup (before start_loop) to clear that false state.

Uses a temporary config/DB/workspace under C:\\tmp (never the real jianagency.db).
No real CLI/LLM is invoked. Cleans up temp dir unless --keep.
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
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


async def _seed(paths: dict) -> dict:
    """Seed a project + one top-level task + one parent/sub pair, and four run_queue rows:
      #A running on the top-level task   (orphan → failed)
      #B running on the sub task         (orphan → failed)
      #C done   on the top-level task    (untouched)
      #D queued on the sub task          (untouched — not running, so not an orphan here)
    Plus three task_runs rows (the data source for the detail-page run list):
      #R1 running (orphan → killed), #R2 running (orphan → killed), #R3 succeeded (untouched)
    Returns the seeded run_queue ids and task_runs ids keyed by label.
    """
    from database import get_connection  # noqa: PLC0415

    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("__orphan_probe_project__", str(paths["project"]), "orphan reclaim probe"),
        )
        pid = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO tasks (title, status, project_id) VALUES (?,?,?)",
            ("__orphan_top__", "in_progress", pid))
        top_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO tasks (title, status, project_id) VALUES (?,?,?)",
            ("__orphan_parent__", "in_progress", pid))
        parent_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO tasks (title, status, project_id, parent_task_id) VALUES (?,?,?,?)",
            ("__orphan_sub__", "done", pid, parent_id))
        sub_id = cur.lastrowid

        ids = {}
        for label, task_id, slug, status, is_leader in [
            ("A", top_id, "x-leader", "running", 1),
            ("B", sub_id, "y-worker", "running", 0),
            ("C", top_id, "z-leader", "done", 1),
            ("D", sub_id, "w-worker", "queued", 0),
        ]:
            cur = await db.execute(
                "INSERT INTO run_queue (task_id, agent_slug, trigger, is_leader, prompt, status) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, slug, "collaborate", is_leader, "", status))
            ids[label] = cur.lastrowid

        tr = {}
        for label, task_id, status in [
            ("R1", top_id, "running"),
            ("R2", sub_id, "running"),
            ("R3", top_id, "succeeded"),
        ]:
            cur = await db.execute(
                "INSERT INTO task_runs (task_id, agent_slug, status) VALUES (?,?,?)",
                (task_id, "x-leader", status))
            tr[label] = cur.lastrowid
        await db.commit()
        return {"pid": pid, "top_id": top_id, "parent_id": parent_id, "sub_id": sub_id,
                "rq": ids, "tr": tr}
    finally:
        await db.close()


async def _statuses(ids: dict, table: str = "run_queue") -> dict:
    from database import get_connection  # noqa: PLC0415
    db = await get_connection()
    try:
        out = {}
        for label, rid in ids.items():
            row = await (await db.execute(f"SELECT status FROM {table} WHERE id=?", (rid,))).fetchone()
            out[label] = row["status"] if row else None
        return out
    finally:
        await db.close()


async def _task_failed_notes(task_ids: list[int]) -> int:
    from database import get_connection  # noqa: PLC0415
    ph = ",".join("?" for _ in task_ids)
    db = await get_connection()
    try:
        row = await (await db.execute(
            f"SELECT COUNT(*) c FROM activities WHERE action='task_failed' AND task_id IN ({ph})",
            tuple(task_ids))).fetchone()
        return row["c"] if row else 0
    finally:
        await db.close()


async def run(paths: dict) -> Probe:
    import collab  # noqa: PLC0415

    p = Probe()
    seed = await _seed(paths)
    rq, tr = seed["rq"], seed["tr"]

    before_q = await _statuses(rq)
    before_r = await _statuses(tr, "task_runs")
    p.check("seed has two running run_queue orphans",
            sum(1 for s in before_q.values() if s == "running") == 2, str(before_q))
    p.check("seed has two running task_runs orphans",
            sum(1 for s in before_r.values() if s == "running") == 2, str(before_r))

    n = await collab.reclaim_orphan_runs()
    after_q = await _statuses(rq)
    after_r = await _statuses(tr, "task_runs")

    # 2 run_queue + 2 task_runs = 4 orphans reclaimed
    p.check("reclaim returns total count across both layers", n == 4, f"returned={n}")
    p.check("run_queue orphan A → failed", after_q["A"] == "failed", f"A={after_q['A']}")
    p.check("run_queue orphan B → failed", after_q["B"] == "failed", f"B={after_q['B']}")
    p.check("run_queue done C untouched", after_q["C"] == "done", f"C={after_q['C']}")
    p.check("run_queue queued D untouched", after_q["D"] == "queued", f"D={after_q['D']}")
    # R1 在 top_id（in_progress，未收尾）→ killed；R2 在 sub_id（done，已成功）→ succeeded
    # （不把已完成任务的成果 run 误标 killed，避免污染卡片「执行完成」与 solved_tasks 计数）
    p.check("task_runs orphan R1 (unfinished task) → killed", after_r["R1"] == "killed", f"R1={after_r['R1']}")
    p.check("task_runs orphan R2 (done task) → succeeded", after_r["R2"] == "succeeded", f"R2={after_r['R2']}")
    p.check("task_runs succeeded R3 untouched", after_r["R3"] == "succeeded", f"R3={after_r['R3']}")

    # 仅未收尾任务(top_id) 记「回收失败」活动；已 done 的 sub_id 不记（它是成功任务）
    top_notes = await _task_failed_notes([seed["top_id"]])
    sub_notes = await _task_failed_notes([seed["sub_id"]])
    p.check("unfinished task gets recycle note", top_notes >= 1, f"top_notes={top_notes}")
    p.check("done task gets NO misleading fail note", sub_notes == 0, f"sub_notes={sub_notes}")

    n2 = await collab.reclaim_orphan_runs()
    p.check("idempotent: second call reclaims nothing", n2 == 0, f"returned={n2}")

    return p


async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep temporary directory")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili-orphan-", dir=r"C:\tmp"))
    paths = setup_isolated_config(tmp)
    await bootstrap_backend(paths)
    try:
        p = await run(paths)
    finally:
        if args.keep:
            print(f"Kept temp dir: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in p.results if r[1])
    print(f"\nOrphan reclaim probe: {passed}/{len(p.results)} passed")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
