"""一次性清理：删除「团队自我介绍 / 项目 Kickoff」这类已删任务残留在成员记忆里的孤儿条目。

背景：这些老条目写入时还没有任务 ID 标记（本次改动才引入），无法按 ID 精准定位，
故按内容特征匹配——只清 recent 段落里标题为「自我介绍…」「…Kickoff…团队 Show」的块。
明确不动任何 knowhow（如数据工程师的经验来自仍存活的任务 43，与介绍无关）。

用法：
    python scripts/purge_orphan_intro_memory.py            # 预览（dry-run，不写）
    python scripts/purge_orphan_intro_memory.py --apply    # 实际执行
"""
import re
import sys
from pathlib import Path

# 让脚本能 import 到 backend 根目录的模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import _managed_body, upsert_managed_section, read_memory  # noqa: E402
from config import load_settings  # noqa: E402

# 命中即视为「团队介绍/Kickoff」孤儿的 recent 块标题特征
ORPHAN_TITLE_RE = re.compile(r"自我介绍|团队\s*Show|Kickoff\s*团队|团队自我介绍")


def _iter_memory_slugs():
    root = Path(load_settings().memory_dir)
    for p in sorted(root.glob("*.md")):
        if p.stem == "README":
            continue
        yield p.stem


def purge_one(slug: str, apply: bool) -> list[str]:
    """返回被删（或将删）的 recent 块标题列表。"""
    body = _managed_body(read_memory(slug), "recent")
    if not body:
        return []
    head_m = re.match(r"(##[^\n]*\n+)?(.*)", body, re.DOTALL)
    head = (head_m.group(1) or "").strip()
    rest = head_m.group(2) or ""
    blocks = re.findall(r"### .*?(?=\n### |\Z)", rest, re.DOTALL)
    kept, removed_titles = [], []
    for b in blocks:
        title = b.splitlines()[0][4:].strip() if b.strip() else ""
        if ORPHAN_TITLE_RE.search(title):
            removed_titles.append(title)
        else:
            kept.append(b.strip())
    if not removed_titles:
        return []
    if apply:
        new_body = (head + "\n\n" + "\n\n".join(kept)).strip() if kept else ""
        upsert_managed_section(slug, "recent", new_body)
    return removed_titles


def main():
    apply = "--apply" in sys.argv
    total = 0
    for slug in _iter_memory_slugs():
        removed = purge_one(slug, apply)
        if removed:
            total += len(removed)
            mark = "已删" if apply else "将删"
            for t in removed:
                print(f"[{mark}] {slug}: {t}")
    if total == 0:
        print("没有发现孤儿介绍条目。")
    elif not apply:
        print(f"\n共 {total} 条（预览）。加 --apply 实际执行。")
    else:
        print(f"\n完成，共清理 {total} 条。")


if __name__ == "__main__":
    main()
