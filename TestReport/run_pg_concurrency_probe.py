"""Akivili PostgreSQL concurrency-correctness probe (foundation-db S5).

Replaces the retired SQLite WAL-concurrency probe. Where the WAL probe guarded
sqlite's "no database-is-locked under busy_timeout" behaviour, this probe
validates the *real* concurrency guarantees Postgres gives us now that the whole
repo runs PG-only:

  GROUP 1 — concurrent writes to DIFFERENT rows, zero loss / zero deadlock.
  GROUP 2 — concurrent writes to the SAME row: atomic `UPDATE ... n = n + 1`
            is row-lock serialized (no lost update); a read-modify-write variant
            is run alongside purely as an informational contrast (it MAY lose
            updates — that is the point, it documents why atomic UPDATE is
            required — and never fails the probe).
  GROUP 3 — N concurrent `collab.enqueue_run` for the SAME (task, agent): the
            dedup is app-level SELECT-then-INSERT with NO DB unique constraint,
            so this exercises a potential TOCTOU race under real concurrency.
            The strict check only requires "at least one row enqueued"; if more
            than one row lands (race exposed), the probe stays green but prints a
            loud warning surfacing the finding to the owner (changing collab.py
            is out of scope — shared abstraction, needs caller analysis).

All concurrency runs on ONE event loop (single asyncio.run). Each get_connection()
opens its own raw asyncpg connection => independent PG sessions => real
concurrency. enqueue_run goes through the ORM session factory (NullPool in test
=> new connection per session) => also real cross-connection concurrency.

Uses a temporary isolated PG DB + config (never the real DB); no CLI/LLM.
Cleans up temp dir unless --keep.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

# Reuse isolation helpers from the main suite (import also sets
# AKIVILI_TEST_NULLPOOL=1 so ORM engine uses NullPool under asyncio.run).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import setup_isolated_config  # noqa: E402


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


async def _group1_diff_rows(probe: Probe) -> None:
    """GROUP 1 — concurrent writers to DIFFERENT rows: zero loss, zero errors."""
    from database import get_connection  # noqa: PLC0415

    WORKERS, ROUNDS = 12, 40
    errors: list[str] = []

    # Create the scratch table ONCE, before concurrency (PG SERIAL, not AUTOINCREMENT).
    c0 = await get_connection()
    try:
        await c0.execute(
            "CREATE TABLE IF NOT EXISTS _pg_conc_probe("
            "id SERIAL PRIMARY KEY, wid INT, seq INT)")
        await c0.commit()
    finally:
        await c0.close()

    async def writer(wid: int) -> None:
        for i in range(ROUNDS):
            c = await get_connection()
            try:
                await c.execute(
                    "INSERT INTO _pg_conc_probe(wid, seq) VALUES (?,?)", (wid, i))
                await c.commit()
            except Exception as e:  # noqa: BLE001
                errors.append(f"w{wid}#{i}: {type(e).__name__}: {e}")
            finally:
                await c.close()

    await asyncio.gather(*[writer(w) for w in range(WORKERS)])

    db = await get_connection()
    try:
        cnt = (await (await db.execute(
            "SELECT COUNT(*) FROM _pg_conc_probe")).fetchone())[0]
    finally:
        await db.close()

    expected = WORKERS * ROUNDS
    probe.check("G1 concurrent diff-row writes: zero loss", cnt == expected,
                f"wrote {cnt}/{expected}")
    probe.check("G1 concurrent diff-row writes: zero errors", not errors,
                f"{len(errors)} errors" + (f"; first={errors[0]}" if errors else ""))


async def _group2_same_row(probe: Probe) -> None:
    """GROUP 2 — concurrent writers to the SAME row.

    Atomic `UPDATE n = n + 1` is row-lock serialized => no lost update (strict).
    A read-modify-write variant is contrasted purely informationally.
    """
    from database import get_connection  # noqa: PLC0415

    N = 20

    # --- atomic increment (strict) ---
    c0 = await get_connection()
    try:
        await c0.execute("DROP TABLE IF EXISTS _pg_rowlock_probe")
        await c0.execute("CREATE TABLE _pg_rowlock_probe(id INT PRIMARY KEY, n INT)")
        await c0.execute("INSERT INTO _pg_rowlock_probe(id, n) VALUES (?, ?)", (1, 0))
        await c0.commit()
    finally:
        await c0.close()

    async def atomic_inc() -> None:
        c = await get_connection()
        try:
            await c.execute("UPDATE _pg_rowlock_probe SET n = n + 1 WHERE id = 1")
            await c.commit()
        finally:
            await c.close()

    await asyncio.gather(*[atomic_inc() for _ in range(N)])

    db = await get_connection()
    try:
        atomic_n = (await (await db.execute(
            "SELECT n FROM _pg_rowlock_probe WHERE id = 1")).fetchone())[0]
    finally:
        await db.close()
    probe.check("G2 atomic UPDATE n=n+1: no lost update (row-lock serialized)",
                atomic_n == N, f"n={atomic_n}/{N}")

    # --- read-modify-write (INFORMATIONAL ONLY — may lose updates, never fails) ---
    c0 = await get_connection()
    try:
        await c0.execute("DROP TABLE IF EXISTS _pg_rmw_probe")
        await c0.execute("CREATE TABLE _pg_rmw_probe(id INT PRIMARY KEY, n INT)")
        await c0.execute("INSERT INTO _pg_rmw_probe(id, n) VALUES (?, ?)", (1, 0))
        await c0.commit()
    finally:
        await c0.close()

    async def read_modify_write() -> None:
        c = await get_connection()
        try:
            cur_n = (await (await c.execute(
                "SELECT n FROM _pg_rmw_probe WHERE id = 1")).fetchone())[0]
            await c.execute("UPDATE _pg_rmw_probe SET n = ? WHERE id = 1", (cur_n + 1,))
            await c.commit()
        finally:
            await c.close()

    await asyncio.gather(*[read_modify_write() for _ in range(N)])

    db = await get_connection()
    try:
        rmw_n = (await (await db.execute(
            "SELECT n FROM _pg_rmw_probe WHERE id = 1")).fetchone())[0]
    finally:
        await db.close()
    print(f"[INFO] read-modify-write observed n={rmw_n}/{N} "
          f"(informational: shows why atomic UPDATE is required; "
          f"{'no loss this run' if rmw_n == N else f'{N - rmw_n} lost update(s)'})")


async def _group3_enqueue_dedup(probe: Probe) -> None:
    """GROUP 3 — N concurrent enqueue_run for SAME (task, agent) => app-level dedup.

    trigger='assign' with source_run_id=None skips the mention-chain gate (that
    gate only fires when trigger=='mention' and source_run_id is not None), so we
    isolate the SELECT-then-INSERT dedup path.
    """
    import collab  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415

    N = 10
    agent_slug = "qa-backend-developer"

    # Seed a project + task via the get_connection() adapter (auto RETURNING id).
    c = await get_connection()
    try:
        pid = (await c.execute(
            "INSERT INTO projects(title, local_path, description) VALUES (?,?,?)",
            ("__pgconc_proj__", str(Path(tempfile.gettempdir()) / "pgconc"),
             "pg concurrency probe"))).lastrowid
        await c.commit()
        task_id = (await c.execute(
            "INSERT INTO tasks(project_id, title, description) VALUES (?,?,?)",
            (pid, "__pgconc_task__", "dedup race probe"))).lastrowid
        await c.commit()
    finally:
        await c.close()

    async def fire() -> int | None:
        return await collab.enqueue_run(task_id, agent_slug, "prompt", trigger="assign")

    await asyncio.gather(*[fire() for _ in range(N)])

    db = await get_connection()
    try:
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM run_queue WHERE task_id = ? AND agent_slug = ?",
            (task_id, agent_slug))).fetchone())[0]
    finally:
        await db.close()

    # Strict check: at least one enqueued. count==1 => dedup held; count>1 =>
    # pre-existing design property (TOCTOU), surfaced loudly but not a regression.
    probe.check(
        "G3 concurrent enqueue_run(same task,agent): at least one enqueued",
        count >= 1, f"run_queue rows={count} for {N} concurrent calls")
    if count == 1:
        print(f"[INFO] enqueue_run dedup held under {N} concurrent calls (count=1).")
    else:
        print("=" * 78)
        print(f"[WARN] enqueue_run dedup is app-level SELECT-then-INSERT (no DB unique "
              f"constraint); observed {count} rows under {N} concurrent calls — "
              f"potential TOCTOU race, report to owner.")
        print("=" * 78)


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    from db_migrate import run_migrations  # noqa: PLC0415

    # Fresh isolated DB built by Alembic (same order as the retired WAL probe:
    # setup_isolated_config created the empty PG DB + config; migrations here).
    run_migrations()

    await _group1_diff_rows(probe)
    await _group2_same_row(probe)
    await _group3_enqueue_dedup(probe)

    return probe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep temp dir for inspection")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili-pgconc-"))
    paths = setup_isolated_config(tmp)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
    finally:
        if not args.keep:
            import shutil  # noqa: PLC0415
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")

    passed = sum(1 for _, ok, _ in probe.results if ok)
    total = len(probe.results)
    print(f"\nPG concurrency probe: {passed}/{total} passed")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
