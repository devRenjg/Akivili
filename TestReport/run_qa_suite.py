"""Akivili isolated QA suite.

Runs against a temporary config/database/workspace under C:\\tmp by default.
It does not use the real backend/config.json or the real SQLite database.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
REPORT_DIR = ROOT / "TestReport"

# 管理员凭证与 auth.seed_admin 一致：从环境变量读，缺省用占位。
import os as _os
ADMIN_USER = _os.environ.get("AKIVILI_ADMIN_USER", "admin")
ADMIN_PASS = _os.environ.get("AKIVILI_ADMIN_PASSWORD", "changeme")


@dataclass
class CaseResult:
    name: str
    status: str
    category: str
    severity: str = "P1"
    detail: str = ""
    elapsed_ms: float = 0.0


@dataclass
class QaState:
    results: list[CaseResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool, category: str, severity: str = "P1",
            detail: str = "", elapsed_ms: float = 0.0) -> None:
        self.results.append(CaseResult(
            name=name,
            status="PASS" if ok else "FAIL",
            category=category,
            severity=severity,
            detail=detail,
            elapsed_ms=round(elapsed_ms, 2),
        ))

    def note(self, text: str) -> None:
        self.notes.append(text)


def write_agent(path: Path, name: str, desc: str, emoji: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {desc}\nemoji: {emoji}\ncolor: blue\n---\n\n{body}\n",
        encoding="utf-8",
    )


def write_skill(path: Path, name: str, desc: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def setup_isolated_config(tmp: Path) -> dict[str, Path]:
    agents_dir = tmp / "agents"
    memory_dir = tmp / "memory"
    skills_dir = tmp / "skills"
    project_dir = tmp / "workspace"
    db_path = tmp / "qa.db"
    project_dir.mkdir(parents=True, exist_ok=True)

    write_agent(
        agents_dir / "management" / "specialized-project-owner.md",
        "项目负责人",
        "统筹任务、拆解目标、按成员技能委派并收尾",
        "🧭",
        "你是团队负责人，只负责统筹、委派和验收，不直接做具体实现。",
    )
    write_agent(
        agents_dir / "engineering" / "qa-backend-developer.md",
        "后端开发者",
        "FastAPI、数据库、接口实现",
        "🛠️",
        "你负责后端接口、数据模型和服务端测试。",
    )
    write_agent(
        agents_dir / "engineering" / "qa-frontend-developer.md",
        "前端开发者",
        "Vue、Element Plus、交互体验",
        "🎨",
        "你负责前端页面、状态和交互。",
    )
    write_agent(
        agents_dir / "testing" / "qa-tester.md",
        "测试专员",
        "验收、接口测试、安全边界",
        "✅",
        "你负责验证、风险识别和测试结论。",
    )
    write_agent(
        agents_dir / "security" / "qa-security-engineer.md",
        "安全工程师",
        "鉴权、密钥、路径穿越",
        "🔐",
        "你负责安全审计和风险证据。",
    )
    (agents_dir / "README.md").write_text("not an agent", encoding="utf-8")
    (agents_dir / "examples").mkdir(parents=True, exist_ok=True)
    write_agent(
        agents_dir / "examples" / "ignored.md",
        "不应导入",
        "examples should be ignored",
        "x",
        "ignored",
    )

    write_skill(
        skills_dir / "backend-api-test.md",
        "后端接口测试",
        "验证 API 状态码、响应体与数据库副作用",
        "优先使用隔离数据库和明确断言验证接口行为。",
    )
    write_skill(
        skills_dir / "security-boundary-check.md",
        "安全边界检查",
        "验证鉴权、密钥脱敏、路径穿越和越权写入",
        "先确认资产边界，再用最小化 payload 验证拒绝行为。",
    )

    cfg = {
        "db_path": str(db_path),
        "agent_library_dir": str(agents_dir),
        "memory_dir": str(memory_dir),
        "skills_dir": str(skills_dir),
        "host": "127.0.0.1",
        "port": 8100,
        "providers": [],
        "default_provider_id": "",
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    sys.path.insert(0, str(BACKEND))
    import config  # noqa: PLC0415

    config.CONFIG_FILE = cfg_path
    return {
        "tmp": tmp,
        "config": cfg_path,
        "agents": agents_dir,
        "memory": memory_dir,
        "skills": skills_dir,
        "project": project_dir,
        "db": db_path,
    }


async def bootstrap_backend(paths: dict[str, Path]):
    import database  # noqa: PLC0415
    import auth as auth_mod  # noqa: PLC0415
    import agents as agents_mod  # noqa: PLC0415
    import skills as skills_mod  # noqa: PLC0415
    import memory as memory_mod  # noqa: PLC0415
    import main  # noqa: PLC0415

    await database.init_db()
    await auth_mod.seed_admin()
    memory_mod.ensure_memory_dir()
    skills_mod.ensure_skills_dir()
    await agents_mod.rescan()
    await skills_mod.rescan()
    return main.app


async def timed(coro):
    start = time.perf_counter()
    result = await coro
    return result, (time.perf_counter() - start) * 1000


async def count_db(sql: str, params: tuple = ()) -> int:
    from database import get_connection  # noqa: PLC0415

    db = await get_connection()
    try:
        row = await (await db.execute(sql, params)).fetchone()
        return int(row[0]) if row else 0
    finally:
        await db.close()


async def fetch_one(sql: str, params: tuple = ()):
    from database import get_connection  # noqa: PLC0415

    db = await get_connection()
    try:
        row = await (await db.execute(sql, params)).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def run_suite(paths: dict[str, Path], include_live: bool) -> QaState:
    state = QaState()
    app = await bootstrap_backend(paths)
    transport = httpx.ASGITransport(app=app)
    anon = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    admin = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    try:
        r, ms = await timed(anon.get("/api/projects"))
        state.add("匿名可读取项目列表", r.status_code == 200, "auth", "P0", f"HTTP {r.status_code}", ms)

        r, ms = await timed(anon.post("/api/projects", json={
            "title": "__qa_anon_forbidden__",
            "local_path": str(paths["project"]),
            "description": "",
        }))
        state.add("匿名创建项目被拦截", r.status_code in (401, 403), "auth", "P0",
                  f"HTTP {r.status_code}; OpenSpec 写 403，当前实现未登录返回 401", ms)
        if r.status_code == 401:
            state.note("规格差异：OpenSpec 的匿名禁止写期望 403，当前实现对未登录返回 401、非管理员返回 403。")

        r, ms = await timed(admin.post("/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"}))
        state.add("错误密码登录失败", r.status_code == 401, "auth", "P0", f"HTTP {r.status_code}", ms)

        r, ms = await timed(admin.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}))
        set_cookie = r.headers.get("set-cookie", "")
        state.add("管理员登录成功且 cookie HttpOnly", r.status_code == 200 and "httponly" in set_cookie.lower(),
                  "auth", "P0", f"HTTP {r.status_code}; Set-Cookie has HttpOnly={('httponly' in set_cookie.lower())}", ms)

        r, ms = await timed(admin.post("/api/projects", json={
            "title": "__qa_invalid_path__",
            "local_path": str(paths["tmp"] / "missing"),
            "description": "",
        }))
        state.add("无效项目路径被拒绝", r.status_code == 400, "project", "P0", f"HTTP {r.status_code}", ms)

        r, ms = await timed(admin.post("/api/projects", json={
            "title": "__qa_collab_project__",
            "local_path": str(paths["project"]),
            "description": "isolated qa project",
        }))
        project = r.json() if r.status_code == 200 else {}
        pid = project.get("id")
        state.add("管理员创建项目成功", r.status_code == 200 and bool(pid), "project", "P0",
                  f"HTTP {r.status_code}; pid={pid}", ms)

        # v0.16.2 起：新项目从空团队开始，不再自动种子 Team Leader（由用户自行导入并指定）
        r = await admin.get(f"/api/projects/{pid}/agents")
        team = r.json()["agents"]
        leader = next((a for a in team if a["is_leader"]), None)
        state.add("新项目从空团队开始（不自动种子负责人）", len(team) == 0 and leader is None,
                  "collaboration", "P0", f"team_size={len(team)}; leader={leader['slug'] if leader else None}")

        r = await admin.get("/api/agents/templates")
        templates = r.json()["templates"]
        slugs = {t["slug"]: t for t in templates}
        state.add("Agent 库扫描排除 examples 且导入有效模板",
                  "ignored" not in slugs and len(templates) >= 5,
                  "agent-library", "P1", f"templates={len(templates)}")

        # 显式导入负责人并设为 Team Leader，模拟用户自选负责人的新流程
        owner_import = await admin.post(f"/api/projects/{pid}/agents/import",
                                        json={"template_id": slugs["specialized-project-owner"]["id"]})
        leader = owner_import.json() if owner_import.status_code == 200 else None
        set_leader = None
        if leader:
            set_leader = await admin.put(f"/api/projects/{pid}/agents/{leader['id']}/leader")
        r = await admin.get(f"/api/projects/{pid}/agents")
        team_after = r.json()["agents"]
        seeded_leader = next((a for a in team_after if a["is_leader"]), None)
        state.add("显式导入负责人并设为 Team Leader",
                  bool(leader) and set_leader is not None and set_leader.status_code == 200
                  and seeded_leader is not None and seeded_leader["slug"] == "specialized-project-owner",
                  "collaboration", "P0",
                  f"leader={seeded_leader['slug'] if seeded_leader else None}")

        imported = {}
        for slug in ("qa-backend-developer", "qa-frontend-developer", "qa-tester", "qa-security-engineer"):
            rr = await admin.post(f"/api/projects/{pid}/agents/import", json={"template_id": slugs[slug]["id"]})
            if rr.status_code == 200:
                imported[slug] = rr.json()
        state.add("项目导入后端/前端/测试/安全成员", len(imported) == 4,
                  "project-agents", "P0", f"imported={list(imported)}")

        r, ms = await timed(admin.put("/api/settings", json={
            "providers": [{
                "id": "qa-api",
                "name": "QA API",
                "type": "api",
                "api_key": "sk-qa-secret-value",
                "base_url": "http://127.0.0.1:9",
                "model": "qa-model",
                "api_format": "openai",
                "executable": "",
            }],
            "default_provider_id": "qa-api",
        }))
        state.add("供应商配置保存成功", r.status_code == 200, "settings", "P1", f"HTTP {r.status_code}", ms)

        r = await admin.get("/api/settings")
        data = r.json()
        masked_key = data["providers"][0]["api_key"]
        state.add("api_key 读取脱敏且不泄露原文",
                  masked_key != "sk-qa-secret-value" and "*" in masked_key,
                  "security", "P0", f"masked={masked_key}")

        r = await admin.put("/api/settings", json={
            "providers": [{**data["providers"][0], "name": "QA API Renamed"}],
            "default_provider_id": "qa-api",
        })
        import config  # noqa: PLC0415

        saved = config.load_settings().providers[0]
        state.add("脱敏 key 回写时保留原始密钥", r.status_code == 200 and saved.api_key == "sk-qa-secret-value",
                  "security", "P1", "原始密钥只在本地隔离 config 中校验，不写入报告")

        import memory as memory_mod  # noqa: PLC0415

        try:
            memory_mod.read_memory("../secret")
            memory_slash_rejected = False
        except ValueError:
            memory_slash_rejected = True
        state.add("Memory 模块拒绝带斜杠的路径穿越 slug",
                  memory_slash_rejected,
                  "security", "P0")

        try:
            memory_mod.read_memory("..")
            dotdot_rejected = False
        except ValueError:
            dotdot_rejected = True
        state.add("Memory slug 含 '..' 按 OpenSpec 应拒绝",
                  dotdot_rejected,
                  "security", "P1",
                  "当前实现会把 '..' 映射为 memory/...md，未越界但与 OpenSpec 描述不一致")

        r = await admin.get("/api/__qa_unknown_endpoint__")
        body_head = r.text[:40].replace("\n", " ")
        state.add("未知 API 路径不应被 SPA fallback 返回 200",
                  r.status_code == 404,
                  "security", "P1",
                  f"HTTP {r.status_code}; body_head={body_head}")

        r = await admin.post("/api/skills", json={
            "slug": "../escape",
            "name": "bad",
            "description": "",
            "body": "",
        })
        state.add("Skill slug 路径穿越被拒绝", r.status_code == 400, "security", "P0", f"HTTP {r.status_code}")

        r = await admin.put("/api/agent-config/qa-backend-developer/skills", json={
            "skill_slugs": ["backend-api-test", "security-boundary-check"],
        })
        mem = await admin.get("/api/memory/qa-backend-developer")
        content = mem.json()["content"]
        state.add("配置 Skills 后同步到 Agent 记忆受管段落",
                  r.status_code == 200 and "可用 Skills" in content and "后端接口测试" in content,
                  "agent-skills", "P1")

        r, ms = await timed(admin.post(f"/api/projects/{pid}/tasks", json={
            "title": "__qa_api_task__",
            "description": "请实现 API 并补充测试，优先后端开发者处理。",
            "assignee_slug": "qa-backend-developer",
            "priority": "high",
        }))
        task = r.json() if r.status_code == 200 else {}
        tid = task.get("id")
        state.add("创建任务并建立 Thread", r.status_code == 200 and bool(tid) and task.get("conversation_id"),
                  "task-system", "P0", f"tid={tid}", ms)

        r = await admin.put(f"/api/projects/{pid}/tasks/{tid}/status", json={"status": "in_progress"})
        acts = await admin.get(f"/api/projects/{pid}/tasks/{tid}/activities")
        has_status_activity = any(x.get("action") == "status_changed" for x in acts.json()["timeline"])
        state.add("任务状态变更写入活动时间线", r.status_code == 200 and has_status_activity,
                  "task-system", "P1")

        r = await admin.put(f"/api/projects/{pid}/tasks/{tid}", json={"priority": "urgent"})
        acts = await admin.get(f"/api/projects/{pid}/tasks/{tid}/activities")
        has_prio_activity = any(x.get("action") == "priority_changed" for x in acts.json()["timeline"])
        state.add("任务优先级变更写入活动时间线", r.status_code == 200 and has_prio_activity,
                  "task-system", "P1")

        r = await admin.post(f"/api/projects/{pid}/tasks/{tid}/subtasks", json={"title": "补接口测试"})
        subs = await admin.get(f"/api/projects/{pid}/tasks/{tid}/subtasks")
        state.add("子任务创建与查询成功", r.status_code == 200 and len(subs.json()["subtasks"]) == 1,
                  "task-system", "P1")

        r, ms = await timed(admin.post(f"/api/tasks/{tid}/dispatch", json={
            "prompt": "请先说明你能否执行。",
            "assignee_slug": "qa-backend-developer",
        }))
        text = r.text
        failed_runs = await count_db("SELECT COUNT(*) FROM task_runs WHERE task_id=? AND status='failed'", (tid,))
        state.add("未接入有效模型时分派失败可控并记录 run", r.status_code == 200 and "未接入有效模型" in text and failed_runs >= 1,
                  "agent-execution", "P0", f"HTTP {r.status_code}; failed_runs={failed_runs}", ms)

        # Collaboration deterministic engine tests.
        import collab  # noqa: PLC0415
        from executor import runner  # noqa: PLC0415
        from executor.base import ExecEvent  # noqa: PLC0415

        backend_agent = imported["qa-backend-developer"]
        tester_agent = imported["qa-tester"]
        # leader 由上方显式导入并设为 Team Leader；缺省回退到 owner slug，避免协同块因 None 崩溃中断整套件
        leader_slug = leader["slug"] if leader else "specialized-project-owner"
        executed: list[dict[str, Any]] = []
        original_execute = runner.execute_dispatch

        async def fake_execute_dispatch(task_obj, agent_obj, prompt,
                                        persist_user_msg=True, user_name=""):
            start = time.perf_counter()
            await asyncio.sleep(0.02)
            slug = agent_obj["slug"]
            if slug == leader_slug:
                text_out = "@后端开发者 请实现 API 并补充接口测试。"
            elif slug == backend_agent["slug"]:
                text_out = "后端接口已完成，@测试专员 请验证状态码、响应体和日志。"
            elif slug == tester_agent["slug"]:
                text_out = "验证通过：接口、日志和边界均符合预期。"
            else:
                text_out = "无需处理。"
            executed.append({
                "slug": slug,
                "prompt": prompt,
                "start": start,
                "end": time.perf_counter(),
                "text": text_out,
            })
            yield ExecEvent("text", text_out)
            yield ExecEvent("done")

        runner.execute_dispatch = fake_execute_dispatch
        collab_start = time.perf_counter()
        try:
            rid = await collab.enqueue_run(tid, leader_slug, "请协调完成 API 任务", "collaborate", is_leader=True)
            for _ in range(10):
                did = await collab._tick()
                if not did:
                    break
            collab_ms = (time.perf_counter() - collab_start) * 1000
        finally:
            runner.execute_dispatch = original_execute

        order = [x["slug"] for x in executed]
        state.metrics["deterministic_collab_ms"] = round(collab_ms, 2)
        state.metrics["deterministic_collab_order"] = order
        state.add("协同：Leader 入队并按后端任务找对后端开发者",
                  bool(rid) and order[:2] == [leader_slug, backend_agent["slug"]],
                  "collaboration", "P0", f"order={order}")
        state.add("协同：成员回报后触发测试专员验证",
                  tester_agent["slug"] in order,
                  "collaboration", "P0", f"order={order}")
        state.add("协同：假执行器 3 轮队列性能达标",
                  collab_ms < 1000,
                  "performance", "P1", f"{collab_ms:.2f} ms")

        roster = await collab.build_roster(pid, leader_slug)
        state.add("Leader 花名册包含成员技能与精确 @语法",
                  "@后端开发者" in roster and "后端接口测试" in roster,
                  "collaboration", "P0", roster[:180])

        duplicate_task = await admin.post(f"/api/projects/{pid}/tasks", json={
            "title": "__qa_dedupe_task__",
            "description": "dedupe",
            "assignee_slug": "",
            "priority": "none",
        })
        dedupe_tid = duplicate_task.json()["id"]
        first = await collab.enqueue_run(dedupe_tid, leader_slug, "", "collaborate", is_leader=True)
        second = await collab.enqueue_run(dedupe_tid, leader_slug, "", "collaborate", is_leader=True)
        state.add("协同：同一任务同一成员 queued/running 去重",
                  bool(first) and second is None,
                  "collaboration", "P1", f"first={first}; second={second}")

        from database import get_connection  # noqa: PLC0415

        limit_task = await admin.post(f"/api/projects/{pid}/tasks", json={
            "title": "__qa_limit_task__",
            "description": "limit",
            "assignee_slug": "",
            "priority": "none",
        })
        limit_tid = limit_task.json()["id"]
        db = await get_connection()
        try:
            for _ in range(collab.MAX_RUNS_PER_TASK):
                await db.execute(
                    "INSERT INTO run_queue (task_id, agent_slug, trigger, is_leader, prompt, status) VALUES (?,?,?,?,?,?)",
                    (limit_tid, leader_slug, "qa", 1, "", "done"),
                )
            await db.commit()
        finally:
            await db.close()
        blocked = await collab.enqueue_run(limit_tid, leader_slug, "", "collaborate", is_leader=True)
        limit_note_count = await count_db(
            "SELECT COUNT(*) FROM activities WHERE task_id=? AND action='commented'", (limit_tid,))
        state.add("协同：达到深度上限后拒绝继续入队并记录活动",
                  blocked is None and limit_note_count >= 1,
                  "collaboration", "P1", f"blocked={blocked}; activity_notes={limit_note_count}")

        r = await admin.get(f"/api/projects/{pid}/tasks")
        board = r.json()["board"]
        state.add("看板按状态分组返回任务", r.status_code == 200 and "in_progress" in board,
                  "task-board", "P1", f"columns={list(board.keys())}")

        # Simple API latency probe.
        latencies = []
        for _ in range(10):
            _, one_ms = await timed(admin.get(f"/api/projects/{pid}/tasks"))
            latencies.append(one_ms)
        latencies_sorted = sorted(latencies)
        p95 = latencies_sorted[int(len(latencies_sorted) * 0.95) - 1]
        state.metrics["tasks_list_p95_ms"] = round(p95, 2)
        state.add("性能：任务列表本地 p95 < 300ms", p95 < 300,
                  "performance", "P1", f"p95={p95:.2f} ms")

        if not include_live:
            state.note("真实模型协同未执行：本次默认只跑隔离可重复测试。需要设置 --live 并确认供应商/CLI 可用后执行真实质量评估。")
        else:
            await run_live_collaboration_probe(state, admin, paths, pid, leader_slug)

    finally:
        await anon.aclose()
        await admin.aclose()

    return state

async def run_live_collaboration_probe(state: QaState, admin: httpx.AsyncClient,
                                       paths: dict[str, Path], pid: int,
                                       leader_slug: str) -> None:
    """Run one real CLI-backed collaboration probe in the isolated project."""
    import collab  # noqa: PLC0415
    from database import get_connection  # noqa: PLC0415

    if not shutil.which("claude"):
        state.add("真实协同：Claude CLI 可用", False, "live-collaboration", "P0", "claude not found")
        return

    provider_id = "qa-live-claude"
    start_total = time.perf_counter()
    r = await admin.put("/api/settings", json={
        "providers": [{
            "id": provider_id,
            "name": "QA Live Claude CLI",
            "type": "claude-cli",
            "api_key": "",
            "base_url": "",
            "model": "",
            "api_format": "openai",
            "executable": "",
        }],
        "default_provider_id": provider_id,
    })
    state.add("真实协同：隔离配置写入 Claude CLI provider",
              r.status_code == 200, "live-collaboration", "P0", f"HTTP {r.status_code}")

    for slug in (leader_slug, "qa-backend-developer", "qa-tester"):
        await admin.put(f"/api/agent-config/{slug}/model", json={"provider_id": provider_id})
    await admin.put("/api/agent-config/qa-backend-developer/skills", json={"skill_slugs": ["backend-api-test"]})
    await admin.put("/api/agent-config/qa-tester/skills", json={"skill_slugs": ["backend-api-test", "security-boundary-check"]})

    (paths["project"] / "README.md").write_text(
        "# Live collaboration QA workspace\n\nOnly write files in this directory.\n",
        encoding="utf-8",
    )

    # Remove queued items left by deterministic probes so the live probe starts cleanly.
    db = await get_connection()
    try:
        await db.execute("DELETE FROM run_queue WHERE status IN ('queued','running')")
        await db.commit()
    finally:
        await db.close()

    task_resp = await admin.post(f"/api/projects/{pid}/tasks", json={
        "title": "__qa_live_collaboration__",
        "description": (
            "这是一次真实多 Agent 协同评测。请 Team Leader 只做统筹，不要自己写文件。"
            "目标：选择最合适成员在当前临时工作区创建 QA_COLLAB_RESULT.md，"
            "文件必须包含三行：Akivili live collaboration QA、owner=backend、verified=pending。"
            "成员完成后应 @测试专员 验证文件内容。测试专员验证后给出结论。"
        ),
        "assignee_slug": "",
        "priority": "high",
    })
    if task_resp.status_code != 200:
        state.add("真实协同：创建 live 任务", False, "live-collaboration", "P0", f"HTTP {task_resp.status_code}")
        return
    task_id = task_resp.json()["id"]

    collab_resp = await admin.post(f"/api/tasks/{task_id}/collaborate")
    state.add("真实协同：通过真实 collaborate 接口唤醒 Leader",
              collab_resp.status_code == 200,
              "live-collaboration", "P0", f"HTTP {collab_resp.status_code}; {collab_resp.text[:120]}")

    live_order: list[str] = []
    tick_durations: list[float] = []
    for _ in range(6):
        db = await get_connection()
        try:
            queued = await (await db.execute(
                "SELECT COUNT(*) c FROM run_queue WHERE task_id=? AND status='queued'", (task_id,))).fetchone()
        finally:
            await db.close()
        if not queued or queued["c"] == 0:
            break
        before = await fetch_one(
            "SELECT agent_slug FROM run_queue WHERE task_id=? AND status='queued' ORDER BY id LIMIT 1", (task_id,))
        tick_start = time.perf_counter()
        did = await collab._tick()
        tick_ms = (time.perf_counter() - tick_start) * 1000
        tick_durations.append(round(tick_ms, 2))
        if before:
            live_order.append(before["agent_slug"])
        if not did:
            break

    total_ms = (time.perf_counter() - start_total) * 1000
    state.metrics["live_collab_ms"] = round(total_ms, 2)
    state.metrics["live_collab_tick_ms"] = tick_durations
    state.metrics["live_collab_order"] = live_order

    db = await get_connection()
    try:
        messages = await (await db.execute(
            """SELECT role, content FROM messages
               WHERE conversation_id=(SELECT conversation_id FROM tasks WHERE id=?) ORDER BY id""",
            (task_id,))).fetchall()
        runs = await (await db.execute(
            "SELECT agent_slug, status FROM task_runs WHERE task_id=? ORDER BY id", (task_id,))).fetchall()
    finally:
        await db.close()
    assistant_texts = [m["content"] for m in messages if m["role"] == "assistant"]
    combined = "\n".join(assistant_texts)
    run_summary = [(r["agent_slug"], r["status"]) for r in runs]

    result_file = paths["project"] / "QA_COLLAB_RESULT.md"
    file_text = result_file.read_text(encoding="utf-8") if result_file.exists() else ""
    leader_text = assistant_texts[0] if assistant_texts else ""

    leader_delegated = "@后端开发者" in leader_text
    backend_triggered = "qa-backend-developer" in live_order or any(r[0] == "qa-backend-developer" for r in run_summary)
    tester_triggered = "qa-tester" in live_order or any(r[0] == "qa-tester" for r in run_summary)
    file_ok = all(x in file_text for x in [
        "Akivili live collaboration QA", "owner=backend", "verified=pending",
    ])
    no_failed_runs = bool(run_summary) and all(r[1] == "succeeded" for r in run_summary)

    score = 0
    score += 20 if leader_delegated else 0
    score += 20 if backend_triggered else 0
    score += 15 if tester_triggered else 0
    score += 20 if file_ok else 0
    score += 15 if str(result_file).startswith(str(paths["project"])) else 0
    score += 10 if total_ms < 10 * 60 * 1000 and len(run_summary) <= 6 else 0
    state.metrics["live_collab_score"] = score

    state.add("真实协同：Leader 按任务找对后端开发者", leader_delegated,
              "live-collaboration", "P0", leader_text[:220])
    state.add("真实协同：@mention 触发后端成员执行", backend_triggered,
              "live-collaboration", "P0", f"order={live_order}; runs={run_summary}")
    state.add("真实协同：后端完成后触发测试专员", tester_triggered,
              "live-collaboration", "P1", f"order={live_order}; runs={run_summary}")
    state.add("真实协同：任务产物完成度", file_ok,
              "live-collaboration", "P0", file_text[:220] if file_text else "QA_COLLAB_RESULT.md missing")
    state.add("真实协同：所有 live runs 成功结束", no_failed_runs,
              "live-collaboration", "P1", f"runs={run_summary}")
    state.add("真实协同：综合评分 >= 80", score >= 80,
              "live-collaboration", "P0", f"score={score}; combined={combined[:260]}")

def render_markdown(state: QaState, paths: dict[str, Path]) -> str:
    total = len(state.results)
    failed = [r for r in state.results if r.status != "PASS"]
    passed = total - len(failed)
    by_cat: dict[str, dict[str, int]] = {}
    for r in state.results:
        by_cat.setdefault(r.category, {"PASS": 0, "FAIL": 0})
        by_cat[r.category][r.status] += 1

    lines = [
        "# Akivili QA Run Report",
        "",
        f"- Total: {total}",
        f"- Passed: {passed}",
        f"- Failed: {len(failed)}",
        f"- Isolated workspace: `{paths['tmp']}`",
        "",
        "## Category Summary",
        "",
        "| Category | PASS | FAIL |",
        "|---|---:|---:|",
    ]
    for cat, nums in sorted(by_cat.items()):
        lines.append(f"| {cat} | {nums['PASS']} | {nums['FAIL']} |")

    lines += ["", "## Metrics", ""]
    if state.metrics:
        for k, v in state.metrics.items():
            lines.append(f"- `{k}`: {v}")
    else:
        lines.append("- No metrics captured.")

    lines += ["", "## Notes", ""]
    if state.notes:
        for n in state.notes:
            lines.append(f"- {n}")
    else:
        lines.append("- No notes.")

    lines += ["", "## Case Results", "", "| Status | Severity | Category | Case | Detail | ms |", "|---|---|---|---|---|---:|"]
    for r in state.results:
        detail = (r.detail or "").replace("|", "\\|").replace("\n", " ")
        if len(detail) > 220:
            detail = detail[:217] + "..."
        lines.append(f"| {r.status} | {r.severity} | {r.category} | {r.name} | {detail} | {r.elapsed_ms:.2f} |")
    return "\n".join(lines) + "\n"


async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep temporary directory")
    parser.add_argument("--live", action="store_true", help="reserved for live LLM collaboration evaluation")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="akivili-qa-", dir=r"C:\tmp"))
    paths = setup_isolated_config(tmp)
    state = await run_suite(paths, include_live=args.live)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"qa_results_{stamp}.json"
    md_path = REPORT_DIR / f"qa_results_{stamp}.md"
    payload = {
        "summary": {
            "total": len(state.results),
            "passed": sum(1 for r in state.results if r.status == "PASS"),
            "failed": sum(1 for r in state.results if r.status != "PASS"),
        },
        "metrics": state.metrics,
        "notes": state.notes,
        "results": [r.__dict__ for r in state.results],
        "paths": {k: str(v) for k, v in paths.items()},
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(state, paths), encoding="utf-8")

    failed = payload["summary"]["failed"]
    print(f"QA results: {payload['summary']['passed']}/{payload['summary']['total']} passed")
    print(f"Markdown report: {md_path}")
    print(f"JSON report: {json_path}")
    if args.keep:
        print(f"Kept temp dir: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))




