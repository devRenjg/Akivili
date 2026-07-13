"""Akivili Skill 不可下载探针。

验证「仅展示、禁止下载」的 Skill 全链路（隔离库）：
  1. frontmatter `downloadable: false` → 扫描得 downloadable=0；缺省 → 1。
  2. 列表/详情 API 暴露 downloadable 字段。
  3. 禁下载 Skill 的 /download 端点返回 403；正常 Skill 仍可下载(200)。
临时 config/DB，测完清理。
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


async def run_probe(paths):
    probe = Probe()
    skills_dir = paths["skills"]
    # 写两个 skill：一个禁下载、一个正常
    (skills_dir / "kb-locked.md").write_text(
        "---\nname: 锁定知识库\ndescription: 仅供 Agent 集成\ndownloadable: false\n---\n\n正文内容。\n",
        encoding="utf-8")
    (skills_dir / "normal-skill.md").write_text(
        "---\nname: 普通技能\ndescription: 可下载\n---\n\n正文。\n", encoding="utf-8")

    app = await bootstrap_backend(paths)   # 内部会 rescan skills
    import httpx
    tr = httpx.ASGITransport(app=app)
    anon = httpx.AsyncClient(transport=tr, base_url="http://testserver")

    # 扫描后列表
    r = await anon.get("/api/skills")
    skills = {s["slug"]: s for s in r.json()["skills"]}
    probe.check("列表含两个 skill", "kb-locked" in skills and "normal-skill" in skills,
                f"slugs={list(skills)}")
    probe.check("禁下载 skill downloadable=0", skills.get("kb-locked", {}).get("downloadable") == 0,
                f"downloadable={skills.get('kb-locked',{}).get('downloadable')}")
    probe.check("正常 skill downloadable=1", skills.get("normal-skill", {}).get("downloadable") == 1,
                f"downloadable={skills.get('normal-skill',{}).get('downloadable')}")

    # 详情也暴露字段
    locked_id = skills["kb-locked"]["id"]
    normal_id = skills["normal-skill"]["id"]
    d = (await anon.get(f"/api/skills/{locked_id}")).json()
    probe.check("详情暴露 downloadable", d.get("downloadable") == 0, f"downloadable={d.get('downloadable')}")

    # 下载端点：禁下载→403，正常→200
    r_locked = await anon.get(f"/api/skills/{locked_id}/download")
    probe.check("禁下载 skill /download 返回 403", r_locked.status_code == 403,
                f"HTTP {r_locked.status_code}")
    r_normal = await anon.get(f"/api/skills/{normal_id}/download")
    probe.check("正常 skill /download 返回 200", r_normal.status_code == 200,
                f"HTTP {r_normal.status_code}")

    # 禁下载不产生下载记录（被 403 拦在写库前）
    r2 = await anon.get("/api/skills")
    locked2 = next(s for s in r2.json()["skills"] if s["slug"] == "kb-locked")
    probe.check("禁下载 skill 无下载计数", locked2.get("download_count", 0) == 0,
                f"count={locked2.get('download_count')}")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili_skilldl_"))
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
