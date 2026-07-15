"""Akivili 实时 Agent 总览接口探针（/api/runs/agents-overview）。

钉死运行时页「实时 Agent 总览」区域的数据契约：
  - stats：累计口径（total_runs / failed_runs / distinct_agents），不含限流/429 字段。
  - running：正在执行的 Agent（task_runs.status='running'），带项目/任务信息 + 展示名。
  - idle：启用成员里此刻无 running run 的（显示 idle），身份粒度 (project_id, slug)。
  - 分区互斥：一个成员正在跑就不出现在 idle；同 slug 跨项目算不同在岗实例。

隔离库，测完清理。
"""
from __future__ import annotations

import argparse
import asyncio
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


async def seed(paths):
    """两个项目 + 含昵称成员；跨项目共用同一 slug 验在岗实例粒度。返回关键 id。"""
    import agents as agents_mod
    from database import get_connection
    await agents_mod.rescan()
    db = await get_connection()
    try:
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("总览探针·甲", str(paths["project"]), "overview probe A"))
        pid_a = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
            ("总览探针·乙", str(paths["project"]), "overview probe B"))
        pid_b = cur.lastrowid
        # 甲：负责人(星) + 后端架构师(卡芙卡) + 前端(花火)
        roster_a = [
            ("specialized-project-owner", "项目负责人", "星", 1),
            ("engineering-backend-architect", "后端架构师", "卡芙卡", 0),
            ("engineering-frontend-developer", "前端开发者", "花火", 0),
        ]
        # 乙：同一后端架构师 slug（验跨项目算两个在岗实例）+ 一个禁用成员（不应出现）
        roster_b = [
            ("engineering-backend-architect", "后端架构师", "卡芙卡", 0),
        ]
        for pid, roster in [(pid_a, roster_a), (pid_b, roster_b)]:
            for slug, name, nickname, is_leader in roster:
                await db.execute(
                    "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader, enabled) "
                    "VALUES (?,?,?,?,?,?,1)", (pid, slug, name, "🧩", f"你是{name}。", is_leader))
                await db.execute(
                    "INSERT INTO agent_profiles (slug, provider_id, nickname) VALUES (?,?,?) "
                    "ON CONFLICT(slug) DO UPDATE SET nickname=excluded.nickname",
                    (slug, "", nickname))
        # 甲：一个禁用成员，不应出现在 idle
        await db.execute(
            "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader, enabled) "
            "VALUES (?,?,?,?,?,0,0)", (pid_a, "qa-test-engineer", "测试工程师", "🧩", "你是测试。"))

        # 甲的顶层任务 + 一个子任务
        t = await db.execute(
            "INSERT INTO tasks (project_id, title, status) VALUES (?,?,?)",
            (pid_a, "上线收口", "in_progress"))
        top_a = t.lastrowid
        t = await db.execute(
            "INSERT INTO tasks (project_id, title, status, parent_task_id) VALUES (?,?,?,?)",
            (pid_a, "补映射子任务", "in_progress", top_a))
        sub_a = t.lastrowid
        await db.commit()
        return {"pid_a": pid_a, "pid_b": pid_b, "top_a": top_a, "sub_a": sub_a}
    finally:
        await db.close()


async def _mk_run(task_id, slug, status):
    """建一个 task_runs 行（指定状态），返回 id。"""
    from database import get_connection
    db = await get_connection()
    try:
        c = await db.execute(
            "INSERT INTO task_runs (task_id, agent_slug, provider_id, status, started_at) "
            "VALUES (?,?,?,?, datetime('now'))", (task_id, slug, "p", status))
        rid = c.lastrowid
        await db.commit()
        return rid
    finally:
        await db.close()


async def _mk_run_at(task_id, slug, status, started_offset, dur_seconds=None):
    """建一个 task_runs 行，started_at = now + started_offset（如 '-40 days'）；
    dur_seconds 给定时 ended_at = started_at + 该秒数。用于测窗口过滤与总时长。"""
    from database import get_connection
    db = await get_connection()
    try:
        if dur_seconds is not None:
            ended = f"datetime('now', ?, '+{int(dur_seconds)} seconds')"
            c = await db.execute(
                f"INSERT INTO task_runs (task_id, agent_slug, provider_id, status, started_at, ended_at) "
                f"VALUES (?,?,?,?, datetime('now', ?), {ended})",
                (task_id, slug, "p", status, started_offset, started_offset))
        else:
            c = await db.execute(
                "INSERT INTO task_runs (task_id, agent_slug, provider_id, status, started_at) "
                "VALUES (?,?,?,?, datetime('now', ?))",
                (task_id, slug, "p", status, started_offset))
        rid = c.lastrowid
        await db.commit()
        return rid
    finally:
        await db.close()


async def run_probe(paths, keep):
    probe = Probe()
    await bootstrap_backend(paths)
    from routes import runs as runs_route

    ids = await seed(paths)

    # 造窗口内历史：甲 top 上后端架构师一条成功(时长10s)、一条失败(时长20s)
    await _mk_run_at(ids["top_a"], "engineering-backend-architect", "succeeded", "-1 days", dur_seconds=10)
    await _mk_run_at(ids["top_a"], "engineering-frontend-developer", "failed", "-2 days", dur_seconds=20)
    # 造窗口外历史：40 天前一条成功(时长100s)——默认 30 天窗口应排除，扩到 180 天应计入
    await _mk_run_at(ids["top_a"], "qa-test-engineer", "succeeded", "-40 days", dur_seconds=100)
    # 造实时：甲子任务上后端架构师「正在运行」（无 ended_at，不计入总时长）
    running_rid = await _mk_run(ids["sub_a"], "engineering-backend-architect", "running")

    ov = await runs_route.agents_overview()   # 默认 30 天

    # --- stats：窗口口径（默认 30 天），不含限流字段 ---
    st = ov.get("stats", {})
    probe.check("stats 含 total_runs/failed_runs/distinct_agents/total_run_seconds",
                {"total_runs", "failed_runs", "distinct_agents", "total_run_seconds"}.issubset(st.keys()),
                f"keys={sorted(st.keys())}")
    probe.check("默认窗口 window_days=30",
                ov.get("window_days") == 30, f"window_days={ov.get('window_days')}")
    probe.check("total_runs 计入 30 天窗口内 run（成功+失败+运行中=3，排除40天前那条）",
                st.get("total_runs") == 3, f"total_runs={st.get('total_runs')}")
    probe.check("failed_runs 只计窗口内失败（1 条）",
                st.get("failed_runs") == 1, f"failed_runs={st.get('failed_runs')}")
    probe.check("distinct_agents 去重窗口内（后端架构师+前端=2，排除40天前的测试）",
                st.get("distinct_agents") == 2, f"distinct_agents={st.get('distinct_agents')}")
    probe.check("total_run_seconds 为窗口内已结束 run 时长和（10+20=30，运行中/窗口外不计）",
                abs(st.get("total_run_seconds", 0) - 30) < 1.0,
                f"total_run_seconds={st.get('total_run_seconds')}")
    probe.check("不再暴露限流/429 字段（rate_limit* / by_fail_reason 已移除）",
                not any(k in ov for k in ("rate_limited_runs", "rate_limit_hit_rate", "by_fail_reason")),
                f"顶层键={sorted(ov.keys())}")

    # --- 时间窗口筛选：扩到 180 天应把 40 天前那条计入 ---
    ov180 = await runs_route.agents_overview(days=180)
    st180 = ov180["stats"]
    probe.check("window_days=180 时把 40 天前的 run 计入（total_runs=4）",
                ov180.get("window_days") == 180 and st180["total_runs"] == 4,
                f"window_days={ov180.get('window_days')} total_runs={st180['total_runs']}")
    probe.check("window_days=180 总时长含 40 天前那条（10+20+100=130）",
                abs(st180["total_run_seconds"] - 130) < 1.0,
                f"total_run_seconds={st180['total_run_seconds']}")
    probe.check("days 参数越界被 clamp（0→1，10000→365，上限为最近一年）",
                (await runs_route.agents_overview(days=0))["window_days"] == 1
                and (await runs_route.agents_overview(days=10000))["window_days"] == 365,
                "clamp 生效")

    # --- running：正在运行的 Agent，带项目/任务 + 展示名 ---
    run = ov.get("running", [])
    probe.check("running 恰 1 条（当前唯一运行中 run）",
                ov.get("running_count") == 1 and len(run) == 1,
                f"running_count={ov.get('running_count')} len={len(run)}")
    if run:
        r0 = run[0]
        need = {"task_run_id", "agent_slug", "agent_display", "project_id",
                "project_title", "task_id", "task_title", "is_subtask", "started_at"}
        probe.check("running 项含卡片渲染所需全部字段",
                    need.issubset(r0.keys()), f"缺={need - set(r0.keys()) or '无'}")
        probe.check("running 项 agent_display 为展示名「昵称（角色名）」非裸 slug",
                    r0.get("agent_display") == "卡芙卡（后端架构师）",
                    f"agent_display={r0.get('agent_display')!r}")
        probe.check("running 项带正确项目信息（甲）",
                    r0.get("project_id") == ids["pid_a"] and r0.get("project_title") == "总览探针·甲",
                    f"project_id={r0.get('project_id')} title={r0.get('project_title')!r}")
        probe.check("running 项识别为子任务（is_subtask=True）",
                    r0.get("is_subtask") is True and r0.get("task_id") == ids["sub_a"],
                    f"is_subtask={r0.get('is_subtask')} task_id={r0.get('task_id')}")

    # --- idle：启用成员里无 running run 的 ---
    idle = ov.get("idle", [])
    idle_keys = {(a["project_id"], a["agent_slug"]) for a in idle}
    # 甲的后端架构师正在跑 → 不在 idle；乙的后端架构师（另一在岗实例）空闲 → 在 idle
    probe.check("正在跑的在岗实例(甲·后端架构师)不出现在 idle",
                (ids["pid_a"], "engineering-backend-architect") not in idle_keys,
                f"idle_keys={sorted(idle_keys)}")
    probe.check("跨项目同 slug 的空闲在岗实例(乙·后端架构师)出现在 idle",
                (ids["pid_b"], "engineering-backend-architect") in idle_keys,
                f"idle_keys={sorted(idle_keys)}")
    probe.check("甲·负责人(星)空闲 → 在 idle",
                (ids["pid_a"], "specialized-project-owner") in idle_keys,
                f"idle_keys={sorted(idle_keys)}")
    probe.check("禁用成员(测试工程师)不出现在 idle",
                all(a["agent_slug"] != "qa-test-engineer" for a in idle),
                f"idle_slugs={[a['agent_slug'] for a in idle]}")
    if idle:
        probe.check("idle 项 agent_display 为展示名且含 is_leader/project 字段",
                    all({"agent_slug", "agent_display", "project_id", "project_title", "is_leader"}.issubset(a.keys())
                        for a in idle),
                    "字段完整")
        leader = next((a for a in idle if a["agent_slug"] == "specialized-project-owner"), None)
        probe.check("idle 负责人 is_leader=True 且展示名为「星（项目负责人）」",
                    leader is not None and leader["is_leader"] is True
                    and leader["agent_display"] == "星（项目负责人）",
                    f"leader={leader}")

    # --- 分区互斥不变量：running 与 idle 无交集 ---
    run_keys = {(r["project_id"], r["agent_slug"]) for r in run}
    probe.check("running 与 idle 分区互斥（无同一在岗实例同时出现）",
                run_keys.isdisjoint(idle_keys), f"交集={run_keys & idle_keys}")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili-overview-"))
    paths = setup_isolated_config(tmp)
    try:
        probe = asyncio.run(run_probe(paths, args.keep))
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"Kept temp dir: {tmp}")
    total = len(probe.results)
    passed = sum(1 for r in probe.results if r[1])
    print(f"\n=== agents overview probe: {passed}/{total} ===")
    return 0 if probe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
