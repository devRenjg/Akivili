"""把 Agent 的工作区与 Skills 使用说明同步进其记忆（受管段落）。

记忆按 slug 全局共享，而工作区是项目维度的——因此“工作区”段落列出该 Agent
当前所在的**所有**项目（项目名→路径+约束）。“可用 Skills”段落列出启用的每个
Skill（名称+描述+何时调用）。两段都从 DB 真实状态实时重建，幂等。

触发点：加入项目 / 移除 / 配 Skills 后调用 sync_agent_memory(slug)。
"""
from config import is_test_project
from database import get_connection
from memory import upsert_managed_section


async def _workspace_body(db, slug: str) -> str:
    cur = await db.execute(
        """SELECT p.title, p.local_path
           FROM project_agents pa JOIN projects p ON p.id = pa.project_id
           WHERE pa.slug = ? ORDER BY p.title""", (slug,))
    rows = await cur.fetchall()
    # 测试项目不进入 Agent 记忆的工作区段落
    rows = [r for r in rows if not is_test_project(r["title"])]
    if not rows:
        return ""
    lines = ["## 🗂️ 工作区（系统维护，请遵守）", "",
             "你在以下项目中工作。**只能在对应项目的本地路径内操作文件，不得越界到其他目录**："]
    for r in rows:
        lines.append(f"- **{r['title']}** → `{r['local_path']}`")
    lines.append("")
    lines.append("开始任何任务前，先确认当前任务属于哪个项目，并把操作限定在该项目路径内。")
    return "\n".join(lines)


async def _skills_body(db, slug: str) -> str:
    cur = await db.execute(
        """SELECT s.name, s.description, s.body
           FROM agent_skills a JOIN skills s ON s.slug = a.skill_slug
           WHERE a.agent_slug = ? ORDER BY s.name""", (slug,))
    rows = await cur.fetchall()
    if not rows:
        return ""
    lines = ["## 🧩 可用 Skills（系统维护）", "",
             "你已被赋予以下 Skill。遇到对应场景时，**主动调用对应 Skill 的能力指令**来完成工作："]
    for r in rows:
        desc = r["description"] or "（无描述）"
        lines.append(f"- **{r['name']}**：{desc}")
        # 取正文首行作为“何时/如何使用”的提示，避免把整段塞进记忆
        first = next((ln.strip() for ln in (r["body"] or "").splitlines() if ln.strip()), "")
        if first:
            lines.append(f"  - 使用要领：{first}")
    lines.append("")
    lines.append("判断要做的事匹配某个 Skill 时，按其能力指令执行；不确定是否适用时，优先参考 Skill 描述。")
    return "\n".join(lines)


async def sync_agent_memory(slug: str) -> None:
    """重建该 Agent 记忆里的“工作区”和“可用 Skills”受管段落。"""
    db = await get_connection()
    try:
        ws = await _workspace_body(db, slug)
        sk = await _skills_body(db, slug)
    finally:
        await db.close()
    upsert_managed_section(slug, "workspace", ws)
    upsert_managed_section(slug, "skills", sk)
