"""Agent 记忆基础设施：每个 Agent 在 memory/<slug>.md 维护一份持久记忆。

约定（见 memory/README.md）：Agent 开工先读自己的记忆恢复上下文与做事要领，
收工把思考/结论/进度/信息写回。本模块只做读写与路径安全，自动闭环在执行层（P4）。

路径安全：slug 拼接后 resolve，必须仍在 memory_dir 内，否则拒绝（防 ../ 穿越）。
"""
import re
from pathlib import Path

from config import load_settings

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

MEMORY_README = """# Agent 记忆目录

本目录每个 `<agent-slug>.md` 文件是一个 Agent 的持久记忆，跨项目共用。

## 约定

- **开工先读**：Agent 执行任务前，先读取自己的记忆，恢复上下文、做事方式与要领。
- **收工写回**：任务中的思考、结论、进度、进展、关键信息，写回自己的记忆。
- **记要领**：不只记某次任务的细节，更要沉淀"这类活怎么做更好"的通用要领。

文件名即 Agent 的 slug（与 Agent 库模版 slug 一致）。
"""


def _memory_path(slug: str) -> Path:
    """把 slug 解析为 memory_dir 下的 .md 路径，并校验未越界。"""
    if not slug or ".." in slug or not _SLUG_RE.match(slug):
        raise ValueError("非法的 agent slug")
    root = Path(load_settings().memory_dir).resolve()
    target = (root / f"{slug}.md").resolve()
    if not target.is_relative_to(root):
        raise ValueError("记忆路径越界")
    return target


def ensure_memory_dir() -> None:
    root = Path(load_settings().memory_dir)
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(MEMORY_README, encoding="utf-8")


def read_memory(slug: str) -> str:
    """读取记忆；不存在返回空串（不报错）。"""
    path = _memory_path(slug)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def write_memory(slug: str, content: str) -> None:
    """覆盖写入记忆，自动建目录。"""
    path = _memory_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_memory(slug: str, text: str) -> None:
    """在记忆末尾追加，不覆盖既有内容。"""
    existing = read_memory(slug)
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    write_memory(slug, f"{existing}{sep}{text}")


def upsert_managed_section(slug: str, key: str, body: str) -> None:
    """写入/更新一个“系统受管段落”，用锚点包裹，反复重写不影响用户手写内容。

    锚点形如 <!-- akivili:managed:key:start --> ... <!-- akivili:managed:key:end -->。
    body 为空时移除该段落。受管段落统一置于记忆顶部，便于 Agent 开工即见。
    """
    start = f"<!-- akivili:managed:{key}:start -->"
    end = f"<!-- akivili:managed:{key}:end -->"
    existing = read_memory(slug)

    block = f"{start}\n{body.rstrip()}\n{end}" if body.strip() else ""

    import re
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(existing):
        # 用函数式替换，避免 block 里的反斜杠（如 Windows 路径）被当成正则替换转义
        new = pattern.sub(lambda _m: block, existing)
        # 清理替换后可能残留的多余空行
        new = re.sub(r"\n{3,}", "\n\n", new).strip() + "\n"
    elif block:
        # 新段落放到顶部（受管区在前，用户手写在后）
        new = f"{block}\n\n{existing.lstrip()}" if existing.strip() else f"{block}\n"
    else:
        return  # 无该段落、也无内容可写
    write_memory(slug, new)


# ── 任务级记忆标记与清理 ────────────────────────────────────────────────
# 记忆里与某个任务/卡片绑定的条目，统一用不可见的 HTML 注释标记归属其任务 ID，
# 便于任务删除时精准剔除对应记忆（任务没了 → 沉淀的东西也无效）。
#   - 「近期动态」recent 条目：紧跟 ### 标题后写 <!-- akivili:task:ID -->
#   - 「Know-how」knowhow 条目：行尾写 <!-- akivili:task:ID -->
# 标记是 Markdown 注释，Agent 阅读时不可见、不干扰理解。

def task_marker(task_id: int) -> str:
    """生成某任务的记忆归属标记（HTML 注释，渲染时不可见）。"""
    return f"<!-- akivili:task:{int(task_id)} -->"


def _managed_body(mem: str, key: str) -> str | None:
    """取出某受管段落的正文（不含锚点）；无则 None。"""
    import re
    start = f"<!-- akivili:managed:{key}:start -->"
    end = f"<!-- akivili:managed:{key}:end -->"
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), mem, re.DOTALL)
    return m.group(1).strip() if m else None


def purge_task_memory(slug: str, task_ids: list[int]) -> int:
    """从该 Agent 记忆里删除属于给定任务的条目（recent 块 + knowhow 条目）。

    按 <!-- akivili:task:ID --> 标记匹配。返回删除的条目总数。
    段落被删空时一并移除。不影响用户手写内容与其它任务的条目。
    """
    if not task_ids:
        return 0
    import re
    mem = read_memory(slug)
    if not mem:
        return 0
    markers = {task_marker(t) for t in task_ids}
    removed = 0

    # 1) recent：以 ### 分块，丢弃含目标标记的块
    recent_body = _managed_body(mem, "recent")
    if recent_body:
        # 保留段落标题行（## 开头），仅对 ### 任务块过滤
        head_m = re.match(r"(##[^\n]*\n+)?(.*)", recent_body, re.DOTALL)
        head = head_m.group(1) or ""
        rest = head_m.group(2) or ""
        blocks = re.findall(r"### .*?(?=\n### |\Z)", rest, re.DOTALL)
        kept = []
        for b in blocks:
            if any(mk in b for mk in markers):
                removed += 1
            else:
                kept.append(b.strip())
        new_body = (head.strip() + "\n\n" + "\n\n".join(kept)).strip() if kept else ""
        upsert_managed_section(slug, "recent", new_body)

    # 2) knowhow：逐条 bullet 过滤（行尾带标记的删掉）
    mem = read_memory(slug)  # recent 可能已改，重读
    know_body = _managed_body(mem, "knowhow")
    if know_body:
        lines = know_body.splitlines()
        kept_lines = []
        for ln in lines:
            if ln.strip().startswith("- ") and any(mk in ln for mk in markers):
                removed += 1
                continue
            kept_lines.append(ln)
        # 若过滤后已无任何 bullet，则整段清空
        has_bullet = any(l.strip().startswith("- ") for l in kept_lines)
        new_body = "\n".join(kept_lines).strip() if has_bullet else ""
        upsert_managed_section(slug, "knowhow", new_body)

    return removed
