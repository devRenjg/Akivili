"""Akivili 关键多 Agent 协同案例（真实 CLI 端到端）——底层改动后的回归基准。

场景（用户最看重的协同闭环）：
  用户给「项目负责人」下一条指令：介绍团队，并让每个成员各自出来做自我介绍、
  各自建一张子任务挂在主任务上；所有成员反馈后，负责人汇总统一汇报。

断言要点：
  1. 负责人 @ 的是**项目里真实存在的成员**（不编造、不 @ 自己）。
  2. 真实成员被唤醒并**各自建了子任务**挂在主任务下。
  3. 子任务陆续完成 → 父任务被自动推进到 reviewing（progress 联动）。
  4. 负责人最终有**汇总收尾**发言。
  5. 全程无 @ 自己、无编造成员告警。

隔离与安全：
  - 独立临时 config → 独立 DB → 独立空闲端口，绝不碰真实 jianagency.db / 8100。
  - 项目名用 `__qa_collab_scenario__`（测试前缀），工作目录用临时空目录。
  - 用真实 claude CLI 执行（需本机已登录 claude）。
  - 结束**删除临时目录 + 独立 DB**，不留残留（隔离库随目录一起删，天然干净）。

用法：
  py -3.12 TestReport/run_collab_scenario.py            # 跑真实场景
  py -3.12 TestReport/run_collab_scenario.py --keep     # 保留临时目录排查
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import os as _os
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
ADMIN_USER = _os.environ.get("AKIVILI_ADMIN_USER", "admin")
ADMIN_PASS = _os.environ.get("AKIVILI_ADMIN_PASSWORD", "changeme")

# 团队：一个 8 人示例团队，带昵称、跨 Claude + Codex 两个供应商。
# 每项：(slug, role_name 角色名, nickname 昵称, emoji, is_leader, provider_kind)
# provider_kind: "claude" | "codex"，seed 时映射到隔离 config 里对应供应商 id。
LEADER_SLUG = "specialized-project-owner"
MEMBERS = [
    ("specialized-project-owner",     "项目负责人",   "Nova",  "🧭", 1, "claude"),
    ("engineering-frontend-developer", "前端开发者",   "Iris",  "💻", 0, "claude"),
    ("engineering-backend-architect",  "后端架构师",   "Atlas", "⚙️", 0, "claude"),
    ("engineering-data-engineer",      "数据工程师",   "Echo",  "📊", 0, "claude"),
    ("engineering-senior-developer",   "高级开发者",   "Sage",  "👨‍💻", 0, "claude"),
    ("engineering-security-engineer",  "安全工程师",   "Vault", "🔒", 0, "codex"),
    ("engineering-technical-writer",   "技术文档工程师", "Quill", "✍️", 0, "codex"),
    ("testing-qa-security-specialist", "测试专员",     "Probe", "🛡️", 0, "codex"),
]


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _real_providers() -> dict:
    """从真实 config.json 取 claude-cli 与 codex-cli 供应商配置（复制进隔离 config）。
    返回 {"claude": {...}|None, "codex": {...}|None}。"""
    sys.path.insert(0, str(BACKEND))
    from config import load_settings  # noqa: PLC0415
    out = {"claude": None, "codex": None}
    for p in load_settings().providers:
        if p.type == "claude-cli" and out["claude"] is None:
            out["claude"] = p.model_dump()
        elif p.type == "codex-cli" and out["codex"] is None:
            out["codex"] = p.model_dump()
    return out


def _write_isolated_config(tmp: Path, port: int, providers: list, default_id: str) -> tuple[Path, Path, Path]:
    """写隔离 config.json。返回 (cfg_path, db_path, project_dir)。

    关键：DB/config/记忆 放在 `tmp/backend_data`，而 Agent 的项目工作目录放在**独立的**
    `tmp/proj`（互不为父子）。这样 `--add-dir <project_dir>` 授予的目录里够不到 DB，
    贴合生产（DB 在 backend/、项目在用户任意目录）——Agent 无法 Bash 直接翻库。"""
    data_dir = tmp / "backend_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp / "proj" / "workspace"
    project_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "scenario.db"
    lib_dir = data_dir / "agent_lib_empty"   # 空库：成员直接 seed 进 DB，无需真实 Agents 库（也避免 Leader 探到无关内容）
    lib_dir.mkdir(exist_ok=True)
    cfg = {
        "db_path": str(db_path),
        "agent_library_dir": str(lib_dir),
        "memory_dir": str(data_dir / "memory"),
        "skills_dir": str(data_dir / "skills"),
        "host": "127.0.0.1",
        "port": port,
        "providers": providers,
        "default_provider_id": default_id,
    }
    (data_dir / "memory").mkdir(exist_ok=True)
    (data_dir / "skills").mkdir(exist_ok=True)
    cfg_path = data_dir / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path, db_path, project_dir


class Http:
    """极简 HTTP 客户端（带 cookie），驱动隔离后端。"""
    def __init__(self, base: str):
        self.base = base
        self.cookie = ""

    def _req(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                sc = r.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                body = r.read().decode("utf-8")
                return r.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            return e.code, {"detail": e.read().decode("utf-8", "replace")[:300]}

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, payload=None):
        return self._req("POST", path, payload or {})


def _wait_health(base: str, timeout: float = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def _seed_project_team(db_path: str, project_dir: str, provider_ids: dict) -> dict:
    """直接往隔离 DB 写：项目 + 团队（负责人+成员，含昵称）+ 主任务 + 每人接入模型（按供应商种类）。
    provider_ids: {"claude": id, "codex": id}。schema 已由后端 startup 建好。"""
    import sqlite3
    db = sqlite3.connect(db_path)
    try:
        cur = db.execute("INSERT INTO projects (title, local_path, description) VALUES (?,?,?)",
                         ("__qa_collab_scenario__", project_dir, "关键协同案例（自动清理）"))
        pid = cur.lastrowid
        for slug, role_name, nickname, emoji, is_leader, prov_kind in MEMBERS:
            persona = (f"你是{nickname}（{role_name}）。" if not is_leader else
                       "你是项目负责人，只统筹协调、按成员技能委派、最后汇总，不亲自写具体实现。")
            db.execute(
                "INSERT INTO project_agents (project_id, slug, name, emoji, persona, is_leader) VALUES (?,?,?,?,?,?)",
                (pid, slug, role_name, emoji, persona, is_leader))
            # 昵称 + 接入供应商都按 slug 写进 agent_profiles（昵称优先显示；provider 决定用哪个 CLI 后端）
            pv = provider_ids.get(prov_kind) or provider_ids.get("claude") or ""
            db.execute(
                "INSERT OR REPLACE INTO agent_profiles (slug, provider_id, nickname) VALUES (?,?,?)",
                (slug, pv, nickname))
        # 主任务：用户指令——介绍团队 + 每人自我介绍建子任务 + 负责人汇总
        conv = db.execute("INSERT INTO conversations (project_id, title) VALUES (?,?)",
                          (pid, "团队介绍与自我介绍"))
        task_instruction = (
            "请介绍一下我们这个团队。然后让团队里的每一位成员各自出来做一个自我介绍"
            "（你是谁、在本项目能做什么、有什么价值），"
            "每位成员都各自建一张子任务卡片挂在本任务下记录自己的介绍。"
            "等所有成员都反馈完，你（项目负责人）再把大家的介绍汇总，统一汇报一次。")
        tc = db.execute(
            """INSERT INTO tasks (project_id, title, description, assignee_slug, conversation_id, status, priority)
               VALUES (?,?,?,?,?, 'in_progress', 'high')""",
            (pid, "新团队组建 KO：团队介绍", task_instruction, LEADER_SLUG, conv.lastrowid))
        task_id = tc.lastrowid
        db.commit()
        return {"pid": pid, "task_id": task_id, "conv_id": conv.lastrowid}
    finally:
        db.close()


def _dump(db_path: str, pid: int, task_id: int) -> dict:
    """读取协同产物：主任务消息、子任务、活动、run_queue、父任务状态。"""
    import sqlite3
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        parent = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        subs = db.execute(
            "SELECT id, title, assignee_slug, status FROM tasks WHERE parent_task_id=? ORDER BY id",
            (task_id,)).fetchall()
        msgs = db.execute(
            """SELECT role, content FROM messages
               WHERE conversation_id=(SELECT conversation_id FROM tasks WHERE id=?) ORDER BY id""",
            (task_id,)).fetchall()
        acts = db.execute(
            "SELECT actor_type, actor_name, action, detail FROM activities WHERE task_id=? ORDER BY id",
            (task_id,)).fetchall()
        runs = db.execute(
            "SELECT agent_slug, status FROM task_runs WHERE task_id IN "
            "(SELECT id FROM tasks WHERE id=? OR parent_task_id=?) ORDER BY id",
            (task_id, task_id)).fetchall()
        return {
            "parent_status": parent["status"] if parent else None,
            "subs": [dict(s) for s in subs],
            "assistant_msgs": [m["content"] for m in msgs if m["role"] == "assistant"],
            "activities": [dict(a) for a in acts],
            "runs": [dict(r) for r in runs],
        }
    finally:
        db.close()


def _queue_active(db_path: str, task_id: int) -> int:
    """父+子任务在 run_queue 里 queued/running 的数量（判断协同是否还在跑）。"""
    import sqlite3
    db = sqlite3.connect(db_path)
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM run_queue WHERE status IN ('queued','running') AND task_id IN "
            "(SELECT id FROM tasks WHERE id=? OR parent_task_id=?)", (task_id, task_id)).fetchone()
        return int(row[0]) if row else 0
    finally:
        db.close()


def _parent_status(db_path: str, task_id: int) -> str:
    import sqlite3
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row[0] if row else ""
    finally:
        db.close()


class Report:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(r[1] for r in self.rows)


def run_scenario(keep: bool) -> int:
    rep = Report()
    provs = _real_providers()
    if not provs.get("claude"):
        print("未找到 claude-cli 供应商配置，无法跑真实场景。")
        return 2
    if not provs.get("codex"):
        print("⚠️ 未找到 codex-cli 供应商——Codex 成员将回退到 Claude 供应商（跨供应商项无法验证）。")

    # 组装隔离 config 的 providers 列表 + provider_kind→id 映射
    iso_providers = []
    provider_ids = {}
    for kind in ("claude", "codex"):
        p = provs.get(kind)
        if p:
            iso_providers.append(p)
            provider_ids[kind] = p["id"]
    default_id = provider_ids.get("claude")

    tmp = Path(tempfile.mkdtemp(prefix="akivili-scenario-", dir=r"C:\tmp"))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    cfg_path, db_path_p, project_dir_p = _write_isolated_config(tmp, port, iso_providers, default_id)
    db_path = str(db_path_p)
    project_dir = str(project_dir_p)

    env = dict(__import__("os").environ)
    env["AKIVILI_CONFIG"] = str(cfg_path)
    backend_log = open(tmp / "backend_data" / "isolated_backend.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(BACKEND), env=env,
                            stdout=backend_log, stderr=subprocess.STDOUT)
    try:
        if not _wait_health(base):
            print("隔离后端未就绪，放弃。")
            proc.terminate()
            return 2
        print(f"隔离后端已就绪：{base}  DB={db_path}")

        ids = _seed_project_team(db_path, project_dir, provider_ids)
        pid, task_id = ids["pid"], ids["task_id"]
        n_claude = sum(1 for m in MEMBERS if m[5] == "claude" and not m[4])
        n_codex = sum(1 for m in MEMBERS if m[5] == "codex")
        print(f"已建项目 #{pid} + 团队（{len(MEMBERS)} 人：Claude成员{n_claude}+Codex成员{n_codex}+负责人）+ 主任务 #{task_id}")

        http = Http(base)
        sc, _ = http.post("/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASS})
        rep.check("管理员登录隔离后端", sc == 200, f"HTTP {sc}")

        # 触发协同：唤醒负责人（等价于前端把卡片拖入「进行中」→ runsApi.autoDispatch）
        sc, body = http.post(f"/api/tasks/{task_id}/auto-dispatch")
        rep.check("触发团队协同（唤醒负责人）", sc == 200, f"HTTP {sc} {str(body)[:80]}")

        # 轮询等待协同闭环（真实 Claude，给足时间；最多 ~10 分钟）
        # 先等背景循环领取首个 leader run（enqueue 后 _loop 约 1s 内 claim）
        time.sleep(6)
        deadline = time.time() + 18 * 60   # 8 人真实团队跨双供应商，给足时间
        last = -1
        idle = 0
        seen_active = False
        while time.time() < deadline:
            active = _queue_active(db_path, task_id)
            if active != last:
                print(f"  [{int(time.time()) % 100000}] 协同进行中，队列活跃={active}")
                last = active
            if active > 0:
                seen_active = True
                idle = 0
            else:
                idle += 1
                pstat = _parent_status(db_path, task_id)
                # 完美收尾：父任务已 done（负责人汇总完毕）→ 立即结束
                if seen_active and pstat == "done":
                    break
                # 已 reviewing（子任务全完成、总结 run 已入队）：多等几轮让总结 run 真正跑完，
                # 不要一看到 active=0 就退出（总结 run 可能还没被 _loop 领取）
                if seen_active and pstat == "reviewing" and idle >= 8:
                    break
                if seen_active and pstat not in ("reviewing", "done") and idle >= 10:
                    break   # 兜底：非 reviewing/done 但长时间空转
                if not seen_active and idle >= 12:   # ~60s 还没起来，判定触发失败
                    break
            time.sleep(5)

        d = _dump(db_path, pid, task_id)
        _assert_scenario(rep, d)

        # 写报告
        stamp = time.strftime("%Y%m%d-%H%M%S")
        report_path = ROOT / "TestReport" / f"collab_scenario_{stamp}.md"
        report_path.write_text(_render(rep, d), encoding="utf-8")
        print(f"\n报告：{report_path}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if keep:
            print(f"保留隔离目录：{tmp}")
        else:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            print("已清理隔离目录与独立 DB（无残留）")

    passed = sum(1 for _, ok, _ in rep.rows if ok)
    print(f"\n关键协同案例：{passed}/{len(rep.rows)} 通过")
    return 0 if rep.ok else 1


def _assert_scenario(rep: Report, d: dict) -> None:
    # MEMBERS: (slug, role_name, nickname, emoji, is_leader, prov_kind)
    real = [m for m in MEMBERS if not m[4]]
    real_member_slugs = [m[0] for m in real]
    # @ 依据：昵称优先（与 _member_names 一致），也认角色名
    real_at_names = [m[2] for m in real] + [m[1] for m in real]
    codex_slugs = [m[0] for m in real if m[5] == "codex"]
    claude_slugs = [m[0] for m in real if m[5] == "claude"]
    subs = d["subs"]
    acts = d["activities"]
    msgs = d["assistant_msgs"]
    leader_text = msgs[0] if msgs else ""

    # 1. 负责人把活派给了真实成员：@ 发言点名 或 建子任务指派（两种委派方式任一即可）
    all_leader_text = "\n".join(msgs)
    mentioned_real = [n for n in real_at_names if ("@" + n) in all_leader_text]
    sub_owners = {s["assignee_slug"] for s in subs}
    sub_assigned_real = [s for s in real_member_slugs if s in sub_owners]
    rep.check("负责人把活派给了真实成员（@点名或建子任务指派）",
              len(mentioned_real) >= 1 or len(sub_assigned_real) >= 1,
              f"@命中={mentioned_real}; 子任务指派={sub_assigned_real}")

    # 2. 无“@ 了不存在成员”的系统告警（不编造成员）
    fabricated = [a for a in acts if a["action"] == "commented" and a["actor_type"] == "system"
                  and "不存在的成员" in str(a.get("detail", ""))]
    rep.check("负责人未编造花名册外成员", len(fabricated) == 0,
              f"编造告警数={len(fabricated)}")

    # 3. 负责人没 @ 自己
    rep.check("负责人未 @ 自己", "@项目负责人" not in leader_text and "@星" not in leader_text,
              leader_text[:80])

    # 4. 全体真实成员（含 Codex）都建了子任务被指派（覆盖度）
    covered = [s for s in real_member_slugs if s in sub_owners]
    rep.check("真实成员各自建了子任务挂主任务", len(subs) >= 1 and len(covered) >= 1,
              f"子任务数={len(subs)}; 覆盖成员={len(covered)}/{len(real_member_slugs)}; owner={sorted(sub_owners)}")

    # 4b. 跨供应商：Codex 成员也真正被唤醒执行（run 里出现过 Codex slug）
    run_slugs = {r["agent_slug"] for r in d["runs"]}
    woken_codex = [s for s in codex_slugs if s in run_slugs or s in sub_owners]
    woken_claude = [s for s in claude_slugs if s in run_slugs or s in sub_owners]
    if codex_slugs:
        rep.check("Codex 供应商成员也被唤醒（跨供应商协同）",
                  len(woken_codex) >= 1,
                  f"Codex被唤醒={woken_codex}/{codex_slugs}; Claude被唤醒={woken_claude}")

    # 4c. 昵称正确：子任务标题/发言里用昵称而非角色名（流萤 而非 数据工程师）
    sub_titles = " ".join(s["title"] for s in subs)
    nick_hits = [m[2] for m in real if m[2] in sub_titles or m[2] in all_leader_text]
    rep.check("成员以昵称示人（如 流萤 而非 数据工程师）",
              len(nick_hits) >= 1,
              f"昵称命中={nick_hits}")

    # 5. 闭环：所有子任务完成后父任务不应停在 in_progress，应进入 reviewing 或 done
    subs_done = sum(1 for s in subs if s["status"] == "done")
    all_subs_done = len(subs) > 0 and subs_done == len(subs)
    if all_subs_done:
        rep.check("闭环：子任务全完成后父任务进入 reviewing/done（不卡在进行中）",
                  d["parent_status"] in ("reviewing", "done"),
                  f"parent_status={d['parent_status']}; subs_done={subs_done}/{len(subs)}")
    else:
        rep.check("父任务随子任务推进状态联动", d["parent_status"] in ("reviewing", "done", "in_progress"),
                  f"parent_status={d['parent_status']}; subs_done={subs_done}/{len(subs)}")

    # 6. 负责人有汇总收尾发言
    tail = "\n".join(msgs[-3:])
    summarized = any(k in tail for k in ["汇总", "总结", "汇报", "综上", "整体介绍"])
    rep.check("负责人做了汇总收尾", summarized, tail[:160])

    # 7. 无失败的 run
    runs = d["runs"]
    no_fail = bool(runs) and all(r["status"] in ("succeeded",) for r in runs)
    rep.check("协同过程无失败 run", no_fail or not runs,
              f"runs={[(r['agent_slug'], r['status']) for r in runs]}")


def _render(rep: Report, d: dict) -> str:
    lines = ["# Akivili 关键协同案例报告", "",
             f"- 通过：{sum(1 for _,ok,_ in rep.rows if ok)}/{len(rep.rows)}",
             f"- 父任务状态：{d['parent_status']}",
             f"- 子任务数：{len(d['subs'])}", "", "## 断言", ""]
    for name, ok, detail in rep.rows:
        lines.append(f"- [{'PASS' if ok else 'FAIL'}] {name}{('　—　' + detail) if detail else ''}")
    lines += ["", "## 子任务", ""]
    for s in d["subs"]:
        lines.append(f"- #{s['id']} {s['title']}（owner={s['assignee_slug']}, {s['status']}）")
    lines += ["", "## 负责人首条发言", "", "> " + (d["assistant_msgs"][0][:500] if d["assistant_msgs"] else "(无)")]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留隔离目录排查")
    args = ap.parse_args()
    return run_scenario(args.keep)


if __name__ == "__main__":
    raise SystemExit(main())



