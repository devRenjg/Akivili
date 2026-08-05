"""Akivili 执行面 worker 进程入口（worker-split-minimal 组 1，对标 Multica daemon）。

**为什么存在**：平滑重启的根因是「Agent 执行寿命 = API 进程寿命」——执行层（collab 的
`_loop`/`_process_one`/`_run_one` + CLI 子进程）原本跑在 API 进程里，改后端代码重启 API
就会连带杀掉在跑的 CLI 子进程。本 worker 把**队列路径**执行层搬到独立进程：API 只入队+查询，
worker 领队列+执行，二者经 DB（run_queue/task_runs）交接、可各自独立重启。

**做法 A 边界**：`routes/runs.py` 的 SSE 直连对话路径仍驻留 API 进程（不在本进程）；
因此「重启 API 不打断执行」仅覆盖**队列路径**。直连路径迁移归后续做法 B change。

**reclaim 按路径切分**（组 1 决策）：本进程启动 reclaim 只清**队列路径**孤儿（有 run_queue
行的 run）；API 死掉的直连路径孤儿交给周期 sweep（idle-based，不误杀在产出的 run）兜底。
这样「只重启 API / 只重启 worker / 两者都重启」三种场景都不会误杀另一进程正在跑的 run。
（完全按进程代际 runtime_id 整批回收 = 组 3，本组不引新列。）

**单实例**：本组只跑单 worker。多 worker 的原子领取由组 0 的 `_claim_one` CAS+SKIP LOCKED
预留，但并发多 worker 不在本组。启动纪律同 API：单实例，重启前杀净旧 worker 进程。
"""
import asyncio

from db_migrate import run_migrations
import collab as collab_mod


async def _amain() -> None:
    # 迁移：与 API 一致最先 upgrade head。run_migrations 自带 pg advisory lock（S5b），
    # API 与 worker 并发启动时抢同一把锁、串行迁移，谁先拿锁谁迁移、另一方见 head 空跑——
    # 故这里无条件调用是安全的（不会与 API 的迁移竞争损坏 alembic_version）。
    action = await asyncio.to_thread(run_migrations)
    print(f"[worker] alembic upgrade head done (action={action})", flush=True)

    # 组 2 进程树 containment：创建 Job Object（KILL_ON_JOB_CLOSE）。此后本进程起的 CLI 子进程
    # 都会被 contain() 加入该 Job——worker 进程一死（含强杀/崩溃），OS 连带清理全部 CLI 子进程，
    # 保证「死 worker 不会再有子进程写库/写文件」。失败自动降级（依赖 kill_run 兜底），不阻断启动。
    from executor.containment import init_containment  # noqa: PLC0415
    init_containment()

    # 回收上一代 worker 遗留的**队列路径**孤儿 running（仅本进程负责的路径）。
    # 只在 start_loop 之前调用，此刻本进程 _running/_RUN_PIDS 必为空、_loop 尚未领新活。
    n = await collab_mod.reclaim_orphan_runs(scope="queue")
    if n:
        print(f"[worker] reclaim 队列路径孤儿 running {n} 条", flush=True)

    # 拉起执行面后台循环：_loop（领队列+并发池执行）+ _orphan_sweep_loop（运行期孤儿巡检，
    # 含直连路径 idle 兜底）。start_loop 幂等，本进程只调一次。
    collab_mod.start_loop()
    print("[worker] 执行面已启动（_loop + orphan_sweep），常驻等待队列", flush=True)

    # 常驻：本进程主业是跑后台循环，主协程挂起不退出。
    await asyncio.Event().wait()


def main() -> None:
    try:
        asyncio.run(_amain())
    except (KeyboardInterrupt, SystemExit):
        print("[worker] 收到退出信号，worker 停止", flush=True)


if __name__ == "__main__":
    main()
