"""Skill 库：扫描 skills_dir 下的 .md（能力指令文本），解析入库。

Skill = 能力说明/规范/操作要领的纯文本，运行时（P4）注入到 Agent 系统提示。
复用 agents.py 的 frontmatter 解析。读写限定在 skills_dir 内（白名单 slug 防穿越）。
"""
import re
from pathlib import Path

import aiosqlite

from agents import parse_frontmatter
from config import load_settings
from database import get_db_path

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

SKILLS_README = """# Skill 库目录

每个 `<skill-slug>.md` 是一个 Skill —— 一段能力说明 / 规范 / 操作要领（纯文本）。

## 约定

- frontmatter 至少含 `name`，建议含 `description`；`---` 之后是能力正文。
- Agent 可在项目里勾选启用若干 Skill；运行时这些 Skill 的正文会注入到该 Agent 的能力上下文。
- Skill 的启用按 Agent 身份（slug）跨项目共享。

可直接往本目录丢 `.md`，或在平台「Skills」页新建。
"""


def _skill_path(slug: str) -> Path:
    if not slug or ".." in slug or not _SLUG_RE.match(slug):
        raise ValueError("非法的 skill slug")
    root = Path(load_settings().skills_dir).resolve()
    target = (root / f"{slug}.md").resolve()
    if not target.is_relative_to(root):
        raise ValueError("skill 路径越界")
    return target


def ensure_skills_dir() -> None:
    root = Path(load_settings().skills_dir)
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(SKILLS_README, encoding="utf-8")


def scan_from_disk(root_dir: str) -> tuple[list[dict], int]:
    root = Path(root_dir)
    if not root.exists():
        return [], 0
    skills, skipped = [], 0
    for md in root.glob("*.md"):
        if md.name.lower() == "readme.md":
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        meta, body = parse_frontmatter(text)
        name = meta.get("name") or md.stem
        skills.append({
            "slug": md.stem,
            "name": name,
            "description": meta.get("description", ""),
            "source_path": str(md),
            "body": body,
            "is_dir": 0,
        })
    # 目录型 Skill：<slug>/SKILL.md（Anthropic Skill 标准，含 scripts/references 子目录）
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        meta, body = parse_frontmatter(text)
        name = meta.get("name") or sub.name
        skills.append({
            "slug": sub.name,
            "name": name,
            "description": meta.get("description", ""),
            "source_path": str(sub),   # 指向目录，供打包 zip 下载
            "body": body,
            "is_dir": 1,
        })
    return skills, skipped


async def rescan() -> dict:
    skills, skipped = scan_from_disk(load_settings().skills_dir)
    inserted = updated = 0
    async with aiosqlite.connect(get_db_path()) as db:
        for s in skills:
            ex = await (await db.execute("SELECT id FROM skills WHERE slug=?", (s["slug"],))).fetchone()
            if ex:
                await db.execute(
                    "UPDATE skills SET name=?, description=?, source_path=?, body=?, is_dir=? WHERE slug=?",
                    (s["name"], s["description"], s["source_path"], s["body"], s.get("is_dir", 0), s["slug"]))
                updated += 1
            else:
                await db.execute(
                    "INSERT INTO skills (slug, name, description, source_path, body, is_dir) VALUES (?,?,?,?,?,?)",
                    (s["slug"], s["name"], s["description"], s["source_path"], s["body"], s.get("is_dir", 0)))
                inserted += 1
        await db.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "total": inserted + updated}


async def count_skills() -> int:
    async with aiosqlite.connect(get_db_path()) as db:
        row = await (await db.execute("SELECT COUNT(*) FROM skills")).fetchone()
        return row[0] if row else 0


def save_skill_file(slug: str, name: str, description: str, body: str) -> None:
    """新建/编辑：把 Skill 落盘为 skills/<slug>.md（含 frontmatter）。"""
    path = _skill_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    path.write_text(fm, encoding="utf-8")
