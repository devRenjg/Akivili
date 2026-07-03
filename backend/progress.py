"""父子任务进度聚合 + 父任务完成联动。

规则（见验收决策）：
- 有子任务的父任务，只有当**所有子任务都 done** 时才自动置为 `reviewing`（等负责人汇总）；
  负责人汇总收尾后由其手动/协议置 `done`。
- 父任务是否"仍在执行中"= 父任务或其任意子任务，在 run_queue 里还有 queued/running 的 run。
- 执行进度（哪些 Agent 还在跑/排队）聚合父 + 全部子任务，供前端执行日志区展示。
"""
from database import get_connection


async def blocking_subtasks(task_id: int) -> list[dict]:
    """返回该任务下**尚未完成**的子任务（用于阻止父任务提前 done）。
    无子任务或全部 done 时返回空列表。"""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            "SELECT id, title, assignee_slug, status FROM tasks "
            "WHERE parent_task_id=? AND status != 'done'", (task_id,))).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def task_progress(task_id: int) -> dict:
    """聚合父任务 + 其所有子任务的执行进度。

    返回 {
      running: [{task_id, agent_slug, is_sub}], queued: [...],
      sub_total, sub_done, active: bool
    }
    """
    db = await get_connection()
    try:
        subs = await (await db.execute(
            "SELECT id, status FROM tasks WHERE parent_task_id=?", (task_id,))).fetchall()
        sub_ids = [r["id"] for r in subs]
        sub_total = len(subs)
        sub_done = sum(1 for r in subs if r["status"] == "done")

        all_ids = [task_id] + sub_ids
        ph = ",".join("?" * len(all_ids))
        runs = await (await db.execute(
            f"""SELECT task_id, agent_slug, status FROM run_queue
                WHERE task_id IN ({ph}) AND status IN ('queued','running')
                ORDER BY id""", all_ids)).fetchall()
    finally:
        await db.close()

    running, queued = [], []
    for r in runs:
        item = {"task_id": r["task_id"], "agent_slug": r["agent_slug"],
                "is_sub": r["task_id"] != task_id}
        (running if r["status"] == "running" else queued).append(item)

    return {
        "running": running, "queued": queued,
        "sub_total": sub_total, "sub_done": sub_done,
        "active": bool(running or queued),
    }


async def maybe_advance_parent(sub_task_id: int) -> None:
    """某子任务状态变更后调用：若父任务的**所有子任务都已 done**，且父任务还在进行中，
    则推进父任务到 `reviewing`，并**唤醒负责人做总结汇报**（协同闭环的收尾环节）。
    只在"进行中→全完成"这一刻触发一次（reviewing/done 状态不再重复触发）。
    父任务的 `done` 由负责人汇总后自行决定，不在这里直接置 done。"""
    db = await get_connection()
    try:
        row = await (await db.execute(
            "SELECT parent_task_id FROM tasks WHERE id=?", (sub_task_id,))).fetchone()
        parent_id = row["parent_task_id"] if row else None
        if not parent_id:
            return
        subs = await (await db.execute(
            "SELECT id, title, assignee_slug, status FROM tasks WHERE parent_task_id=?",
            (parent_id,))).fetchall()
        if not subs:
            return
        all_done = all(s["status"] == "done" for s in subs)
        parent = await (await db.execute(
            "SELECT title, project_id, assignee_slug, status FROM tasks WHERE id=?",
            (parent_id,))).fetchone()
        parent_status = parent["status"] if parent else ""
        parent_title = parent["title"] if parent else ""
        project_id = parent["project_id"] if parent else 0
        leader_slug = parent["assignee_slug"] if parent else ""
        sub_list = [dict(s) for s in subs]
    finally:
        await db.close()

    # 只在"仍在进行中且全部子任务完成"时收尾；已 reviewing/done 不重复触发
    if not (all_done and parent_status in ("in_progress", "planning", "backlog")):
        return

    # 1) 父任务 → reviewing
    db = await get_connection()
    try:
        await db.execute(
            "UPDATE tasks SET status='reviewing', updated_at=datetime('now') WHERE id=?",
            (parent_id,))
        await db.commit()
    finally:
        await db.close()
    from activity import log_activity
    await log_activity(parent_id, "status_changed", "system", "",
                       {"from": parent_status, "to": "reviewing",
                        "note": "所有子任务已完成，自动进入验证中，唤醒负责人汇总收尾"})

    # 2) 唤醒负责人做总结汇报（把各子任务成果清单喂进 prompt，避免它再去探索）
    if not leader_slug:
        return
    import collab  # 延迟导入避免循环依赖
    done_lines = "\n".join(
        f"- 子任务#{s['id']}「{s['title']}」（负责人 {s['assignee_slug']}）：已完成"
        for s in sub_list)
    summary_prompt = (
        f"任务：{parent_title}\n\n"
        f"【收尾汇报环节】本任务的全部 {len(sub_list)} 个子任务都已完成，现在轮到你（负责人）做总结汇报。\n"
        f"各子任务成果如下：\n{done_lines}\n\n"
        f"请你：\n"
        f"1) 逐个查看各子任务的成果（`jian` 无需再派活，成员都已完成）；\n"
        f"2) 用 `jian comment` 写一段**统一汇总汇报**，把各成员的产出/结论整合成一份完整交付；\n"
        f"3) 汇总完成后执行 `jian status done` 把本任务标记为完成。\n"
        f"**不要再 @ 任何人、不要再建子任务**——这是收尾，不是重新分配。")
    # is_leader=True：注入协作协议+花名册；trigger=collaborate 表明是统筹环节
    await collab.enqueue_run(parent_id, leader_slug, summary_prompt, "collaborate", is_leader=True)
