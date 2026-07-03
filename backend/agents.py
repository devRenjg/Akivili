"""Agent 模版扫描：读取库目录下的 .md，解析 frontmatter + 正文，登记到 agent_templates。

只读取与解析，绝不执行文件内容。扫描范围限定在配置的库根目录内。
"""
from pathlib import Path

import aiosqlite

from config import load_settings
from database import get_db_path

# 非角色目录：示例 / 工具集成 / 策略文档 / 资源 / 脚本 / 版本控制
EXCLUDED_DIRS = {"examples", "integrations", "strategy", "assets", "scripts", ".git", ".github", "node_modules"}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML-ish frontmatter（--- 包裹的简单 key: value）+ 返回正文。

    不依赖 yaml 库：模版 frontmatter 都是单层 key: value，手解析足够且更稳。
    返回 (meta dict, body)。无合法 frontmatter 时返回 ({}, 原文)。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    body_start = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        line = lines[i]
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip('"').strip("'")
    if body_start is None:
        return {}, text          # 没有闭合的 ---，视为无 frontmatter
    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def _slug_and_division(md_path: Path, root: Path) -> tuple[str, str]:
    rel = md_path.relative_to(root)
    parts = list(rel.parts)
    division = parts[0] if len(parts) > 1 else ""
    # slug 用文件名 stem（库内文件名已含分类前缀，足够唯一）；
    # 若多层子目录下重名，前缀子目录名消歧。
    stem = md_path.stem
    mid = parts[1:-1]  # division 与文件名之间的子目录（如 game-development/unity/）
    slug = "-".join([*mid, stem]) if mid else stem
    return slug, division


def scan_templates_from_disk(root_dir: str) -> tuple[list[dict], int]:
    """扫描磁盘，返回 (templates, skipped_count)。纯读取，无 DB 操作。"""
    root = Path(root_dir)
    if not root.exists():
        return [], 0
    templates: list[dict] = []
    skipped = 0
    for md_path in root.rglob("*.md"):
        rel_parts = md_path.relative_to(root).parts
        if any(p in EXCLUDED_DIRS for p in rel_parts[:-1]):
            continue
        # 顶层 README 等非角色文件：无 division 且常无 frontmatter，靠解析失败兜底跳过
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        meta, body = parse_frontmatter(text)
        if not meta.get("name"):
            skipped += 1
            continue
        slug, division = _slug_and_division(md_path, root)
        templates.append({
            "slug": slug,
            "name": meta.get("name", ""),
            "division": division,
            "description": meta.get("description", ""),
            "emoji": meta.get("emoji", ""),
            "color": meta.get("color", ""),
            "source_path": str(md_path),
            "body": body,
        })
    return templates, skipped


async def rescan(root_dir: str | None = None) -> dict:
    """扫描并幂等 upsert 到 agent_templates。返回计数摘要。"""
    root_dir = root_dir or load_settings().agent_library_dir
    templates, skipped = scan_templates_from_disk(root_dir)
    inserted = updated = 0
    async with aiosqlite.connect(get_db_path()) as db:
        for t in templates:
            cur = await db.execute("SELECT id FROM agent_templates WHERE slug = ?", (t["slug"],))
            exists = await cur.fetchone()
            if exists:
                await db.execute(
                    """UPDATE agent_templates SET name=?, division=?, description=?,
                       emoji=?, color=?, source_path=?, body=? WHERE slug=?""",
                    (t["name"], t["division"], t["description"], t["emoji"],
                     t["color"], t["source_path"], t["body"], t["slug"]),
                )
                updated += 1
            else:
                await db.execute(
                    """INSERT INTO agent_templates
                       (slug, name, division, description, emoji, color, source_path, body)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (t["slug"], t["name"], t["division"], t["description"],
                     t["emoji"], t["color"], t["source_path"], t["body"]),
                )
                inserted += 1
        await db.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "total": inserted + updated}


async def count_templates() -> int:
    async with aiosqlite.connect(get_db_path()) as db:
        cur = await db.execute("SELECT COUNT(*) FROM agent_templates")
        row = await cur.fetchone()
        return row[0] if row else 0
