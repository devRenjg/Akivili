## Why

项目愿景是「通过各种任务让 Agent 学习并成长，做更多任务、协同效率更高，达到 Loop 变强」。但目前我们**无法回答一个根本问题：Agent 真的变强了吗？** 现有信号只有 `solved_tasks`（累计完成数）——那是「量」，不是「变强」。

参考综述 *Self-Improving Agents in the Era of Experience*（Papers/，清华+Frontis.AI，2026-06）指出：让 Agent 部署后变强，本质是 **trace-to-capability（痕迹→能力）问题**——必须能**捕获经验、分配到正确更新面、验证其价值、并保住控制权**。其中 **验证(verification) 是所有能力的底座，也是随 Agent 变强最不易 scale 的能力**。论文提出 **SIP-Bench** 作为「怎么测自改进」的协议层，强调评估必须区分**真实纵向增益** vs **过拟合 / 成本转移(cost transfer) / 隐藏回归(hidden regression)**。

我们已经天然具备 `agent-reflection`（任务完成沉淀 knowhow）+ `agent-memory`（持久记忆）+ `agent-skills` 三个更新面，也每天在产生任务轨迹（`task_runs`/`activities`/`messages`）。**缺的是把这些痕迹组织成「这个 Agent 在成长吗」的度量层。** 没有度量，经验闭环和协同进化都是盲改。

本能力是一个**长期演进的 Feature**：从零成本的可视化快照起步，逐级走向反事实验证。本提案先立骨架与 L1，L2/L3 随讨论持续迭代。

## What Changes

> 本提案为**规划态**（不立即实现，作为持续探讨与分阶段落实的蓝本）。分三级（L1→L3），先立 L1。

- **L1 单 Agent 效率成长快照（近期落地目标）**：以单个 Agent 为主语，用**现有字段零埋点**算出「同类任务效率趋势」+ **knowhow 复用率（核心指标）**，输出只读「成长体检快照」。
- **L2 任务分型 + 基线（L1 的前置依赖）**：分型以**角色 slug 作类型标签**起步（两级可延展，真实数据显示纯标题关键词会撞车），先过滤仪式任务（启发式+run兜底），并**单点先行**（哪个 Agent 先攒够同类任务就先度量谁）。L1 与 L2 是绑定的——无分型则 L1 是噪声。
- **L3 反事实探针（远期，最接近 SIP-Bench）**：拿已解决老任务当回归集，在「带/不带某条 knowhow」两种条件下重跑比对，测因果贡献与负迁移。成本高，需先想清重跑机制。

## Capabilities

### New Capabilities
- `agent-self-improvement-metrics`: 度量单个 Agent（及未来团队）在部署后是否真的变强的能力层。以 knowhow 复用率为核心信号，配合效率代理指标（耗时/返工/求助/对话轮次），输出只读诊断，用于人工观测而非 Agent 奖励。

## Impact

- **规划态，暂不改代码**。未来落实时预计：后端新增 `routes/metrics.py`（只读聚合查询 `task_runs`/`activities`/`messages`/memory knowhow 段）；可能新增任务分型字段（L2）；前端新增只读「成长」视图。
- 关联能力：[agent-reflection]（沉淀 knowhow 的来源）、[agent-memory]（knowhow 存储处）、[agent-skills]（knowhow 升级为 Skill 的去向）、[agent-collaboration]（团队层度量的前置）。
- 关联文档：`Papers/Self-Improving-Agents-Survey-核心思考.md`、`Papers/51177_Self_Improving_Agents_in.pdf`。
