"""父子任务进度聚合 + 父任务完成联动。

规则（见验收决策）：
- 有子任务的父任务，只有当**所有子任务都 done** 时才自动置为 `reviewing`（等负责人汇总）；
  负责人汇总收尾后由其手动/协议置 `done`。
- 父任务是否"仍在执行中"= 父任务或其任意子任务，在 run_queue 里还有 queued/running 的 run。
- 执行进度（哪些 Agent 还在跑/排队）聚合父 + 全部子任务，供前端执行日志区展示。
"""
from database import get_connection


async def blocking_subtasks(task_id: int) -> list[dict]:
    """返回该任务下**尚在执行中**的子任务（用于阻止父任务提前验收 done）。

    新模型下子任务执行完成后保持 in_progress（不自动 done），故"完成"的判据不再看 status=done，
    而看子任务是否还有排队/运行中的 run。只有仍在执行（queued/running）的子任务才阻止父任务 done；
    已执行完（无待跑 run）的子任务不阻塞——人工验收父任务即代表连同子任务一起验收通过。"""
    db = await get_connection()
    try:
        rows = await (await db.execute(
            """SELECT t.id, t.title, t.assignee_slug, t.status FROM tasks t
               WHERE t.parent_task_id=?
                 AND EXISTS (SELECT 1 FROM run_queue q
                             WHERE q.task_id=t.id AND q.status IN ('queued','running'))""",
            (task_id,))).fetchall()
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
        # 负责人是否已做过收尾汇总：父任务上有一条以负责人身份完成的 collaborate run
        parent = await (await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))).fetchone()
        parent_status = parent["status"] if parent else ""
        summ = await (await db.execute(
            "SELECT 1 FROM run_queue WHERE task_id=? AND is_leader=1 AND trigger='collaborate' "
            "AND status='done' LIMIT 1", (task_id,))).fetchone()
        summarized = bool(summ)
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
        "parent_status": parent_status,
        "summarized": summarized,
    }


async def _has_pending_run(task_ids: list[int], exclude_run_id: int | None = None) -> bool:
    """这些任务里是否还有排队/运行中的 run（判断"是否还在执行"）。

    exclude_run_id: 排除某个 run_queue 行——用于"刚跑完的 run 触发本次检查、但其队列行
    尚未被 _process_one 的 finally 标为 done"的时刻（否则它会把自己算成 pending，
    导致父任务永远等不到"全部完成"、无法自动进验证中/唤醒汇总）。
    """
    if not task_ids:
        return False
    db = await get_connection()
    try:
        ph = ",".join("?" * len(task_ids))
        sql = (f"SELECT COUNT(*) c FROM run_queue "
               f"WHERE task_id IN ({ph}) AND status IN ('queued','running')")
        params = list(task_ids)
        if exclude_run_id is not None:
            sql += " AND id<>?"
            params.append(exclude_run_id)
        row = await (await db.execute(sql, params)).fetchone()
        return bool(row and row["c"])
    finally:
        await db.close()


async def _set_reviewing(task_id: int, note: str) -> bool:
    """把任务从 in_progress/backlog/planning 推进到 reviewing（幂等：已 reviewing/done 不动）。返回是否改动。"""
    db = await get_connection()
    try:
        row = await (await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))).fetchone()
        if not row or row["status"] not in ("in_progress", "backlog", "planning"):
            return False
        old = row["status"]
        await db.execute(
            "UPDATE tasks SET status='reviewing', updated_at=datetime('now') WHERE id=?", (task_id,))
        await db.commit()
    finally:
        await db.close()
    from activity import log_activity
    await log_activity(task_id, "status_changed", "system", "",
                       {"from": old, "to": "reviewing", "note": note})
    return True


async def _set_done(task_id: int, note: str) -> bool:
    """把任务从执行中状态置为 done（幂等）。**不触发经验沉淀**——沉淀只在父任务人工验收时发生。"""
    db = await get_connection()
    try:
        row = await (await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))).fetchone()
        if not row or row["status"] not in ("in_progress", "backlog", "planning", "reviewing"):
            return False
        old = row["status"]
        await db.execute(
            "UPDATE tasks SET status='done', updated_at=datetime('now') WHERE id=?", (task_id,))
        await db.commit()
    finally:
        await db.close()
    from activity import log_activity
    await log_activity(task_id, "status_changed", "system", "",
                       {"from": old, "to": "done", "note": note})
    return True


async def _qa_member_hint(parent_id: int) -> str:
    """若项目团队里有测试/QA/安全类成员，给收尾 prompt 一句「可点名谁验收」的提示（不强制）。
    无则返回空串。判据：slug/name 含 test/qa/测试/安全/验收 等关键词。"""
    db = await get_connection()
    try:
        prow = await (await db.execute(
            "SELECT project_id FROM tasks WHERE id=?", (parent_id,))).fetchone()
        if not prow:
            return ""
        rows = await (await db.execute(
            "SELECT slug, name FROM project_agents WHERE project_id=?", (prow["project_id"],))).fetchall()
    finally:
        await db.close()
    kws = ("test", "qa", "测试", "安全", "验收", "质量")
    qa = [r["name"] for r in rows
          if any(k in (r["slug"] or "").lower() for k in kws) or any(k in (r["name"] or "") for k in kws)]
    if not qa:
        return ""
    return f"（本团队的验收/测试成员：{ '、'.join(qa) }，如需验收可 @ 其中合适的一位）"


async def _advance_and_summarize_parent(parent_id: int) -> None:
    """父任务全部子任务完成时调用：置 reviewing（幂等，作为一次性闸门）并唤醒负责人做统一汇总汇报。

    只有当 _set_reviewing 真正发生状态迁移（in_progress→reviewing）时才唤醒负责人，
    保证汇总只触发一次；若父任务已是 reviewing/done（人工已推进或已汇报过），不重复唤醒。
    """
    changed = await _set_reviewing(
        parent_id, "全部子任务已完成，自动进入验证中，唤醒负责人汇总收尾")
    if not changed:
        return  # 已 reviewing/done：不重复唤醒汇总

    db = await get_connection()
    try:
        parent = await (await db.execute(
            "SELECT title, assignee_slug FROM tasks WHERE id=?", (parent_id,))).fetchone()
        subs = await (await db.execute(
            "SELECT id, title, assignee_slug FROM tasks WHERE parent_task_id=?", (parent_id,))).fetchall()
    finally:
        await db.close()
    leader_slug = parent["assignee_slug"] if parent else ""
    if not leader_slug:
        return  # 无负责人（历史任务）：仅置 reviewing，不唤醒
    parent_title = parent["title"] if parent else ""
    sub_list = [dict(s) for s in subs]

    import collab  # 延迟导入避免循环依赖
    done_lines = "\n".join(
        f"- 子任务#{s['id']}「{s['title']}」（负责人 {s['assignee_slug']}）：已完成"
        for s in sub_list)
    # 是否存在测试/QA 类成员（供负责人按需交付验收）——从项目团队里找带 test/qa 的角色
    qa_hint = await _qa_member_hint(parent_id)
    verify_step = (
        f"1) 逐个查看各子任务的成果；\n"
        f"2) **如果本任务原计划需要测试/验收**（例如你在派活时说过「交测试专员验收」，或改动涉及"
        f"代码/数据正确性需要把关），先用 `jian comment @<验收成员>` 点名相应成员做验收，"
        f"把要验收的范围说清楚，**等其验收反馈后再进入下一步**；{qa_hint}\n"
        f"3) 待验收通过（或本任务无需验收）后，用 `jian comment` 写一段**统一汇总汇报**，"
        f"把各成员的产出整合成一份完整交付（内容较长先写入 .md 再用 `jian comment --body-file <文件>` 发）；\n"
        f"4) 汇总汇报完成即结束——除验收所需的点名外，不要重复派活或重建子任务。")
    summary_prompt = (
        f"任务：{parent_title}\n\n"
        f"【收尾环节】本任务的全部 {len(sub_list)} 个子任务都已完成，现在轮到你（负责人）收尾。\n"
        f"各子任务成果如下：\n{done_lines}\n\n"
        f"请你：\n{verify_step}")
    # is_leader=True：注入协作协议+花名册；trigger=collaborate 表明是统筹收尾环节
    await collab.enqueue_run(parent_id, leader_slug, summary_prompt, "collaborate", is_leader=True)


async def on_execution_complete(task_id: int, exclude_run_id: int | None = None) -> None:
    """某任务的 run 执行成功后调用，处理执行完成后的状态流转（**绝不触发经验沉淀**）：

    - 子任务执行完 → 直接置「完成(done)」（子任务无"验证中"概念），不触发沉淀；
      随后若父任务的全部子任务都已 done → 父任务自动进「验证中」等人工验收。
    - 无子任务的独立顶层任务执行完 → 自身进「验证中」等人工验收。
    经验沉淀 + 已解决计数一律由人工验收（routes/tasks.py 把父/独立任务置 done）触发。

    exclude_run_id: 触发本次调用的 run_queue 行 id。此刻它可能还是 running（其 done 标记
    在 _process_one 的 finally 里、晚于本调用），需从"是否还有待跑 run"判断中排除，
    否则最后一个完成的子任务会把自己算作 pending，父任务永远无法自动收尾。
    """
    db = await get_connection()
    try:
        t = await (await db.execute(
            "SELECT id, parent_task_id, status FROM tasks WHERE id=?", (task_id,))).fetchone()
        if not t:
            return
        parent_id = t["parent_task_id"]
    finally:
        await db.close()

    if parent_id:
        # 子任务：执行完直接置 done（不沉淀）
        await _set_done(task_id, "执行完成，子任务自动完成（等父任务整体验收后一起沉淀经验）")
        # 全部子任务 done 且都无待跑 run → 父任务进「验证中」
        db = await get_connection()
        try:
            subs = await (await db.execute(
                "SELECT id, status FROM tasks WHERE parent_task_id=?", (parent_id,))).fetchall()
        finally:
            await db.close()
        sub_ids = [r["id"] for r in subs]
        all_done = all(r["status"] == "done" for r in subs)
        if all_done and not await _has_pending_run([parent_id] + sub_ids, exclude_run_id):
            # 父任务进「验证中」并唤醒负责人做统一汇总汇报（协同闭环收尾）
            await _advance_and_summarize_parent(parent_id)
    else:
        # 顶层任务：有子任务的由子任务分支推进；此处只处理无子任务的独立任务
        db = await get_connection()
        try:
            sub_ids = [r["id"] for r in await (await db.execute(
                "SELECT id FROM tasks WHERE parent_task_id=?", (task_id,))).fetchall()]
        finally:
            await db.close()
        if sub_ids or await _has_pending_run([task_id], exclude_run_id):
            return
        await _set_reviewing(task_id, "执行完成，自动进入验证中，等待人工验收")


async def maybe_advance_parent(sub_task_id: int) -> None:
    """[保留兼容] 某子任务状态变更后调用：若父任务的**所有子任务都已 done**，且父任务还在进行中，
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
