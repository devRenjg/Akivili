# Self-Improving Agents in the Era of Experience: A Survey of Self- to Meta-Evolution

> 清华 / FrontisAI 团队综述（ICML 顶会方向）。解读文章：《一篇 Loop+Harness 的自进化 Agent 最新综述》(PaperAgent 公众号)。

## 论文出处 / 下载

- **PDF**: `51177_Self_Improving_Agents_in.pdf`（本目录，88 页，用户手动下载，已校验为真 PDF）
- **OpenReview**: https://openreview.net/forum?id=IUltZSgLMm
- **发布日期**: 2026-06-25
- **作者/机构**: 清华大学 + Horizon Research, Frontis.AI。一作 Che Jiang，通讯 Ning Ding / Kaiyan Zhang / Bowen Zhou。
- 备注：本目录 `_source_article_wechat_raw.txt` 是公众号解读原文（二手），以下笔记已用 PDF 原文逐章校正。

---

## 一句话主旨

> 让 Agent 在部署后变强，本质是一个 **trace-to-capability（痕迹→能力）问题**：领域必须学会如何**捕获经验、把它分配到正确的更新面(update surface)、验证其价值、并在此过程中保住控制权**。

传统 AI 以静态数据训练为核心；Agentic AI 的进步来自**部署后与环境交互积累的经验**（Silver & Sutton 的 "Era of Experience"：grounded rewards + 长程后果）。论文把"自进化 Agent"形式化为一个**运行时对象**：

```
基础模型(固定) + 可变的 Harness(运行时控制层) + 用户侧 + 环境侧
```

**Harness = 经验流的中介**：决定"用户请求如何变成模型可见的上下文、模型输出如何变成环境动作、交互痕迹如何编译成未来的更新信号"。关键价值：**时间尺度不对称(timescale asymmetry)**——Harness 状态可在部署期被频繁、低成本地检查/修订/治理，而模型权重不能。

**原文关键论断（校正/补强）**：
- 改进何时可信？—— **"当围绕一次改动的证据能持续留给未来的尝试并继续支撑后续行为时，改进才可信。"** 这就是从 Gödel Machine 的"证明门"到经验时代"经验选择"的转变（Darwin Gödel Machine 保留成功后代进 archive）。实践社区把这种工作方式称为 **"loop engineering"**（Osmani 2026）——即用户命题里的"Loop 变强"。
- Harness 的两类角色：**guides**（行动前，规定 agent 被允许/被鼓励做什么）+ **sensors**（行动后，暴露反馈让 agent 检测错误并纠偏）。设计 Harness = 设计好这两者。
- 全文主问题原文：*how can long-term deployed agents use harness-mediated control to accumulate experience, self-evolve, and eventually support meta-evolution?*

---

## 论文骨架（5 大层 + 元进化）

### 1. Harness：经验基础设施（三代进化）
- **Gen 1 — Task Loops**（WebGPT / ReAct / Reflexion）：建立了接口接地(interface grounding)，但状态多是 episodic，任务结束即丢失，无法复用。
- **Gen 2 — Cross-Task Reuse**（Voyager / AutoGen / MetaGPT / LangGraph / SWE-agent）：引入持久化状态、可复用技能库、多智能体协调与工作流结构。
- **Gen 3 — Runtime Systems**（Claude Code / Codex / Cursor 3 / OpenClaw / Hermes Agent）：**Harness 本身成为工程对象**，支持部署时自适应与经验积累。

> 核心洞见：**"一个正在运行的循环"和"一个会学习的循环"不是一回事**。单任务内 agent loop（行动→观察→决策→重复）只需成功一次；而**学习**需要在多个窗口之间累积、跨会话的经验。

### 2. Skills：经验如何变成可复用程序
技能 = 程序性经验的外部化。采用社区收敛的 **SKILL.md 模式（MIRA）**：
- **M** = Manifest（元数据，用于运行时发现）
- **I** = Instruction（任务执行指令）
- **R** = Reference（参考文档、Schema、示例）
- **A** = Artifact（可执行脚本、模板、辅助代码）

技能生命周期：**创建 → 使用 → 进化**
- 创建：专家编写(Anthropic/ClawHub) / 仓库化挖掘(Repo-Based) / 文档蒸馏(Corpus2Skill) / 离线轨迹(Trace2Skill, SkillX)
- 使用三问：**Routing**（大库中检索正确技能，强制加载精选技能优于大规模混合池检索）/ **Composition**（组合成任务流：SkillNet 全局关系图、GoSkills 局部图）/ **Execution**（Harness 约束下运行；SkillSmith 把技能编译为边界引导的运行时接口，减 token、降延迟）
- 进化：**Add**(Trace2Skill) / **Edit**(SkillForge) / **Prune/Retire**(AutoRefine)
- **Validation**：SkillsBench 显示自生成技能可能**负迁移(negative transfer)**，端到端验证必不可少。

### 3. Memory：经验如何变成持久状态
> **Skill vs Memory 的判据（原文）**：一段轨迹既可产出技能也可产出记忆——**若它变成一个"命名的过程(named procedure)"→ 归 skill 层；若它作为"情境证据(situated evidence)"仍有价值 → 归 memory 层。** 技能记"如何再做一次"；记忆记"发生了什么、什么变了、什么失败了、用户偏好什么"。
> Memory ≠ RAG：RAG 从相对静态语料检索；agent memory 是**由 agent 自己生产、编辑、复用**的（xMemory：self-authored, revisable, action-coupled）。本质是"上下文中介的持久化系统(context-mediated persistence)"。
> 原文验证要点：SkillClaw 的验证**在夜间空闲用户环境、用完整工具链**跑——对应我们数据工程师"夜里持续取数"场景，论文把这类"闲时验证"当作正解。skill 进化必须过验证门（SkillEvolver 的 fresh-session auditor 防"授权上下文泄漏"、CoEvoSkills 的 Surrogate Verifier 补稀疏 ground-truth 信号）。

三层架构：
- **表示层(Representation)**：内容单元（原始日志 / 情节轨迹 / 摘要 / 语义抽象）；组织结构（扁平追加式 / 分层式 MemGPT / 关系图式 PlugMem, GAAMA, SAGE）
- **操作层(Operations)**：Write/Admission（自适应准入控制）、Compression（压到语义事实）、Consolidation（跨会话合并抽象 A-MEM）、Retrieval/Activation（相似度→图遍历）、Update/Revision（处理过时与冲突 Live-Evo, MemoRepair）
- **进化层(Evolution)**：Content（记忆内容精炼）/ Mechanism（组织与提取方式改进 MetaMem, MemSkill）/ Policy（何时写/读/遗忘的控制策略学习 AgeMem, MemRL, DeltaMem）

### 4. Environment：智能体经验的天花板
> 环境不仅是外部世界，更是**自改进的上限变量**。动作面狭窄、反馈稀疏、任务 horizon 短 → 再复杂的 Harness 改进也难产生实质收益。

三个分析维度：
- **Action Diversity**：CLI → Browser → Desktop → App Ecosystem
- **Feedback Density**：可归因性与密度（执行错误、状态测试、验证器输出）
- **Task Horizon**：时序深度与状态可恢复性

演进链：ALFWorld → Voyager / WebArena / OSWorld / AppWorld → Terminal-Bench / SWE-Gym。
**关键三层递进**：可执行环境（让软件可运行）→ 标准化边界（让经验可携带）→ 可学习环境（让经验可优化）。

### 5. RL 与持续学习：经验如何固化到参数
当 Harness 侧经验稳定且通用时，走**参数路径**——把经验内部化为模型权重。三大动机：
- **Internalize priors**：避免重复支付上下文编排开销（Composer 2, Cursor RL）
- **Transfer beyond context**：策略跨任务/会话/用户迁移（SEAgent, UI-Mem）
- **Collective evolution**：联邦式更新，让单体经验成为共享先验

工业界最强证据：**Composer 2 / Cursor RL**（在真实 Cursor 会话中跑 RL，用与部署相同的工具和 Harness）、**Codex**（真实编码任务上 RL）、**OpenClaw-RL/MetaClaw**（把实时用户回复、工具输出、终端状态作为在线 RL 监督）。
> 瓶颈不在策略梯度算法，而在**"痕迹到更新"的系统**：轨迹选择、大规模收集、信号提取、同 Harness 训练。

### 6. 元进化(Meta-Evolution)：谁来控制进化？
沿两个轴（**谁控制进化** × **什么在进化**）分三种 regime（原文精确定义）：

1. **TaskAgent self-evolution**：TaskAgent 把部署后经验转成自己的**持久内容资产**（技能内容、记忆资产、用户偏好/经验摘要、结构化领域知识、子 agent 产物）。进化对象 = "TaskAgent 知道什么"。控制回路在 TaskAgent 内。（SkillClaw, A-MEM, AgentFactory）
2. **TaskAgent meta-learning**：不是攒资产，而是**改执行机制或改进策略**（"learning how to learn"）。进化对象从"知道什么"变成"如何行动/如何改进"——两种形态：改执行机制（记忆检索、技能使用、上下文构建、workflow/routing、策略参数）；改改进策略（如何抽象失败、如何触发/验证更新、如何保留有用改动）。控制回路**仍在** TaskAgent 或其 task-facing harness 内。（MetaAgent, ARISE, ExpWeaver）
3. **Meta-evolving agent**：两点区别——① 进化过程由一个**功能独立的 meta-layer** 作为专门的持续优化任务来组织（该层不负责当前用户任务）；② **meta-layer 自身也可进化**（其状态、规则、评估协议、选择策略、控制机制）。（Hyperagents 把 task_agent.py 与 meta_agent.py 放同一可编辑空间；Agent0；Group-Evolving Agents）

> 现状：论文指出目前多是**局部 meta-evolution**，离"通用 meta-evolution"理想仍远。

Meta-layer 实现范式：
- **局部控制策略**：MetaMem（元记忆规则）、MemSkill（记忆技能库）、CluE（提示进化循环）
- **能力资源生命周期**：Mem2Evolve（工具/子代理 创建-验证-蒸馏）、MetaClaw（快技能进化 + 慢策略固化）、Continual Harness（CRUD 编辑系统提示/子智能体/技能-记忆）
- **持久组织与认知状态**：HERA（编排器+经验库）、AutoAgent（执行循环+进化循环）
- 自我改进极限：**Hyperagents**（把 task_agent.py 和 meta_agent.py 放同一可编辑程序空间，支持自我引用式元进化）、Group-Evolving Agents（群体级改进状态本身演化）

### 7. Part IV — 度量、安全、未解难题（公众号未覆盖，原文补强）

**7.1 度量自改进（第8章）**：现有 benchmark 大多测"单次任务分数",测不出**纵向真实增益**。评估必须区分：真实纵向增益 vs 过拟合 / 成本转移(cost transfer) / 隐藏回归(hidden regression)。论文提出 **SIP-Bench** 作为协议层——不是又一个数据集，而是规定"怎么测自改进"的协议（含改进是否可归因于某个原因）。

**7.2 安全：自改进是一个"移动的攻击面"（第9章）**：对齐通常在发布前固定，但自改进 agent 发布后仍在变（装技能、写记忆、扩工具、更新参数，全在认证配置之外）。**每一个使适应成为可能的面，也都能侵蚀控制**——渐进漂移或蓄意操纵。**meta 层是最尖锐的情况**：一个能重写自己更新规则/评估器的过程，可以让它本该维持的保证失效。"认证一次固定模型"不等于"对一连串部署后改动的安全性质"。

**7.3 未解难题（第10章，10个）**：其中与我们最相关的：
- **10.7 多 Agent 协同与涌现**：⚠️ 关键警示——**多 Agent 系统当下几乎都是"预先接线"的**（设计者固定角色、通信通道、协议，新增 agent 执行预定子任务）。"组织结构能否像单 agent 那样通过适应习得"几乎无人研究。且**报告出来的多 agent 收益常常只是加了算力**，真正"组织带来的、任何单体都没有的能力"的证据很薄。→ **学习协同结构 + 分离出真正的集体能力，都还是未解问题。**
- **10.9 部署后修改下的安全**：改进与控制方向相反，二者在持续进化系统里的权衡尚无处理方法。
- **共同底座 = Verification（验证）**：内部化经验的判据、纵向评估的归因、安全自改的自我能力估计——**都以验证为前提，而验证恰恰是随 agent 变强而"最不易 scale"的能力**。原文结论金句：*"进步更可能来自'如何让经验变成能力'的理论，而非单个更强的 agent。"*

---

## 对我们项目(Akivili)的映射（初步，待一起探讨）

> 用户命题：通过各种任务让 Agent 学习并成长 → 能做更多任务 + 协同效率更高 → **Loop 变强**。这与论文主线完全同频。下面是"我们已有的"和"论文提示可补的"对照，作为规划讨论的输入，**不做任何变更**。

### 我们当前所处的位置
- **Harness**：Akivili 的 `collab.py`(run_queue/并发池/@mention 派发) + `runner`(CLI 后端 Claude/Codex) + 任务看板/详情页 ≈ 论文的 **Gen 2 → Gen 3 之间**的 Runtime System 雏形。
- **Skills**：已有目录型 Skill(`<slug>/SKILL.md` + scripts/references) + downloadable 管控 ≈ 论文的 **MIRA 模式**（我们已天然对齐 M/I/R/A）。
- **Memory**：已有 per-agent 记忆文件 + `akivili:managed:knowhow` 段 + 任务完成触发 `reflect_on_task_done` 沉淀 ≈ 论文 Memory 层的 **表示层(内容单元) + Write/Consolidation 操作**。
- **Environment**：数据工程师跑 Narya 取数、真实入库校验 ≈ 论文强调的**可执行 + 有密集反馈的环境**（这是我们相对稀缺且珍贵的资产）。

### 论文提示我们可以做的（讨论清单，未决策）
1. **技能进化闭环**：目前技能多为"创建+使用"，缺 **Edit/Prune/Validation**。可补：从任务轨迹自动抽取候选技能(Trace2Skill)、技能负迁移检测(SkillsBench 思路)。
2. **记忆的操作层与策略层**：目前 knowhow 是"追加式"，缺 **Consolidation(跨任务合并去重)** 和 **Policy(何时写/读/遗忘)**。多样性/静默损坏探针(数据工程师那条经验)其实已是"轻量验证器"雏形。
3. **Routing 提效**：随技能/记忆增长，"检索正确技能"会成瓶颈——论文指出**强制加载精选技能 > 大池检索**，对应我们"按需激活 Skill"策略要有精选层。
4. **协同即"群体进化"**：@mention 多 Agent 协同 ≈ 论文 Collective/Group-Evolving，可探索"共享经验池"让一个 Agent 的 knowhow 成为团队先验。
5. **Meta-layer 雏形**：Akivili 的 Leader/剑剑派活 + 我(开发)修 bug + 沉淀记忆，其实已有"元层"影子。论文的 **Continual Harness(CRUD 编辑提示/技能/记忆)** 是可借鉴的元层形态。
6. **可学习环境分层**：把"可执行→可携带→可优化"作为环境成熟度标尺，评估每个 Agent 的任务环境处于哪层。

---

## 待办
- [ ] 手动下载 OpenReview PDF 放本目录（脚本被 Cloudflare 拦）。
- [ ] 一起确认上面"讨论清单"哪些进规划（本轮只探讨，不改项目）。
