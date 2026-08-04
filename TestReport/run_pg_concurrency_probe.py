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
  GROUP 3 — N concurrent `collab.enqueue_run` for the SAME (task, agent): asserts
            EXACTLY one row lands. dedup is now two-layer: app-level pre-check
            (SELECT) + DB partial unique index uq_run_queue_active (migration 003)
            backing INSERT ... ON CONFLICT DO NOTHING. The DB index makes the
            TOCTOU race impossible under real concurrency, so this is a hard
            "exactly 1" guarantee (was "at least 1 + warn" before 003).
  GROUP 4 — N concurrent `collab._claim_one` over M queued runs: asserts each run
            is claimed EXACTLY once (no double-execution) with no misses. Guards
            _claim_one's FOR UPDATE SKIP LOCKED + status='queued' CAS (mirrors
            Multica ClaimAgentTask) — the safety belt for "new+old worker briefly
            coexist during graceful restart / future multi-worker".

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

    # 严格断言：N 个并发 enqueue 同 (task,agent) 恰好进 1 条。
    # 迁移 003 的部分唯一索引 uq_run_queue_active 已把去重从应用层软 dedup 升级为 DB 硬保证，
    # TOCTOU 竞态由 ON CONFLICT DO NOTHING + 唯一索引在 DB 层消除——故这里从原「至少 1 条 + 告警」
    # 收紧为「恰好 1 条」。若 >1，说明索引/ON CONFLICT 兜底失效（真回归），必须红。
    probe.check(
        "G3 并发 enqueue_run(同 task,agent) 恰好进 1 条（DB 唯一索引兜底 TOCTOU）",
        count == 1, f"run_queue rows={count} for {N} concurrent calls（期望 1）")


async def _group4_claim_no_double(probe: Probe) -> None:
    """GROUP 4 — N concurrent _claim_one over M queued runs => 每行恰好被领一次。

    验证 _claim_one 的并发领取安全（FOR UPDATE SKIP LOCKED + CAS，对标 Multica
    ClaimAgentTask）：多个并发领取者同时抢队列，绝不能把同一个 run 领两次（双执行），
    也不能漏领（本可领的 run 没被领走）。

    造 M 个不同 (task) 的 queued run（不同 task 规避 uq_run_queue_active 的同 task,agent
    活跃唯一约束——我们要测的是「领取」并发，不是「入队」去重，后者已由 G3 覆盖）。
    起 N=M+K 个并发 _claim_one：M 个应各领到一个不同 run，多出的 K 个应领到 None（队列空）。
    断言：领到的 run_queue id 集合大小 == M（无重复）、且 == 全部入队 id（无遗漏）。
    """
    import collab  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415

    M = 8           # queued run 数
    K = 4           # 多余的领取者（应领到 None）
    agent_slug = "qa-backend-developer"

    c = await get_connection()
    try:
        pid = (await c.execute(
            "INSERT INTO projects(title, local_path, description) VALUES (?,?,?)",
            ("__pgclaim_proj__", str(Path(tempfile.gettempdir()) / "pgclaim"),
             "pg claim race probe"))).lastrowid
        await c.commit()
        qids: list[int] = []
        for i in range(M):
            tid = (await c.execute(
                "INSERT INTO tasks(project_id, title, description) VALUES (?,?,?)",
                (pid, f"__pgclaim_task_{i}__", "claim race probe"))).lastrowid
            await c.commit()
            # 直接插 run_queue（绕过 enqueue_run 的 mention/dedup 逻辑，隔离领取路径）
            qid = (await c.execute(
                "INSERT INTO run_queue(task_id, agent_slug, trigger, prompt, status) "
                "VALUES (?,?,?,?,'queued')",
                (tid, agent_slug, "assign", f"prompt {i}"))).lastrowid
            await c.commit()
            qids.append(qid)
    finally:
        await c.close()

    # 领取前 run_queue 里已有的 queued 行（前序 group 可能遗留，如 G3 的 1 条）——
    # 这些也会被 _claim_one 领走，纳入基线，断言才对「队列非空起点」鲁棒。
    c = await get_connection()
    try:
        pre = (await (await c.execute(
            "SELECT id FROM run_queue WHERE status='queued'")).fetchall())
    finally:
        await c.close()
    pre_queued = {r[0] for r in pre}           # 含我造的 M 条 + 任何遗留
    expected = pre_queued                       # 全部 queued 都应恰好被领一次
    K_eff = (M + K) - len(expected)            # 扣除后真正「多余」的领取者数

    async def claim() -> int | None:
        item = await collab._claim_one()
        return item["id"] if item else None

    claimed = await asyncio.gather(*[claim() for _ in range(M + K)])
    got = [x for x in claimed if x is not None]
    got_set = set(got)

    # 无重复：领到的 id 无重复（同一 run 未被领两次 = 无双执行）——CAS 的核心保证
    probe.check(
        "G4 并发 _claim_one 无重复领取（无双执行）",
        len(got) == len(got_set),
        f"claimed={sorted(got)}（{M + K} 个并发领取者，期望互不重复）")
    # 我造的 M 条必须全部被领走（无遗漏）；并与运行前所有 queued 一致
    probe.check(
        "G4 并发 _claim_one 无遗漏（所有 queued 全部领走，含我造的 M 条）",
        got_set == expected and set(qids) <= got_set,
        f"got={sorted(got_set)} expected={sorted(expected)} mine={sorted(qids)}")
    # 领到的总数 == 运行前 queued 总数；多余领取者领到 None（非报错/非重复）
    probe.check(
        "G4 领取数==队列非空起点数，多余领取者领到 None",
        len(got) == len(expected),
        f"non-None claims={len(got)}（期望={len(expected)}，多余 K_eff={K_eff} 应领到 None）")


async def run_probe(paths: dict, keep: bool) -> Probe:
    probe = Probe()
    from db_migrate import run_migrations  # noqa: PLC0415

    # Fresh isolated DB built by Alembic (same order as the retired WAL probe:
    # setup_isolated_config created the empty PG DB + config; migrations here).
    run_migrations()

    await _group1_diff_rows(probe)
    await _group2_same_row(probe)
    await _group3_enqueue_dedup(probe)
    await _group4_claim_no_double(probe)

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
