# agent-session-resume (delta)

## ADDED Requirements

### Requirement: per-(conversation, agent) CLI 会话复用与串行

系统 SHALL 为每个 `(conversation_id, agent_slug)` 维护一条 CLI session,持久化于 `agent_sessions` 表（唯一键 `(conversation_id, agent_slug)`）。同一 Agent 在同一 task 里再次被触发执行时,系统 SHALL 复用该 session（CLI resume）,而非每次新建会话。session 记录 SHALL 含 `session_id`、`committed_msg_id`（上次成功执行确认的增量水位）、`provider_id`、`backend`、`workdir`;本次执行的快照终点 `planned_through_msg_id` SHALL 落在 `task_runs` 行。粒度 SHALL 为 (conversation, agent)——同一 task 内每个成员各持独立 session,互不干扰。**active 串行唯一索引 SHALL 与 session owner 粒度对齐为 `(conversation_id, agent_slug)`（Review 第五轮 P1-5 拍板方案 B）**：session 键是 `(conversation_id, agent_slug)`，若 active 唯一索引用 `(task_id, agent_slug)`，仅当 `tasks.conversation_id` 被约束为一对一且不可复用时两者才等价;现状 `tasks.conversation_id` 可空且无唯一约束（一个 conversation 可挂多个 task），会出现同一 conversation 的两个 active run 争抢同一 session owner。故系统 SHALL 保证同一 `(conversation, agent)` 至多一个 active run（active = queued/claimed/running），DB 级唯一性 SHALL 用 `partial unique index ON run_queue(conversation_id, agent_slug) WHERE status IN ('queued','claimed','running')` 保证（应用层查重有 TOCTOU 竞态，不足以防两个并发 POST 各插一条）。**`run_queue` 现状无 `conversation_id` 列**，迁移 SHALL 先 `ALTER ADD COLUMN conversation_id`（从 `tasks.conversation_id` 回填存量、新写入路径强制随 task 带入），再建上述 conversation 粒度 partial unique index;建索引前 SHALL 先归并/取消存量同 `(conversation, agent)` 多 active 行。重复触发 SHALL **折叠**进下一轮（合并触发意图，不丢弃），而非并行起第二条;折叠 SHALL 用「queued 原子合并水位 / claimed·running 记 pending intent、收尾据 pending 建至多一个 successor」实现。全局并发上限约束的是不同 (conversation, agent) 的并行度,与此串行正交。**NULL 路径 SHALL 用两组互补 partial unique index、SHALL NOT「不约束」（Review 第六轮 P1-1）**：
```sql
UNIQUE(conversation_id, agent_slug)
  WHERE conversation_id IS NOT NULL AND status IN ('queued','claimed','running');
UNIQUE(task_id, agent_slug)
  WHERE conversation_id IS NULL     AND status IN ('queued','claimed','running');
```
新业务 run SHALL 强制带 `conversation_id`;无法回填 `conversation_id` 的历史/系统 run SHALL 先隔离清理。**第二组 `(task_id, agent_slug) WHERE conversation_id IS NULL` 索引的定位 = 存量脏数据并发防御约束（保证即便历史遗留 NULL active 行也至多一个），SHALL NOT 被理解为「NULL run 可执行的 task 级串行兜底」**——`conversation_id IS NULL` 的 task 一律不可执行（见「可执行 task 必须拥有非 NULL conversation」Requirement 的三层硬门），该索引只防存量脏数据，不代表 NULL run 有合法执行口径;SHALL NOT 留「不约束」空档。

**🔴 task:conversation SHALL 固化为一对一 DB 不变量（Review 第七轮 P0-3，用户拍板）**：现有业务代码正常创建路径（`routes/tasks.py` 的 `create_task`/`create_subtask`）**每个 task/subtask 都新建独立 conversation**，即产品事实已是 1:1;但 `tasks.conversation_id` 现状可空且无唯一约束，规格上一个 conversation 理论可挂多个 task，会让 `(conversation, agent)` 粒度的**单 pending intent 压掉或错投跨 task 触发**（同 conversation 的 task B 触发被折叠进 task A 的单个 `rerun_requested/pending_through_message_id`，丢掉 B 的 task_id/prompt/idempotency key/优先级/先后）。故 SHALL 把该事实固化为 DB 约束：
```sql
CREATE UNIQUE INDEX ... ON tasks(conversation_id) WHERE conversation_id IS NOT NULL;
```
固化后 conversation 粒度与 task 粒度实际一一对应，现有单 pending intent 才安全。**若未来产品确需多 task 共享 conversation**，SHALL 改模型（二选一）：① active 串行只限制 `claimed/running`、允许多个 queued execution 排队;② 新增 FIFO `pending_intents` 表至少存 `task_id/prompt/source/idempotency_key/created_at`，SHALL NOT 只留一个布尔/水位槽。当前拍板走 1:1 DB 约束，不改模型。

**历史重复迁移 SHALL 是可执行、可回滚的完整流程（Review 第八轮 P1-D，「迁移前清理历史重复」不足以直接实施）**：
1. **dry-run 查询**：先输出所有重复 `conversation_id` 及其关联的 task、messages、run_queue、task_runs、agent_sessions（人工可审），SHALL NOT 静默改数据。
2. **保留规则确定性**：明确重复中哪个 task 保留原 conversation（如最早创建/有 active run 的 task），规则 SHALL 确定、可复现。
3. **其他 task 处置（隔离必须让 UNIQUE index 可创建，Review 第九轮）**：partial unique index 谓词是 `WHERE conversation_id IS NOT NULL`，因此「隔离阻断」若保留原重复 `conversation_id` 值，第 5 步建 index 仍会因重复而失败。隔离 SHALL 落到具体列值二选一——① **隔离 task 的 `conversation_id` 置 `NULL`**（退出 partial index 覆盖），同时把原值存入新增 lineage 列 `quarantined_from_conversation_id`（保留血缘、可人工追溯/回迁），该 task 由 `(task_id, agent_slug) WHERE conversation_id IS NULL` 索引**仅作存量脏数据并发防御（不可执行、非可执行兜底，第十二轮 P0-A）**;② 或**为隔离 task 新建独立 conversation 并迁移其消息/执行历史**（使 conversation 粒度与 task 粒度重新一一对应）。当前默认走 ①（置 NULL + lineage，最小侵入、不复制历史）;需要保留独立会话上下文时人工选 ②。SHALL NOT 只在业务层「标记需人工处理」而不改 `conversation_id` 列值——否则 index 建不出来。

   **🔴 走 ① 的 quarantined task（`conversation_id=NULL` + lineage）SHALL NOT 自动执行，人工恢复唯一路径 = 迁移到新 conversation（Review 第十轮 P1-2 + 第十一轮 P1-C 收口）**：ASR 历史读取固定 `messages WHERE conversation_id=?`，且 `messages` 无通用 `task_id`——直接以 NULL 执行读不到历史、直接用 `quarantined_from_conversation_id` 读又会把**原共享 conversation 里其他 task 的消息**一起喂入。因此 quarantined task **不进 claim/dispatch 自动执行**，只走人工出口：**人工把该 task 迁移到一个新独立 conversation**（走上面 ②），在**可审计的消息切分**后把归属本 task 的消息迁入新 conversation（**切分无法可靠判定时保持 quarantine、不允许「猜测迁移」**），迁移后该 task 有真 conversation、进入 1:1 约束、正常 full replay/resume。**第十轮设想的「指定 `history_source_conversation_id` + 消息切分规则、只喂本 task 消息」缺数据结构支撑（messages 无 task_id、结果消息无法带 NULL conversation 落库），第十一轮已删除该可执行口径**——统一为「无真 conversation 不可执行、恢复必先建真 conversation」。见「可执行 task 必须拥有非 NULL conversation」Requirement。
4. **不串档**：迁移 SHALL 保证 `agent_sessions`、pending intent、`run_queue`、`task_runs` 归属正确 task/conversation，SHALL NOT 把 A 的 session/run 串到 B。
5. **原子回滚**：`UNIQUE INDEX` 创建 SHALL 在清理事务成功后进行;创建失败 SHALL 整体回滚，不留部分重写。
6. **备份 + 完整性校验**：迁移前 SHALL `VACUUM INTO`/online backup 安全备份，迁移后运行完整性探针核对无孤儿/串档。

**pending intent SHALL NOT 因 poisoned/failed 而丢失（Review 第七轮 P0-3）**：区分「自动恢复 successor」与「执行期间新到达的外部用户 intent」——
- 自动恢复 successor：poisoned failure 时不建（禁自动重试）。
- **外部 pending intent：即便 poisoned，清 session 后仍 SHALL 建一个 `full_replay` 普通 successor 承接**（poisoned 可禁自动重试，但不能丢用户在执行期间新下的指令）。
- 用户 kill 是唯一明确取消 pending intent 的路径。

**🔴 可执行 task 必须拥有非 NULL conversation，NULL/quarantined task 一律不可执行（Review 第七轮 P1-2 + 第十一轮 P1-C 收口）**：根因是数据模型——`messages` 表 `conversation_id NOT NULL`、**无通用 `task_id` 列**（`run_id` 只覆盖 run 产出的 assistant 消息，人工 user 消息 `run_id` 常为 NULL），且 `agent_sessions` 唯一键只有 `(conversation_id, agent_slug)`、无 task_id。因此「NULL task 从本 task 读取/写入消息」**既无可靠查询条件、结果消息也无法合法落库**（写入必须带 conversation_id），第十轮设想的「以本 task 归属消息 full replay」与「`history_source_conversation_id` + 消息切分」缺数据结构支撑、SHALL NOT 作为可执行口径。故 SHALL 明确：① **新业务 run 强制 `conversation_id` 非空**（正常创建路径已满足，见 task:conversation 1:1）;② **所有可执行 task 必须拥有非 NULL conversation**——`conversation_id IS NULL` 的 task（含无会话归属的系统/历史 task 与迁移隔离的 quarantined task）**一律不进入 claim/dispatch、不可自动执行**，此约束 SHALL 落为**三层硬门**（Review 第十二轮 P0-A，任一层单独即可挡住，三层纵深防御）：**(1) dispatch 拒绝**——`POST /tasks/{id}/dispatch` 创建可执行 run 前 SHALL 校验目标 task `conversation_id IS NOT NULL`，为空则拒绝入队并返回结构化人工迁移原因，SHALL NOT 入队一个 NULL run;**(2) scheduler 排除**——调度器/auto-dispatch 扫描候选 task 时 SHALL 在查询谓词内 `AND conversation_id IS NOT NULL`，NULL task 不进入候选集;**(3) claim CAS 排除**——原子 claim 的 CAS 谓词 SHALL 含 `AND conversation_id IS NOT NULL`，即便脏数据绕过前两层入了队，也无法被 claim 成 running;③ 人工恢复一个 NULL/quarantined task 时，SHALL **创建独立 conversation** 并在**可审计的消息切分**后迁移对应消息（切分无法可靠判定时保持 quarantine、**不允许「猜测迁移」**），迁移后该 task 有真 conversation、进入 1:1 约束、正常 full replay/resume;④ **存量 `conversation_id IS NULL` 的 queued/claimed/running 行 SHALL 在 activate 前按状态分别收尾（Review 第十三轮 P1-A：三态不能用同一个「隔离」动作，见下方「activate 前存量 NULL 在途行三态迁移状态机」Requirement）、并经 `active NULL count=0` gate 才放行**，SHALL NOT 让其在硬门上线后停留在可执行状态、SHALL NOT 只更新 task/conversation 或让 scheduler 忽略而放任旧 Worker 继续执行（幽灵运行）;⑤ 若未来确需支持永久 NULL task，SHALL 新增明确的消息 scope（如 `message_scope_type + message_scope_id` 或可空 `task_id`）并定义 user/assistant/system/tool 全部读写 SQL，SHALL NOT 只加一个 `history_source_conversation_id` 指针。当前**不引入**永久 NULL task 模型，统一走「可执行必有真 conversation」。

#### Scenario: 首次执行建立 session
- **WHEN** 某 Agent 在某 task 首次被触发执行（`agent_sessions` 无该 (conversation, agent) 行）
- **THEN** 系统开新 CLI 会话（不带 resume）、执行成功后把 CLI 返回的 `session_id`、`provider_id`、`backend`、`workdir` 落 `agent_sessions`，并把本次成功喂到的水位作为 `committed_msg_id` 落库

#### Scenario: 再次执行复用 session
- **WHEN** 同一 (conversation, agent) 再次被触发,且已有 session 且 backend/provider/workdir 未变
- **THEN** 系统以 resume 启动 CLI,复用上次会话上下文,不新建会话

#### Scenario: 同 (conversation, agent) 串行
- **WHEN** 某 (conversation, agent) 已有 active run（queued/claimed/running），此时该成员在同一 conversation 被再次触发
- **THEN** partial unique index（`conversation_id` 非空组）挡下并发插入的第二条 run，触发被折叠进下一轮（queued 合并水位 / claimed·running 记 pending intent，收尾据 pending 建至多一个 successor），保证增量边界有确定推进点

#### Scenario: NULL conversation 脏数据并发被存量防御索引挡下（不代表可执行，Review 第十二轮 P0-A）
- **WHEN** 存量历史遗留某 `conversation_id IS NULL` 的行，同 `(task, agent)` 出现并发写入
- **THEN** 第二组 `UNIQUE(task_id, agent_slug) WHERE conversation_id IS NULL` 索引保证至多一个 NULL active 行、防止脏数据并发放大;但该 NULL task **仍不可执行**——调度器/dispatch/claim 三层硬门（见下方 Requirement）拒绝 `conversation_id IS NULL` 的 task 进入执行，该索引 SHALL NOT 被理解为「NULL run 走 task 兜底串行后即可执行」;人工恢复唯一路径 = 迁移到新独立 conversation

#### Scenario: task:conversation 一对一 DB 约束拒绝共享
- **WHEN** 迁移建立 `UNIQUE INDEX ON tasks(conversation_id) WHERE conversation_id IS NOT NULL` 后，尝试让两个 task 复用同一 conversation_id
- **THEN** DB 唯一约束拒绝第二次写入;迁移前历史重复已清理，使 conversation 粒度与 task 粒度一一对应，单 pending intent 安全

#### Scenario: 历史重复迁移可 dry-run 且不串档
- **WHEN** 构造历史上共享同一 conversation_id 的多个 task，运行迁移
- **THEN** dry-run 先输出所有重复 conversation 及关联 task/messages/run_queue/task_runs/agent_sessions 可审;按确定性规则保留一个 task、其余隔离或新建 conversation 迁移历史;迁移不把某 task 的 session/pending/run 串到另一 task

#### Scenario: UNIQUE index 创建失败整体回滚
- **WHEN** 迁移清理 + 建 `UNIQUE INDEX` 过程中任一步失败（如仍有未清理重复）
- **THEN** 整个迁移事务回滚，不留部分重写;迁移前已 `VACUUM INTO`/online backup 备份，迁移后完整性探针核对无孤儿/串档

#### Scenario: 隔离 task 置列值后 UNIQUE index 可成功创建（Review 第九轮）
- **WHEN** 多 task 共享同一 conversation_id，按保留规则留一个 task 原值、其余 task 隔离——把隔离 task 的 `conversation_id` 置 `NULL` 并写入 `quarantined_from_conversation_id` lineage（或为其新建独立 conversation）
- **THEN** partial unique index（`WHERE conversation_id IS NOT NULL`）覆盖的行不再有重复 conversation_id，`UNIQUE INDEX ON tasks(conversation_id)` 成功创建;隔离 task 的 `(task_id, agent_slug) WHERE conversation_id IS NULL` 索引仅作存量脏数据并发防御（**该 task 不可执行**，第十二轮 P0-A）;lineage 列可人工追溯/回迁;SHALL NOT 出现「业务层标记隔离但 conversation_id 仍为重复值导致 index 创建失败」

#### Scenario: 无真 conversation 的 task 一律不可执行（Review 第十一轮 P1-C）
- **WHEN** 任一 `conversation_id IS NULL` 的 task（quarantined task，或无会话归属的系统/历史 task）被调度器扫描或被触发
- **THEN** 该 task **不进入 claim/dispatch、不可自动执行**、不自动 full replay;系统仅在 Runtime 提供人工出口（迁移到新 conversation）;SHALL NOT 以 NULL 空历史执行、SHALL NOT 直接用 lineage conversation 的全部消息执行、SHALL NOT 靠不存在的 `task_id` 查询「本 task 消息」

#### Scenario: NULL task 三层硬门纵深拦截（Review 第十二轮 P0-A）
- **WHEN** 分别在 dispatch、scheduler、claim 三个入口尝试执行一个 `conversation_id IS NULL` 的 task（含构造脏数据直接插入 queued 行的情形）
- **THEN** ① dispatch 校验拒绝入队并返回结构化人工迁移原因;② scheduler 查询谓词 `AND conversation_id IS NOT NULL` 使其不进候选集;③ claim CAS 谓词 `AND conversation_id IS NOT NULL` 使脏数据绕过前两层也无法被 claim 成 running;三层任一单独即可挡住，纵深防御下 NULL task 零执行

### Requirement: activate 前存量 NULL 在途行三态迁移状态机

activate 三层硬门前，库中可能已有 `conversation_id IS NULL` 的在途行。三层硬门只挡**新领取/新入队**，不会停止**既有进程**;故 `queued`/`claimed`/`running` 三态 SHALL NOT 用同一个笼统「隔离」动作收尾（Review 第十三轮 P1-A）——否则 `running` NULL 的旧 CLI 仍在跑、仍会写消息/持 session owner，形成迁移后的幽灵运行。SHALL 按状态分别落到合法收尾态，并以 `active NULL count=0` 作为 activate gate：

```text
1. 先部署 compatibility release，关闭 NULL 新入队。
2. 停止旧版本 Worker 的新 claim/launch。
3. queued  NULL → 移出候选集 + 标记 quarantine（保留原始 intent 供人工迁移，不丢）。
4. claimed NULL → attempt 落 `abandoned` + retire session owner（epoch+1）+ 清 run_queue.current_attempt_id + execution 标记 quarantine（CLI 从未起、无残留进程）。
5. running NULL → 先 generation/attempt fencing → kill/containment 确认完整进程树退出
                 → attempt 落 `orphaned`（进程未确认退出口径）/迁移专用终态
                 → execution 落人工隔离态（保留 pid/create_time/generation/instance/containment 待人工）;
                 进程树未确认清理时 SHALL 保持人工隔离、SHALL NOT 假装迁移完成。
6. 校验 active NULL count（queued/claimed/running 且 conversation_id IS NULL）= 0。
7. count 非 0 → activate/readiness fail-closed，不开新状态写入与三层硬门。
8. 通过后才开启新状态写入和三层硬门。
```

quarantine 的实际字段/状态 SHALL 明确（如复用 `quarantined_from_conversation_id` lineage 列 + execution 隔离态），`pending`/`session` 指针处置 SHALL 定义（queued 保 pending intent、claimed/running retire owner），从人工迁移后的新 conversation 恢复走「可执行 task 必须拥有非 NULL conversation」Requirement 的人工出口。SHALL NOT 只写产品概念而不定字段。**收尾后旧 NULL 行 SHALL NOT 再被任何 Worker claim/finalize/写平台**（三层硬门 + fencing 双保险）。

#### Scenario: queued NULL 行 activate 前移出候选并保 intent
- **WHEN** activate 前存在 `conversation_id IS NULL` 的 `queued` 行
- **THEN** 移出候选集 + 标记 quarantine，保留原始 intent 供人工迁移;因 CLI 从未起、无残留进程，无需 fencing/kill;SHALL NOT 丢失该 intent

#### Scenario: claimed NULL 行 activate 前 abandon 并 retire owner
- **WHEN** activate 前存在 `conversation_id IS NULL` 的 `claimed` 行（已建 attempt、持 claim lease、CLI 未起）
- **THEN** attempt 落 `abandoned`、retire session owner（epoch+1）、清 `run_queue.current_attempt_id`、execution 标记 quarantine;SHALL NOT 只标记业务隔离而留 attempt/owner 悬挂

#### Scenario: running NULL 行 activate 前须 fencing+杀树确认才隔离
- **WHEN** activate 前存在 `conversation_id IS NULL` 的 `running` 行，其 CLI 仍在执行（持续写 sentinel 文件）
- **THEN** SHALL 先 generation/attempt fencing → kill/containment 确认完整进程树退出 → attempt 落 `orphaned`/迁移专用终态 + execution 人工隔离;进程树未确认清理前 activate SHALL fail-closed（不假装迁移完成、不放任幽灵运行）

#### Scenario: active NULL count 非零则 activate fail-closed
- **WHEN** 三态收尾进行中，仍有 `conversation_id IS NULL` 且状态 active（queued/claimed/running）的行
- **THEN** `active NULL count>0` 使 activate/readiness fail-closed，不开启新状态写入与三层硬门;仅 count=0 时才放行

#### Scenario: 清零后旧 NULL 行不可被 claim/finalize/写平台
- **WHEN** active NULL count=0 通过、activate 完成后，注入一条残留 NULL 行的迟到 claim/finalize/平台写
- **THEN** 三层硬门（claim CAS `AND conversation_id IS NOT NULL`）+ generation/attempt fencing 双双拒绝，旧 NULL 行不被任何 Worker claim/finalize/写平台

#### Scenario: quarantined task 人工迁移到新 conversation 后正常执行
- **WHEN** 人工把 quarantined task 迁移到一个新独立 conversation，并在可审计消息切分后把归属本 task 的消息迁入
- **THEN** 该 task 有真 conversation、进入 1:1 约束，之后按 `messages WHERE conversation_id=<新 conversation>` 正常 full replay/resume，迁入的消息只属于目标 task、不混入原共享 conversation 其他 task;切分无法可靠判定时保持 quarantine、SHALL NOT「猜测迁移」

#### Scenario: NULL/quarantine 迁移全事务提交或全回滚
- **WHEN** 人工恢复创建新 conversation + 更新 task.conversation_id + 迁移 user/assistant 消息 + 修正 session/run 归属
- **THEN** 上述动作 SHALL 全事务提交或全回滚，SHALL NOT 出现「新 conversation 已建但消息未迁」「消息迁一半」等半提交;迁移后完整性探针核对无孤儿/串档

#### Scenario: 跨 task 共享 conversation 的触发不丢不错投
- **WHEN**（1:1 约束下）task A 已 active，其 conversation 不可能再挂 task B——若历史脏数据出现共享，B 的触发
- **THEN** 按拍板 1:1 模型 B 应属独立 conversation、独立 active run，不被折叠进 A 的单 pending intent、不错投给 task A、不丢失;脏数据在迁移清理阶段被隔离

#### Scenario: NULL conversation_id 的 run 三层硬门拒绝执行（Review 第十二轮 P0-A，删旧「NULL 固定 full replay」口径）
- **WHEN** 某系统/历史 run 的 `conversation_id IS NULL` 被 dispatch 提交、被调度器扫描、或被 Worker claim
- **THEN** 三层硬门各自拒绝——① dispatch 插入前拒绝 `conversation_id IS NULL` 的可执行 run;② 调度器扫描排除 `conversation_id IS NULL`;③ claim CAS 谓词含 `AND conversation_id IS NOT NULL`;该 run **不进入任何执行路径**，SHALL NOT 以 NULL「固定 full replay」执行、SHALL NOT 创建或复用 `agent_sessions`;恢复唯一路径 = 人工迁移到新独立 conversation 后按 `messages WHERE conversation_id=<新 conversation>` 正常执行

#### Scenario: poisoned 时外部 pending intent 不丢
- **WHEN** 某 attempt poisoned failure，但执行期间已有用户新触发合并成 pending intent
- **THEN** 自动恢复 successor 因 poisoned 不建，但 pending 的外部新意图 SHALL 清 session 后生成一个 `full_replay` 普通 successor 承接;仅用户 kill 才取消 pending

#### Scenario: failed 时 pending intent 不丢
- **WHEN** execution 落 `failed`（非 kill），执行期间已有用户新触发的 pending intent
- **THEN** SHALL 据 pending 建 successor 承接用户新指令，SHALL NOT 因失败而丢弃执行期间到达的 intent

#### Scenario: 会话粒度隔离
- **WHEN** 同一 task 内两个不同成员各自被触发执行
- **THEN** 两者各用自己 (conversation, agent) 的 session,互不串上下文,且不同成员照旧可并行

### Requirement: session_id 抓取与生命周期

系统 SHALL 从 CLI 输出中提取会话标识并按下述策略维护其生命周期。claude backend SHALL 从 `-p --output-format stream-json` 的 `system`(init)/`result` 行提取 `session_id`;codex backend SHALL 从 app-server 的 `threadId` 提取。生命周期 SHALL 满足：① **流中途首次见到 id 即抢先落库（pin）**,防执行中途崩溃丢指针;② **收尾以本次最新 id 覆盖存**（resume 后 id 可能变,非只存首次）;③ 更新用 **COALESCE 空值保护**（本次没抓到 id 时不清空旧指针）;④ **session owner SHALL 走 acquire/pin/final/retire 四阶段 CAS 协议（Review 第四轮 P0-4；替代原「仅 pin/覆盖 CAS」）**：`agent_sessions` SHALL 含 `session_version`（= **owner epoch**，只在 owner 切换/退休时递增，SHALL NOT 每次 pin 都递增——否则调用方要追踪不断变化的 token）与 `current_task_run_id` 字段。协议分四阶段（见下方「session owner 四阶段协议」Requirement 详述）：**(1) acquire**——新 attempt 启动时按观察到的旧 `session_version` 原子 CAS 把 `current_task_run_id` 改为本 attempt 并生成新 owner epoch;**(2) pin**——流中途只允许 `(current_task_run_id, owner_version)` 同时匹配的 attempt 更新 `session_id`;**(3) final**——成功收尾同事务更新最终 session pointer + committed 水位 + backlog/pending successor;**(4) retire**——attempt 终态后清空 `current_task_run_id`（或推进 owner epoch），使该 attempt 的迟到流事件全部 CAS 失败。仅当写入者仍是当前 owner attempt 时才更新，防上一轮迟到的流事件覆盖下一轮已建立的新 session pointer。

#### Scenario: 流中途 pin 落库
- **WHEN** CLI 流中第一次出现 session_id,该 run 尚未收尾
- **THEN** 系统立即把该 session_id 落库一次,使执行中途崩溃时仍有可用 resume 指针（供平滑重启续跑）

#### Scenario: 每轮覆盖存最新
- **WHEN** 一次 resume 执行成功,CLI 返回的 session_id 与上次不同
- **THEN** 系统以本次最新 session_id 覆盖存储,不保留过期的旧 id

#### Scenario: 空值不清指针
- **WHEN** 某次执行未能从输出中抓到 session_id
- **THEN** 系统用 `COALESCE(?, session_id)` 更新,保留上一次的有效指针,不清空

#### Scenario: 迟到流事件不覆盖新 session
- **WHEN** 上一轮 run 的流事件迟到到达，此时该 (task,agent) 已由新一轮 run（新 task_run_id/generation）建立了新的 session pointer
- **THEN** CAS 条件（task_run_id/generation 不匹配）拒绝迟到写入，新 session pointer 不被旧事件覆盖

### Requirement: session owner 四阶段 CAS 协议

系统 SHALL 用 acquire → pin → final → retire 四阶段协议管理 `agent_sessions` 的 owner，使新 attempt 能原子取得 owner、迟到事件必被拒绝（Review 第四轮 P0-4）。原协议只有 pin/覆盖的校验条件、未定义新 attempt 如何取得所有权——新 attempt 启动时 `current_task_run_id` 仍指向上一 attempt（或为空），若无原子 acquire，新 attempt 第一次 pin 永远不满足 `WHERE current_task_run_id=:this_attempt`;若直接无条件改 owner，又重新引入迟到事件覆盖竞态。四阶段 SHALL 满足：

**(1) acquire（attempt 启动时原子取得 owner）**：**acquire 除 version 匹配外 SHALL 按 owner 资格谓词判定（Review 第五轮 P0-2 / 第六轮 P0-1）——SHALL NOT 仅凭「读到最新 version」或「某个时间戳过期」就从仍在运行的旧 owner 手里抢 owner**。

**lease 字段 SHALL 用统一词汇（Review 第六轮 P0-1：`task_runs` 无 lease 字段，SHALL NOT 引用 `task_runs.lease_expires_at`）**：
- `run_queue.claim_lease_until` = claimed/preparing 阶段的**领取租约**（attempt 尚未起 CLI，claim lease 超时可回收）。
- `worker_state.lease_expires_at` = **Worker generation 心跳租约**（代表整个世代，不等价于某一 attempt 自己的租约）。
- running attempt 的失效 SHALL NOT 只看 claim lease，也 SHALL NOT 直接拿 Worker 世代租约当作该 attempt 的租约——running owner 只有在其 Worker generation lease 已失效、新 generation 已完成 fencing/进程清理或该 attempt 已终态转换后才算失效。

owner acquire 资格谓词 SHALL 固定为五档：

1. `current_task_run_id IS NULL`：可以 acquire。
2. 旧 attempt **已终态**：可按 version CAS acquire。
3. 旧 attempt 为 **claimed/preparing**：仅当 `run_queue.claim_lease_until < :db_now` **且** PGR 回收事务已把旧 attempt 落 `abandoned`、退休 owner 后，新 attempt 才可 acquire。
4. 旧 attempt 为 **running**：仅当 Worker generation lease 已失效、新 generation 已成功 fencing、**且**旧 attempt 已回收/终态、owner 已退休后才能 acquire。
5. ASR acquire **SHALL NOT 自行凭「时间过期」跳过 PGR 回收事务**；正常路径 SHALL 优先等待 PGR 回收事务退休 owner，然后从 `current_task_run_id IS NULL`（档 1）接管。

active owner（旧 attempt 仍 running 且其 Worker generation lease 未失效）SHALL NOT 被普通新 attempt 覆盖。partial unique index 只降低正常路径并发，不能替代 lease 回收/恢复 child/人工修复场景下的 owner 资格判定。
```sql
-- 档 1 / 档 2 的常态接管（旧 owner 无 或 已终态）：
UPDATE agent_sessions
SET current_task_run_id = :new_attempt,
    session_version = session_version + 1
WHERE conversation_id = :conversation
  AND agent_slug = :agent
  AND session_version = :observed
  AND ( current_task_run_id IS NULL
     OR current_task_run_id IN (SELECT id FROM task_runs
                                 WHERE id = agent_sessions.current_task_run_id
                                   AND status IN ('succeeded','failed','killed',
                                                  'abandoned','superseded')) )
RETURNING session_version;   -- 得到本 attempt 的 owner_version
```
（上式只覆盖档 1/档 2——旧 owner 为空或已终态。档 3/档 4 的 claimed/running owner **不在此 SQL 内凭时间戳直接抢**：SHALL 先由 PGR 侧的回收事务——claim lease 超时回收（读 `run_queue.claim_lease_until`）或 Worker generation lease 失效 + fencing——把旧 attempt 落终态并退休 owner，退休后 `current_task_run_id` 归 NULL，本 acquire 再走档 1 接管。`task_runs` 无 lease 字段，SHALL NOT 在 acquire SQL 里读 `task_runs.lease_expires_at`。）**首次无 session 行时** SHALL 用 `INSERT ... ON CONFLICT(conversation_id, agent_slug) DO NOTHING` 插入并归属本 attempt（`current_task_run_id=:new_attempt`、`session_version=1`），插入与归属同一步完成;并发下只有一个 attempt 的 acquire 成功，其余按 `RETURNING` 为空重试观察最新 version。acquire 失败（active owner 未让出）SHALL 让新 attempt 走 lease 回收/排队路径，SHALL NOT 强抢。

**(2) pin（流中途更新 session_id）**：只允许当前 owner attempt 更新：
```sql
UPDATE agent_sessions
SET session_id = COALESCE(:session_id, session_id)
WHERE current_task_run_id = :new_attempt
  AND session_version = :owner_version;
```
`owner_version` = acquire 阶段 `RETURNING` 得到的值;pin **SHALL NOT** 递增 `session_version`（epoch 只在 owner 切换/退休时变），使调用方在整个 attempt 生命周期内持有稳定 owner token。

**(3) final（成功收尾同事务）**：成功收尾 SHALL 在**同一事务**内完成：最终 session pointer 覆盖（同 pin 的 CAS 条件）+ `committed_msg_id` 水位推进 + backlog/pending successor 创建（见 committed 水位与 backlog Requirement）+ **owner retire（阶段 4）**，任一失败整体回滚。**该事务 SHALL 就是 [platform-graceful-restart] 的统一 `finish_execution()` 收尾事务（Review 第五轮 P0-4）**——session final/committed/owner retire 与定局 attempt 终态、execution 终态、terminal/superseded/queued 事件写入在**同一次提交**内完成，SHALL NOT 由 ASR 与 PGR 分别提交而出现「committed 已推进但 execution 仍 active」「session pointer 已更新但无 terminal event」「execution 终态但 successor 丢失」等半提交。final 与 retire SHALL NOT 分两次提交（否则 final 后、retire 前崩溃会残留「终态 attempt 仍可写 owner」）。

**(4) retire（owner 退休）**：attempt 终态后 SHALL 原子退休 owner——清空 `current_task_run_id`（或推进 owner epoch），使该 attempt 后续迟到的 pin/final 全部 CAS 失败。**attempt 终态且无 successor 时也 SHALL retire**，SHALL NOT 保留可写 owner 让迟到事件继续改 session。poisoned/降级清 session 与 owner retire SHALL 同事务（清 session_id 的同时退休 owner，不留「session 已清但 owner 仍可写」的空档）。

**fallback 重建 session 的同 attempt rebind（Review 第五轮 P0-2）**：resume 未落地/provider·workdir 变更/poisoned 需在**同一 attempt 内**重建会话时，SHALL 执行显式 `retire old epoch → reacquire/rebind new epoch → start fresh session` 三步——该 attempt 先退休当前 owner epoch、再以新 epoch 重新 acquire 取得新 owner token，之后新 session 的 pin/final 才用新 owner_version。SHALL NOT 在清旧 session 后直接用旧 owner_version pin 新 session（旧 epoch 已失效，CAS 会全部失败）。

**owner 与 lease reclaim / supersede / kill / poisoned 的同步退休（Review 第五轮 P0-2）**：claimed/running attempt 被 attempt lease 回收（落 `abandoned`）、被 supersede、被 kill、或 poisoned failure 时，SHALL 在同一收尾/回收事务内**同步退休其 session owner**（清 `current_task_run_id`/推进 epoch）;SHALL NOT 出现「attempt 已被回收/终止，但其 session owner 仍可写、迟到事件继续改 session」的空档。新 attempt 的 acquire 资格谓词（阶段 1）依赖此退休已生效，故退休 SHALL 先于或同事务于新 attempt 接管。

#### Scenario: 新 attempt 原子取得 owner
- **WHEN** attempt#1 完成后 attempt#2 启动，`current_task_run_id` 仍指向 attempt#1
- **THEN** attempt#2 按观察到的旧 `session_version` CAS acquire，原子把 `current_task_run_id` 改为 attempt#2、epoch+1，此后 attempt#2 的 pin/final 满足 CAS 条件正常更新 session

#### Scenario: 上一 attempt 迟到 pin/final 被拒
- **WHEN** attempt#2 已 acquire owner，attempt#1 的迟到 pin/final 事件到达
- **THEN** attempt#1 的 CAS 条件（`current_task_run_id`/`owner_version` 不匹配）失败，attempt#2 的 session pointer 不被覆盖

#### Scenario: 终态无 successor 时迟到事件仍被拒
- **WHEN** 某 attempt 已终态、暂无 successor，其迟到流事件到达
- **THEN** 因 retire 已清 `current_task_run_id`/推进 epoch，迟到写 CAS 失败，SHALL NOT 因残留 owner 继续修改 session

#### Scenario: 首次无 session 行并发 acquire 只一个成功
- **WHEN** 某 (conversation, agent) 尚无 session 行，多个 attempt 并发首次 acquire
- **THEN** `INSERT ... ON CONFLICT DO NOTHING` + version CAS 保证只有一个 attempt 取得 owner 并归属，其余观察最新 version 后重试或让位，不产生两个并发 owner

#### Scenario: 拒绝抢占仍 active 的 owner
- **WHEN** 旧 owner attempt 仍 running 且其 Worker generation lease 未失效，新 attempt 尝试 acquire
- **THEN** acquire 资格谓词不满足（owner 非 NULL、未终态、其 Worker 世代未失效/未 fencing），CAS 失败，旧 owner 不被替换，新 attempt 走 lease 回收/排队路径而非强抢

#### Scenario: claimed owner 只按 claim lease 回收
- **WHEN** 旧 owner attempt 处于 claimed/preparing、`run_queue.claim_lease_until` 已过期
- **THEN** 由 PGR claim lease 回收事务把旧 attempt 落 `abandoned`、退休 owner（`current_task_run_id` 归 NULL），新 attempt 再走档 1 acquire；acquire SQL SHALL NOT 读不存在的 `task_runs.lease_expires_at`

#### Scenario: running owner 未 fencing 不得被抢
- **WHEN** 旧 owner attempt 仍 running，其 Worker generation lease 已过期但新 generation 尚未完成 fencing
- **THEN** 新 attempt 不得 acquire（档 4 未满足）；只有新 generation 成功 fencing、旧 attempt 回收/终态、owner 退休后，新 attempt 才唯一 acquire

#### Scenario: lease 字段契约启动校验
- **WHEN** 系统启动校验 acquire/回收 SQL 使用的 lease 字段
- **THEN** 校验 `run_queue.claim_lease_until` 与 `worker_state.lease_expires_at` 均存在且语义唯一，`task_runs` 不含 lease 字段，任何引用 `task_runs.lease_expires_at` 的 SQL 视为契约违规

#### Scenario: fallback 同 attempt 重建 session 先 rebind
- **WHEN** resume mismatch/poisoned 后同一 attempt 需清旧 session 并新建
- **THEN** 该 attempt 先 retire 旧 epoch、再 reacquire 取得新 owner token、才 start fresh session；新 session 的 pin/final 用新 owner_version 成功，旧 epoch 的迟到写全部 CAS 失败

#### Scenario: final 与 retire 同事务不留可写 owner
- **WHEN** attempt 成功收尾，final 更新 pointer/committed/successor 后、retire 前进程崩溃
- **THEN** 因 final 与 retire 在统一 `finish_execution()` 同事务，崩溃整体回滚，不存在「execution 终态但 owner 仍可写」的持久状态

#### Scenario: lease 回收同步退休 owner
- **WHEN** claimed owner attempt 的 `run_queue.claim_lease_until` 过期被回收（落 abandoned），或 running owner 的 Worker generation lease 失效经 fencing 后被回收
- **THEN** 回收事务内同步退休其 session owner，新 attempt 才能唯一 acquire；旧 owner 不再能对 session 迟到写

### Requirement: resume 落地判定与失效降级（不劣于现状）

系统 SHALL 判定 resume 是否真正落地,并在 session 不可用的任何情形下回退到「全量回灌 + 新建会话」,保证行为不劣于改造前。落地判定 SHALL 仅在**执行失败**时把 mismatch 判为未落地：`失败 && (CLI 报会话不存在如 "no conversation found" 或 输出 session_id 与请求不一致)` → 判 resume 未落地。**执行成功但输出 session_id 与请求不一致 SHALL 视为正常**（resume 后 CLI 可能 fork 新 session id），系统 SHALL 接受并覆盖保存新 id，SHALL NOT 判为失败。降级情形 SHALL 覆盖：resume 未落地、provider/backend 变更、workdir 变更、（未来多机）runtime 不匹配。降级 SHALL 对用户无感（自动重开会话、执行正常完成）。

#### Scenario: 失败且 mismatch 才判未落地并回退
- **WHEN** 以 resume 启动 CLI，执行**失败**且 CLI 报会话不存在（或输出 session_id 与请求不一致）
- **THEN** 系统判定 resume 未落地、清除失效 session_id、本次降级为全量回灌 + 新建会话,执行正常完成,新 session_id 落库供后续复用

#### Scenario: 成功返回新 session_id 则接受覆盖存
- **WHEN** 以 resume 启动 CLI，执行**成功**但输出的 session_id 与请求的不一致（CLI fork 了新会话）
- **THEN** 系统视为正常，接受并以新 session_id 覆盖保存，SHALL NOT 判为失败或降级

#### Scenario: provider/workdir 变更弃旧会话
- **WHEN** 某 (conversation, agent) 已有 session,但当前执行的 `provider_id`/`backend`/`workdir` 与存储值不一致
- **THEN** 系统弃用旧 session、以新配置开新会话 + 全量回灌,不误用不兼容的 session

### Requirement: poisoned 失败主动丢弃会话

系统 SHALL 识别会污染会话状态的失败类型（poisoned），命中时**主动丢弃 prior session**,使下次执行从头重建而非 resume 坏状态。poisoned 失败集 SHALL 至少包含：迭代上限耗尽（`iteration_limit`）、模型请求非法（`api_invalid_request` / 400 类）、codex 语义静默超时（`codex_semantic_inactivity`）。非 poisoned 的基础设施型失败（如被平滑重启中断）SHALL 保留 session 供 resume 续跑。

#### Scenario: poisoned 失败丢 session
- **WHEN** 某次执行以 poisoned 原因失败（迭代上限 / 模型 400 / codex 语义静默）
- **THEN** 系统丢弃该 (conversation, agent) 的 prior session,下次执行从头重建,不 resume 已污染的会话状态

#### Scenario: 基础设施失败保留 session
- **WHEN** 某执行因基础设施原因（如平滑重启中断）失败,非 poisoned
- **THEN** 系统保留 session_id,供后续 resume 续跑

### Requirement: 增量上下文回灌（快照水位 + 排除自产）

系统 SHALL 在复用 session 执行时只回灌「增量上下文」,而非全量历史。增量水位 SHALL 用 **2 字段两阶段**：`agent_sessions.committed_msg_id`（上次**成功**执行确认的水位，**只有 run 成功收尾才推进**）+ `task_runs.planned_through_msg_id`（本次构建 prompt 那刻的快照终点 = 当时 `MAX(messages.id)`）。系统 **SHALL NOT** 用「执行完成时的 MAX(id)」做单一水位（并发下会把执行期间别人新写的消息算进而漏话）。增量 = `messages WHERE conversation_id=? AND id > committed_msg_id AND id <= planned_through_msg_id AND 作者非本 agent`（参数化）：别人/人工在上次成功水位之后、本次快照之前说的话喂给本 Agent,本 Agent 自己的历史发言由 CLI session 记忆承载、不重喂。**不变量 = at-least-once**：崩溃/中断时 committed 未推进 → 续跑从同一起点重取 → 重复但不漏;prompt 构建后、CLI 接收前崩溃时 committed **SHALL NOT** 提前推进。**单次海量增量 SHALL 用连续前缀分批，SHALL NOT 用「保新丢旧」裁剪**：现状历史裁剪按保留最新、丢弃较早裁剪，若套在增量上会丢中段较早消息而 committed 仍推进到 planned_through → 中段永久漏喂，违反 at-least-once。增量 SHALL 从 committed 之后最旧一条起、按连续前缀截取本轮可喂量。**committed 水位 SHALL 定义为「原始消息扫描水位」`batch_scan_end`，SHALL NOT 定义为「最后一条实际拼入 prompt 的消息 id」**（Review 第四轮 P0-2）——后者在「尾部消息全是本 Agent 自产、被过滤」时会停在旧位置，使 `committed < planned` 恒成立、无限创建没有历史可投递的空 backlog successor 直到批次上限误转人工。正确语义：

```text
batch_scan_end = 本批已检查并完成取舍的最高原始 messages.id
eligible messages = 扫描区间内真正拼入 prompt 的非自产消息
成功后 committed_msg_id = batch_scan_end   （不是最后一条 eligible 的 id）
```

推进规则 SHALL 覆盖以下边界：
- **区间内全是本 Agent 自产消息**（增量查询为空）：committed **SHALL 直接推进到 `planned_through_msg_id`**，SHALL NOT 创建 successor（无历史可投递）。
- **eligible 尾部之后只剩自产消息**：committed SHALL 跨过这些已扫描的自产记录、推进到 `planned_through_msg_id`，SHALL NOT 把这段空尾部误判为未消费 backlog。
- **遇字符/条数上限**：`batch_scan_end` **只能推进到本批已完整扫描的原始区间末尾**，剩余未扫描区间留下一轮。
- **单条 eligible 消息超字符预算**：SHALL 仍完整投递该条并推进过该条，避免永远卡住。

超上限 SHALL 在连续前缀处截断、分多轮喂，**SHALL NOT 跳段**。「连续前缀」SHALL 指所有 eligible message（排除本 agent 自产后）的连续扫描区间，**不要求原始消息 ID 无空洞**（自产消息造成的 ID 空洞不算跳段）。`batch_scan_end` 可作为 attempt 内的临时计算结果（`task_runs` 行或计算得出），**不必新增第三个 session 主水位**——持久字段仍是 `committed_msg_id` 与 `planned_through_msg_id` 两个，但收尾写 `committed_msg_id` 时 SHALL 写 `batch_scan_end` 而非最后一条 eligible id。**剩余尾部 SHALL 自动续批**：本轮 run 成功且 `batch_scan_end < planned_through_msg_id`（即确有未扫描的原始区间）时，系统 SHALL 在**同一事务**内（连同 committed 推进、session pointer 更新）创建一个 `history_backlog` successor run 继续消费尾部（携带 `trigger=history_backlog`/`history_batch_no`/`history_batch_end`/`history_backlog_from_execution_id`），SHALL NOT 依赖新的用户 @ 才继续;**若 `batch_scan_end` 已达 `planned_through_msg_id`（含全自产/尾部自产已跨过的情形）SHALL NOT 创建 successor**。backlog successor SHALL 不计 mention-chain、受独立最大批次数/token 预算限制、复用同一 CLI session;所有批次消费完才清除 backlog 标记;达批次上限仍未消费完 SHALL 进人工提示而非无限自动跑。**backlog tool-enabled 自动续批的副作用安全线 SHALL 采用方案 A（Review 第五轮 P0-1，用户 2026-07-20 拍板；替代第四轮「仅 prompt 约束轻方案」）**——prompt 约束不能形成工程安全边界（模型仍可能建卡/评论/改文件/调外部接口/发消息，或输出普通 assistant 交付间接触发 mention/通知；且历史需多批时首批只拿到上下文前缀就已开始真实推理执行，后批补历史无法撤销首批基于不完整上下文的决定），故 SHALL 满足下列硬约束：

1. **默认 feature flag 关闭**：`history_backlog` 的 tool-enabled 自动续批 SHALL 置于独立 feature flag 之后、**默认关闭**，SHALL NOT 随阶段 1 基础协议默认开启;它是独立上线门，与基础平滑重启解耦。
2. **observe probe 升级为硬门禁**：`history_backlog_side_effect_observe_probe` SHALL 从「只采集现象」升级为 **release gate 硬断言**——发现工具调用、平台写入、普通 assistant 用户交付、mention/通知触发或任务自动流转即判**测试失败**。
3. **摘要仅辅助理解、不算「投递」，无 safe ingestion 则转人工（Review 第七轮 P1-3，用户拍板；消解第六轮 P1-7 与「先摘要再单次业务 turn」的互斥）**：backlog 自动消费的上线契约 SHALL 二选一明确，SHALL NOT 让摘要既「不算消费」又「承担自动完成消费」两义并存：
   - **有可靠 safe ingestion / context-only turn**：原文**逐批**进入该 session（服务端强制禁工具/禁交付），只有原文完整投递后才推进 committed;摘要仅辅助模型理解、不替代原文投递。
   - **无可靠 safe ingestion**：当原始历史总量超过单 turn 上下文、无法保证原文完整投递时，SHALL **直接转人工/外部检索**，SHALL NOT 声称自动消费完成、SHALL NOT 越过未投递原消息推进 committed。
   两情形下都 SHALL NOT 让首批不完整上下文直接执行真实任务。
4. **「原始消息至少一次完整投递」为硬契约（Review 第六轮 P1-7 + 第七轮 P1-3）**：`committed_msg_id` 推进到某 `batch_scan_end` 的前提 SHALL 是「该区间原始消息已至少完整投递过一次给某个真实/safe-ingestion turn」，SHALL NOT 因「已生成摘要」就跨过原消息推进 committed。摘要记录 SHALL 保留其覆盖的 source message id 区间以便追溯。**超模型上下文的处置**：软预算（本平台字符/条数上限）下 SHALL 连续前缀分批、逐轮完整投递（现状）;单条或整体超模型**硬上下文**且无法在一个/多个 safe turn 内完整投递时，SHALL 停止并转人工/外置为附件·检索，SHALL NOT 对同一不可投递输入无限重试、SHALL NOT 静默丢弃后推进 committed。
5. **仍开启轻方案则明确标注 best-effort 已接受风险**：若产品仍决定启用 tool-enabled 轻方案，文档 SHALL 明确其为 **best-effort**、SHALL 记录「可能重复副作用、可能基于不完整历史决策」的已接受风险，SHALL NOT 写成「副作用防重已解决」。

无论是否启用，backlog successor SHALL NOT 计入 mention-chain、SHALL NOT 触发任务自动流转;副作用防重 SHALL NOT 依赖服务端 exactly-once。首次执行（无 session）SHALL 回灌全量历史（现状行为，同样连续前缀分批 + 自动续批、超上限分轮不丢段不空转）。

#### Scenario: 复用执行只喂增量
- **WHEN** 某 (conversation, agent) 带 session resume 执行,其间别人新增了若干条 messages
- **THEN** 系统只把 `id > committed_msg_id AND id <= planned_through_msg_id` 且非本 agent 自产的新增 messages 拼进 prompt,不重复喂更早历史,也不重喂本 Agent 自己的旧发言

#### Scenario: 并发不漏话
- **WHEN** Agent A 执行期间,Agent B 在同一 conversation 写了发言（其 id 大于 A 本次的 planned_through_msg_id）
- **THEN** B 的发言不在 A 本次增量内，但因 A 的 committed 只推进到本次成功水位（仍在 B 之前）,B 的发言在下次触发 A 时被正确纳入增量,不漏

#### Scenario: 崩溃不漏（at-least-once）
- **WHEN** A 的 prompt 构建后、run 未成功收尾即崩溃/被中断（committed_msg_id 未推进）
- **THEN** 续跑从同一 committed_msg_id 起点重取增量，已喂的消息重复喂但不漏，满足 at-least-once

#### Scenario: 增量为空
- **WHEN** 自上次快照后无他人新 messages（增量为空）
- **THEN** prompt 仅含本轮指令,不含历史片段

#### Scenario: 海量增量连续前缀分批不跳段
- **WHEN** 增量消息条数/字符超单轮上限
- **THEN** 系统从 committed 之后最旧一条起按连续前缀截取本轮可喂量、在本批已完整扫描的原始区间末尾截断（`batch_scan_end`），committed_msg_id 推进到 `batch_scan_end` 而非 planned_through，剩余未扫描尾部下一轮从新起点续喂，中间不跳过任何应喂消息

#### Scenario: 尾部全自产不空转（self-only）
- **WHEN** committed=10、planned=20，且 messages 11..20 全部由本 Agent 自产（增量查询为空）
- **THEN** 本批扫描区间 11..20 已完整取舍完毕、`batch_scan_end=20`，committed 直接推进到 20，SHALL NOT 因「无 eligible 消息可喂」把 committed 停在 10、SHALL NOT 创建任何 backlog successor

#### Scenario: eligible 尾部之后是自产（trailing-self）
- **WHEN** committed=10、planned=20，11..15 为他人 eligible 消息、16..20 为本 Agent 自产
- **THEN** 投递 11..15 后 `batch_scan_end=20`（已扫描过整段），committed 推进到 20，SHALL NOT 只推进到最后一条 eligible（15）而把 16..20 误判为未消费 backlog、SHALL NOT 为空尾部创建 successor

#### Scenario: 无新触发也自动续批消费尾部
- **WHEN** 海量增量需分 3 批消费，且期间没有新的用户 @ 触发
- **THEN** 本轮成功后系统在同事务内自动创建 `history_backlog` successor 继续下一批，逐轮推进 committed，直至全部批次消费完才清 backlog；不依赖新触发、不空转在半消费状态

#### Scenario: backlog tool-enabled 默认关闭
- **WHEN** 阶段 1 基础平滑重启协议上线、backlog feature flag 未显式开启
- **THEN** tool-enabled 自动续批默认关闭，不随基础协议启用；开启需单独 feature flag 与独立上线门

#### Scenario: backlog 副作用探针硬门禁
- **WHEN** 三批历史含可执行旧命令，`history_backlog_side_effect_observe_probe` 运行
- **THEN** 探针作为 release gate 硬断言——发现工具调用、平台写入、普通 assistant 用户交付、mention/通知或任务自动流转即判测试失败，SHALL NOT 只采集不断言

#### Scenario: 多批历史首批不在不完整上下文下执行真实任务
- **WHEN** 完整历史需分三批，关键否定信息位于第二批
- **THEN** 有 safe ingestion 时原文逐批进 session（禁工具/禁交付）后才执行真实任务、才推进 committed;无 safe ingestion 时转人工，首批 SHALL NOT 在只拿到上下文前缀时直接执行真实任务

#### Scenario: 历史超单 turn 上下文且无 safe ingestion 转人工
- **WHEN** 原始历史总量超过单 turn 上下文，且系统无可靠 safe ingestion / context-only turn
- **THEN** 系统 SHALL 转人工/外部检索，SHALL NOT 用摘要冒充完整投递、SHALL NOT 越过未原样投递的原消息推进 committed、SHALL NOT 声称自动消费完成

#### Scenario: 单条超大消息不卡死
- **WHEN** 某单条消息本身超过字符预算（软预算）
- **THEN** 系统至少完整投递该条（必要时该轮只喂这一条），committed 得以越过它继续推进，不永久卡住

#### Scenario: 摘要不替代原消息完整投递
- **WHEN** 系统对某段历史生成聚合/摘要以压缩可见上下文
- **THEN** `committed_msg_id` 只有在该区间原始消息已至少完整投递过一次给真实业务 turn 后才推进;SHALL NOT 因「已生成摘要」就跨过尚未原样投递的原消息推进 committed;摘要记录保留其覆盖的 source message id 区间以便追溯

#### Scenario: 单条超模型硬上下文转人工不静默丢弃
- **WHEN** 某单条原始消息超过模型硬上下文、无法在一个 turn 内完整投递
- **THEN** 系统停止并转人工/外置为附件·检索，SHALL NOT 对同一不可投递输入无限重试、SHALL NOT 静默丢弃后推进 committed 而使该原消息永不投递

#### Scenario: 达批次上限转人工
- **WHEN** backlog 自动续批达到独立设定的最大批次数/预算仍未消费完
- **THEN** 系统停止自动续批、转人工提示，不无限自动跑下去

#### Scenario: 首次执行全量回灌
- **WHEN** 某 (conversation, agent) 首次执行（无 session）
- **THEN** 系统回灌全量历史（与改造前一致），不因缺 session 而丢上下文

### Requirement: backlog 快照冻结与 pending intent 优先级

系统 SHALL 固定 backlog chain 与新用户触发（pending intent）的优先级与继承规则，避免持续新消息使 backlog 永远追逐移动终点、或普通用户指令长期饥饿（Review 第四轮 P1-5）：

1. **backlog chain SHALL 冻结初始 `planned_through_msg_id`**——chain 全程消费同一冻结快照终点，SHALL NOT 每批把 `planned_through` 扩到最新 `MAX(messages.id)`（否则持续有新消息时永远追不完）。
2. **chain 期间的新用户触发 SHALL 只合并进 pending intent**，SHALL NOT 扩张当前 backlog 快照。
3. **backlog 全部批次消费完后 SHALL 至多创建一个普通 successor** 处理 pending intent 的新快照（`planned_through` 取该时刻最新 `MAX`）。
4. **用户 kill SHALL 同时取消 backlog 与 pending**;**superseded 时 backlog 与 pending 均 SHALL 由 recovery child 继承**（不丢未消费尾部、不丢待处理新意图）。

#### Scenario: backlog 消费期间追加新消息不延长当前 chain
- **WHEN** backlog chain 正按冻结快照（planned_through=20）分批消费，期间用户又追加了 msg 21..25
- **THEN** 当前 chain 仍按冻结的 20 有界结束，21..25 折叠进 pending intent，chain 消费完后至多创建一个普通 successor 处理新快照，SHALL NOT 把 21..25 并入当前 chain 使其无限延长

#### Scenario: kill 同时取消 backlog 与 pending
- **WHEN** 某 (task,agent) 存在未消费 backlog chain 且有 pending intent，用户 kill
- **THEN** 系统同时取消 backlog 与 pending，不遗留自动续批或待处理意图

#### Scenario: superseded 时 backlog 与 pending 由 recovery child 继承
- **WHEN** backlog chain 执行中被交棒 superseded，且当时有 pending intent
- **THEN** recovery child 同时继承未消费的 backlog 快照与 pending intent，续跑后既补完历史尾部又处理新意图，不丢任一

### Requirement: backend 分流（claude 与 codex 均必选）

系统 SHALL 支持 claude 与 codex 两个 backend 的 session 复用,均为必选（用户重度使用 codex）。claude backend SHALL 用 `-p --resume <session_id>` flag。codex backend SHALL **每次执行 attempt 启动一个** `codex app-server --listen stdio://` 进程（run 结束即关，**非** Worker 全局共享一个 app-server）+ JSON-RPC `thread/resume`（不可恢复时回退 `thread/start`,传输/进程错误 fail-fast）+ `turn/start`,以 `threadId` 作为其 session 标识。codex `thread/resume` 前系统 SHALL 检查 `CODEX_HOME` 下对应 rollout/thread 记录存在性与 workdir 一致性——记录不存在或 workdir 不一致时 SHALL 降级 `thread/start` 全量、不 resume 到不存在/错配的线程。runner SHALL 依 `agent_sessions.backend` 分流到对应实现;两 backend 共用同一 `agent_sessions` 表、降级链与 poisoned 分类。

#### Scenario: claude 走 flag resume
- **WHEN** 执行 backend 为 claude 且命中可用 session
- **THEN** 走 `--resume <session_id>` + 增量回灌

#### Scenario: codex 走 app-server thread resume
- **WHEN** 执行 backend 为 codex 且命中可用 session（threadId）
- **THEN** 系统先校验 `CODEX_HOME` rollout 存在且 workdir 一致，再经 app-server `thread/resume` 恢复线程续接;线程不可恢复（unknown thread/schema 漂移）时回退 `thread/start` 开新线程并如实标记为新会话

#### Scenario: codex rollout 不存在则降级
- **WHEN** codex 命中 session（threadId）但 `CODEX_HOME` 下对应 rollout 记录不存在或 workdir 不一致
- **THEN** 系统不 resume 到该线程，降级 `thread/start` 全量新建，如实标记为新会话

#### Scenario: codex 传输错误 fail-fast
- **WHEN** codex app-server 出现传输/进程级错误（非协议可恢复错误）
- **THEN** 系统 fail-fast 不回退（app-server 已不可应答）,该 run 按失败处置,下次重建
