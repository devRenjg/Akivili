"""Akivili WAL + connection-consolidation regression probe (foundation-db S1).

Guards the S1 outcome so later data-layer work (S2 Alembic / S3 ORM / S4-S5 PG)
cannot silently regress it:
  1. get_connection() returns a connection with journal_mode=WAL,
     busy_timeout = config.db_busy_timeout_ms, foreign_keys=ON.
  2. init_db() leaves the freshly-built DB in WAL mode (no "first get_connection
     flips WAL" timing gap).
  3. Concurrent writers through get_connection() never hit "database is locked"
     (the exact failure WAL + busy_timeout eliminates).
  4. Consolidation guard: whole-repo source has aiosqlite.connect ONLY in
     database.py (init builder + get_connection factory). Any resurrected bypass
     fails this probe.

Uses a temporary config/DB/workspace (never the real jianagency.db); no CLI/LLM.
Cleans up temp dir unless --keep.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
from pathlib import Path

# Reuse isolation helpers from the main suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import BACKEND, setup_isolated_config  # noqa: E402


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


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    import config  # noqa: PLC0415
    import database  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415

    # Fresh isolated DB built by init_db().
    await database.init_db()
    expected_bt = config.load_settings().db_busy_timeout_ms

    # --- Check 1: get_connection() applies WAL + busy_timeout + foreign_keys ---
    db = await get_connection()
    try:
        jm = (await (await db.execute("PRAGMA journal_mode")).fetchone())[0]
        bt = (await (await db.execute("PRAGMA busy_timeout")).fetchone())[0]
        fk = (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0]
    finally:
        await db.close()
    probe.check("get_connection journal_mode=WAL", str(jm).lower() == "wal", f"got {jm}")
    probe.check("get_connection busy_timeout=config", bt == expected_bt,
                f"got {bt}, expected {expected_bt}")
    probe.check("get_connection foreign_keys=ON", fk == 1, f"got {fk}")

    # --- Check 2: init_db() left the DB itself in WAL mode (timing-gap guard) ---
    import aiosqlite  # noqa: PLC0415
    raw = await aiosqlite.connect(paths["db"])
    try:
        jm2 = (await (await raw.execute("PRAGMA journal_mode")).fetchone())[0]
    finally:
        await raw.close()
    probe.check("init_db leaves DB in WAL", str(jm2).lower() == "wal", f"got {jm2}")

    # --- Check 3: concurrent writers never hit "database is locked" ---
    WORKERS, ROUNDS = 12, 40
    errors: list[str] = []

    async def writer(wid: int) -> None:
        for i in range(ROUNDS):
            c = await get_connection()
            try:
                await c.execute(
                    "CREATE TABLE IF NOT EXISTS _wal_probe("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, wid INT, seq INT)")
                await c.execute("INSERT INTO _wal_probe(wid, seq) VALUES (?,?)", (wid, i))
                await c.commit()
            except Exception as e:  # noqa: BLE001
                errors.append(f"w{wid}#{i}: {type(e).__name__}: {e}")
            finally:
                await c.close()

    await asyncio.gather(*[writer(w) for w in range(WORKERS)])
    db = await get_connection()
    try:
        cnt = (await (await db.execute("SELECT COUNT(*) FROM _wal_probe")).fetchone())[0]
    finally:
        await db.close()
    locked = [e for e in errors if "locked" in e.lower()]
    probe.check("concurrent writes all succeeded", cnt == WORKERS * ROUNDS,
                f"wrote {cnt}/{WORKERS * ROUNDS}")
    probe.check("zero database-is-locked under concurrency", not locked,
                f"{len(locked)} locked errors" if locked else f"0 locked in {len(errors)} total errors")

    # --- Check 4: consolidation guard — aiosqlite.connect only in database.py ---
    hits: list[str] = []
    for py in BACKEND.rglob("*.py"):
        if "TestReport" in py.parts or py.name.startswith("test_"):
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in re.finditer(r"aiosqlite\.connect\(", text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{py.relative_to(BACKEND).as_posix()}:{line}")
    non_db = [h for h in hits if not h.startswith("database.py:")]
    probe.check("aiosqlite.connect confined to database.py", not non_db,
                f"stray: {non_db}" if non_db else f"only database.py ({len(hits)} sites)")
    probe.check("database.py has exactly 2 connect sites (init + factory)",
                len([h for h in hits if h.startswith("database.py:")]) == 2,
                f"database.py sites: {[h for h in hits if h.startswith('database.py:')]}")

    return probe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep temp dir for inspection")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="akivili-wal-"))
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
    print(f"\nWAL concurrency probe: {passed}/{total} passed")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
