# Akivili（阿基维利）— 本地优先的多 Agent 工作平台

> **愿此行，终抵群星！** 构建属于你自己的星穹列车，从这里出发，亲手去开拓每一个目标！
>
> 把你日常工作的 Agent 组装成可视化工作流。既能在终端、也能在网页发号施令；Agent 之间还能协同完成任务。每个项目自起标题、绑定一个本地文件夹，Agent 在该文件夹的边界内干活。

## 项目定位

Akivili 是一个**本地优先（local-first）**的多 Agent 编排平台：

- **CLI 执行引擎**：后端调用本地 `claude -p` / `codex exec`（或 API）跑 Agent，能读写文件、执行命令、操作你指向的本地项目文件夹——能力天然对齐真实开发工作流。
- **可导入的数字人才库**：从 Agent 人格库目录（默认项目内 `agents/`，可用环境变量 `AKIVILI_AGENT_LIBRARY_DIR` 指向外部库）挑选导入，平台内也能新建/改造自己的 Agent。
- **项目即工作区**：每个项目自起标题、绑定一个本地文件夹，作为 Agent 的工作边界。
- **看板式任务体系**：Trello 式看板 + 细粒度状态 / 优先级 / 子任务 / 活动时间线；任务详情两栏布局。
- **多 Agent 协同**：Team Leader 统筹调度——读任务、按成员技能 @mention/建子任务委派、成员执行回报、汇总收尾；事件驱动并发池调度（多成员可同时开工、跨供应商混编）+ 父子任务闭环（子任务不全完成父任务不收尾）+ 卡死超时兜底 + 多层防死循环（含防层层派生）。
- **多模型配置化**：Claude Code / Codex / API（Anthropic / OpenAI 格式）等多供应商，在设置页配置切换。
- **内网协作**：可开放给内网同事访问，管理员可写可执行、其他人只读浏览。

## 能力概览

已实现（P0–P6）：
- 主页 Dashboard + 项目空间：项目卡片总览、状态、成员数
- 数字人才库：312 个中文 Agent，浏览 / 搜索 / 导入 / 新建 / 改造人格
- 项目与团队：项目绑定本地文件夹；团队组建、Team Leader、成员配模型 + Skills
- Agent 记忆：每个 Agent 跨项目共用持久记忆，自动写入工作区约束与 Skills 说明；分「近期动态」滚动段落与「Know-how」经验段落
- Skills 库：能力指令文本库，可导入 / 新建 / 下载，Agent 按需启用
- 工作区看板：Trello 式任务看板，细粒度状态 + 优先级 + 子任务（父卡片下嵌套小卡片）+ 活动时间线（两栏详情）；「验证中」独立成列
- Agent 真实执行：@ 分派，CLI 在项目目录真实改文件跑命令 / API 对话，流式输出，可 Kill / 查日志；会话正文只留真实交付（CLI 交付经 jian comment / 过程碎语归日志，API 回复即产出）
- 执行日志与历史：右侧执行日志区进行中运行常显、历史运行折叠可展开，每行为命令缩略 + 状态图标（hover 看 Agent）+ 相对时间；点「日志详情」进弹窗，逐条还原命令的完整参数与运行结果（timeline + 过滤 + 排序 + 复制 + 执行时间 + 供应商·模型），执行中实时展开看命令，敏感信息自动脱敏
- 富文本渲染：任务描述与消息按 Markdown 渲染（标题/粗体/列表/表格/代码块/图片/可点击链接），DOMPurify 消毒防 XSS
- 多 Agent 协同：Team Leader 统筹调度、@mention/子任务委派、成员执行回报、并发池调度（多成员并行、跨供应商）、父子任务闭环收尾（全子完成自动唤醒负责人汇总汇报）、卡死超时兜底（按角色可配）、多层防死循环
- 人工验收闭环：Agent（含 Leader）无法自行标「完成」，顶层任务 `done` 降级为「验证中」；子任务执行成功自动完成（无验证中概念）、父任务全子完成自动进验证中并唤醒负责人汇总，最终由人拖入「已完成」验收
- 任务完成经验反思：人工验收（拖入「已完成」）时，参与角色各自用自身模型复盘、沉淀可迁移 Know-how 到记忆，越做越会做
- 大模型 / CLI 多供应商配置（设置页）
- 内网访问 + 登录鉴权：管理员可写可执行，匿名只读；记忆数据仅管理员可见；用户可按用户名配同名头像

规划中：可视化工作流编排（P5）、终端 CLI 入口、OpenSpec 看板（P7）。

## 技术栈

| 层级 | 技术选型 |
|------|----------|
| 前端 | Vue 3 + Vite + Element Plus + Vue Router |
| 后端 | Python 3.12 + FastAPI + Uvicorn |
| 存储 | SQLite（项目 / Agent / 会话 / 工作流元数据）+ 本地文件系统（项目工作区） |
| 执行引擎 | Claude Code CLI（`claude -p`）/ Codex CLI（`codex exec`）/ 纯 LLM API |
| LLM | OpenAI Chat Completions / Anthropic Messages（双格式兼容） |
| 规格管理 | OpenSpec（specs 已实现能力 / changes 待实现提案） |

## 执行引擎设计

平台核心是一个统一的执行器抽象 `ExecutorBackend`，三种实现：

```
ExecutorBackend.run(agent, prompt, project_dir, on_event) → 流式事件(SSE)
├── ClaudeCodeBackend   claude -p --output-format stream-json --add-dir <项目文件夹>
│                       --append-system-prompt-file <agent.md> --model <模型>
├── CodexBackend        codex exec（codex-cli）
└── ApiLlmBackend       httpx 流式，OpenAI/Anthropic 双格式
```

**安全约定**：

- 子进程一律**列表传参**、绝不 `shell=True`，杜绝参数/命令注入
- 默认用 `--add-dir` 把 Agent 工作目录**限定到当前项目文件夹**，不默认开启 `--dangerously-skip-permissions`
- 阻塞型子进程调用用 `asyncio.to_thread` 卸载，避免冻结事件循环
- SSE 生成器检测客户端断开，及时终止子进程，避免空转烧 token

## 多 Agent 协同设计

协同引擎 `backend/collab.py`——把**调度决策外包给 LLM**（给 Leader 注入协作协议 + 团队花名册），用 **@mention 作为 Agent 间唯一通信原语**，配合**事件驱动队列 + 并发池**推进。

```
任务唤醒 Leader
   └─(注入 协作协议 TEAM_LEADER_PROTOCOL + 团队花名册 build_roster)
      └─ Leader 发言：@后端开发者 @测试专员 …（LLM 自主决策派给谁）
          └─ parse_and_enqueue_mentions：解析 @成员 → 各入队一个 run（run_queue）
              └─ 并发池 _loop：填满空闲槽（MAX_CONCURRENCY 个成员同时开工）
                  └─ 每个 run：runner.execute_dispatch 跑到底 → 落库消息/活动
                      └─ 成员发言里的 @ → 继续入队（事件驱动，直到无人再被 @）
```

**并发池调度**（取代早期「串行单并发」）：

- 后台循环 `_loop` 一次性把空闲槽填满，最多 `MAX_CONCURRENCY`（默认 3）个 Agent **同时执行**——一个任务被 @ 到的多名成员可并行开工，慢成员不阻塞快成员。
- `_claim_one` 原子领取 queued run 并标 `running`；`_process_one` 执行完落库终态并释放并发槽；`_tick` 为确定性单步原语（测试用），与并发池共用同一套 claim/process。

**卡死兜底**（保障 Agent「持续工作不卡壳」）：

- 单个 Agent 执行设超时，**按角色可配**：默认 30 分钟，数据类等长耗时角色（如经外部取数服务拉数）放宽到 60 分钟。超时即 `runner.kill_run` 杀**整棵进程树**（Windows `taskkill /F /T`，非仅父进程，否则子进程成孤儿继续跑）+ `runner.finalize_run` 主动把 `task_runs` 落成终态（生成器被取消时收尾代码跑不到，需外部补落库，杜绝孤儿 `running`）+ 记 `task_failed` 活动（以角色昵称呈现）+ 释放并发槽。卡死成员只占一个槽、到点清理，**绝不拖垮队列或留僵尸**。
- CLI 执行供应商默认全权限放开（跳过授权/沙箱确认、`stdin` 关交互），从源头避免 Agent 因交互提示挂起。

**多层防死循环**：

- 协议教育：Leader 收尾总结不 @ 任何人；能自己答的不硬委派。
- Leader 自触发守卫：Leader 刚以 Leader 身份发言，不因自身发言再唤醒自己。
- pending 去重：同一 `(task, agent)` 已有 queued/running 的不重复入队。
- 深度上限：单任务累计运行数达 `MAX_RUNS_PER_TASK`（默认 20）即停止入队并记活动，防失控烧钱。
- **防层层派生（cascade）**：只有**顶层任务**（`parent_task_id` 为空）才注入"负责人派活"指令，子任务一律叶子工作；**子任务内 @负责人不唤醒**（否则成员在子任务里 @Leader → Leader 又在子任务里派活 → 无限派生）；带完整指令的收尾 run 不再追加派活指令。

**父子任务闭环**（`backend/progress.py`）：

- **子任务不结束，父任务不能结束**：父任务若有未完成子任务，`jian status done`（成员）被拦截、`PUT /status`（管理员）返回 409（管理员可 `force:true` 覆盖）。
- **全部完成→负责人汇总收尾**：最后一个子任务 `done` 时，`maybe_advance_parent` 把父任务置 `reviewing`，并**自动入队一条负责人总结 run**（带汇总指令：查看各子任务成果 → `jian comment` 统一汇报 → `jian status done`）。幂等，`reviewing` 后不重复触发。
- `task_progress` 聚合父 + 全部子任务的 run_queue（哪些成员在跑/排队、`sub_done/sub_total`），供任务详情右侧「执行日志」区实时展示"还有谁在干活"。

**CLI 执行隔离**（保障协同 Agent 只走平台、不被宿主环境污染）：

- **prompt 走 stdin**：`claude -p` 的 prompt 经 stdin 传入，不作命令行参数——避免 Windows 超长 argv 被截断（截断会让 Claude 收到残缺 prompt、输出降级成非 JSON、解析不到事件）。
- **禁用内置编排工具**：`--disallowed-tools Task,Workflow,SendMessage,TaskCreate,…,TodoWrite`，逼 Agent 用平台的 `jian` CLI 真实建卡/派活，而非用 Claude 自带的 Task/Workflow 工具"模拟"协同（那样成员不会被平台真正唤醒）。
- **隔离宿主全局定制**：子进程 `CLAUDE_CONFIG_DIR` 指向空目录，不加载宿主 `~/.claude/CLAUDE.md`、hooks、skills（避免其它全局人格污染 Akivili Agent）；认证走 `~/.claude.json` 不受影响。（注：不能用 `--safe-mode`，它会把 stream-json 输出降级成纯文本。）

> 复用现有 `project_agents.is_leader`（Team Leader）与 `agent_skills`（成员技能）——**项目团队即 squad**，不另建表。成员可跨供应商（Claude CLI / Codex CLI / API 混编），同项目成员经花名册默认互相认识、都能被协同唤醒。

> ⚠️ **已知**：协同调度依赖 Leader（LLM）对注入的协作协议 + 花名册的遵循程度，存在 run-to-run 波动（偶尔会探索工作区后自由发挥、不建子任务）。人格/协议提示仍在打磨中。

## 任务完成经验反思

`backend/reflect.py`——让 Agent「越做越会做」。任务进入 `done` 时，对真正参与执行的角色各自触发一次复盘，把经验沉淀进记忆。**与 per-run 归档区分**：per-run 只记流水（最近做过啥），反思才是提炼可迁移的 Know-how。

```
任务 done（拖入已完成 / jian status done）
   └─ reflect_on_task_done（后台异步，不阻塞）
       ├─ _participants：以 task_runs 为准，收集本任务+子任务里真正跑过的角色
       └─ 每个角色并发复盘（runner.run_oneshot：借其模型+人格想一段，不建 run/不落会话）
           └─ 产出 3-5 条 Know-how（聚焦方法/坑/诀窍，非复述结论）
               └─ 写入记忆受管段落 knowhow：去重合并；超上限再调模型压缩成 Top-N
```

- **只沉淀真参与者**：以 `task_runs` 为准（有 run = 真干过、有产出），未参与的角色不写。
- **产出是要领不是存档**：prompt 明确"提炼方法/坑/诀窍/判断依据，不要复述结论数字"；无可沉淀时不写。
- **治理**：Know-how 存独立受管段落，去重合并；超 `KNOWHOW_MAX`（默认 30）条时压缩成 Top-N，不撑爆人格上下文。per-run 的「近期动态」段落也滚动保留最新 N 条。
- **测试项目跳过**，不污染真实身份记忆。

## 目录结构

```
JianAgency/
├── README.md                  # 版本记录 / 技术方案 / 功能列表（唯一事实源）
├── start.ps1                  # 一键启动
├── memory/                    # Agent 记忆：每个 <agent-slug>.md 是一个 Agent 的跨项目持久记忆
├── skills/                    # Skill 库：每个 <skill-slug>.md 是一段能力指令文本
├── icon/                      # 人才头像图库
├── openspec/                  # 规格驱动：specs（已实现）/ changes（待实现）
├── TestReport/                # QA 套件 + 关键协同案例 + 报告
├── backend/                   # FastAPI（Python 3.12）
│   ├── main.py config.py auth.py database.py
│   ├── collab.py              # ★ 多 Agent 协同引擎（队列/并发池/@解析/协议花名册）
│   ├── progress.py            # ★ 父子任务进度聚合 + 闭环联动
│   ├── reflect.py             # ★ 任务完成经验反思（参与者复盘 → 沉淀 Know-how）
│   ├── activity.py            # 活动时间线（含北京时间转换）
│   ├── timeutil.py            # UTC→北京时间
│   ├── agent_memory_sync.py   # Agent 记忆受管段落同步
│   ├── executor/              # ★ 执行器抽象：base / claude_code / codex / api_llm / runner
│   ├── cli/                   # jian CLI（Agent 在平台上真实建卡/发言/派活的入口）
│   ├── agents.py projects.py memory.py skills.py
│   └── routes/                # agents projects project_agents tasks runs agent_cli agent_config skills memory settings auth icons fs
└── frontend/src/              # Vue 3
    ├── views/                 # Dashboard / Agents / Skills / ProjectDetail / Workspace / TaskDetail / Settings
    └── components/            # AgentAvatar / AgentProfileDialog / DirectoryPicker …
```

## 开发路线图

每个阶段对应一个 OpenSpec change 提案。

| 阶段 | 目标 | 状态 |
|------|------|------|
| **P0** | 脚手架：README、OpenSpec 骨架、目录结构、start.ps1、config、gitignore | ✅ 已完成 |
| **P1** | 地基：后端 config + 设置页多供应商 Tab + DB 初始化 | ✅ 已完成 |
| **P2** | 数字人才库：从 Agent 库目录 导入 / 注册 + 库浏览 UI | ✅ 已完成 |
| **P3** | 项目：CRUD + 本地文件夹绑定 + 主页 Dashboard | ✅ 已完成 |
| **P3.5** | Agent 配置：接入模型 + Skills 库 + 按 Agent 身份跨项目共享 | ✅ 已完成 |
| **P4** | Agent 对话：CLI 执行器跑通，单 Agent 任务安排 + SSE 流式 | ✅ 已完成 |
| **P5** | 可视化工作流：配置编排 + 节点/状态可视化 + 运行 | ⏳ 计划中 |
| **P6** | 多 Agent 协同：Team Leader 调度 + @mention/子任务委派 + 并发池执行 + 父子闭环 + 卡死兜底 | ✅ 已完成（Leader 提示遵循性打磨中） |
| **P7** | OpenSpec 看板 + 整体打磨 | ⏳ 计划中 |

## 功能列表

### 已实现
- 项目脚手架与规格驱动开发基线（OpenSpec）
- 大模型 / CLI 多供应商配置（设置页）：API（Deepseek/OpenAI/Anthropic/通义/智谱/Moonshot/Ollama）+ CLI（Claude Code / Codex），含密钥脱敏、连通性测试、默认供应商
- 后端地基：FastAPI 入口 + SQLite 建库 + CORS 白名单
- Agent 模版库：扫描 Agent 库目录（268 个中文 Agent）入库，按分类/关键词浏览、搜索、查看人格详情、幂等重扫
- 项目管理：项目 CRUD + 绑定本地文件夹（工作边界）+ 主页 Dashboard 卡片总览；项目内 Agent 团队（从库导入 / 自建 / 改造人格 / 移除）
- Agent 记忆：每个 Agent 在 `memory/<slug>.md` 拥有跨项目共用的持久记忆，可在项目内查看/编辑；加入项目 / 配置 Skills 时自动写入🗂️工作区路径约束与🧩Skills 使用说明（受管段落，不影响手写内容）
- Agent 配置：每个 Agent 可接入大模型（从供应商选）+ 启用 Skills；模型/记忆/Skills 按 Agent 身份（slug）跨项目共享，人格各项目独立
- Skills 库：能力指令文本库（`skills/<slug>.md`），可导入/新建，浏览搜索；Agent 按需勾选启用
- 工作区看板：项目下 Trello 式任务看板，5 态流转（待办→进行中→验证中→已完成，可归档）；「验证中」一等独立列；子任务在父卡片下以嵌套小卡片展示（状态/优先级/负责人头像/标题，点击进详情）
- Agent 真实执行：任务内对话终端 @ 分派负责人，Agent 按接入模型真实执行（CLI 在项目目录改文件跑命令 / API 对话），SSE 流式输出，执行前读记忆+会话历史、执行后写回记忆，可 Kill、可查日志
- 执行日志详情：每次 run 的全过程结构化留存（文本/思考/工具调用及其完整命令与输出）；执行日志区每次运行提供「日志详情」弹窗——彩色 timeline 进度条 + 元数据（模型/时长/工具数）+ 工具类型过滤 + 排序 + 复制全部，逐条事件可展开看完整 input/output；SSE 流也带命令详情，执行中工具行可实时展开；密钥/token/私钥/连接串服务端脱敏、前端兜底
- 多 Agent 协同：Team Leader 统筹——读任务、按成员技能 @mention/建子任务委派、成员执行后回报、Leader 汇总收尾；事件驱动 + 并发池调度（多成员并行、跨 Claude/Codex/API 供应商混编）、父子任务闭环、卡死超时兜底、多层防死循环（含防层层派生）、CLI 执行隔离（stdin 传 prompt / 禁内置编排工具 / 隔离宿主全局定制）
- 人工验收闭环：Agent（含 Leader）`jian status done` 一律降级为「验证中」，无法自行完成任务；子任务执行成功自动置完成、父任务全子完成自动进「验证中」、独立任务完成直接进「验证中」；经验反思与「已解决数」计数只在管理员手动拖入「已完成」验收时触发（对父任务连同子任务一起反思）
- 子任务：带描述 + 优先级，强制两级（子任务下不能再建子任务）
- 时间与身份细节：全平台北京时间展示；活动时间线显示真实操作者（登录用户名 / Agent 昵称，兼容历史 slug 记录）；测试项目数据不写入 Agent 记忆

### 规划中
- [ ] 可视化工作流编排与运行
- [ ] 终端 CLI 入口
- [ ] OpenSpec 看板

## 快速开始

```powershell
# 一键启动（安装依赖 + 起前后端）
./start.ps1
# 前端 http://localhost:3100   后端 http://localhost:8100
```

> 端口选用 3100 / 8100，便于与本机其它常用开发端口（如 3000 / 8000）错开。

## 内网访问

服务已绑定到 `0.0.0.0`，同一内网的同事可访问：

- **访问地址：`http://<your-lan-ip>:3100`**（本机内网 IP，随网络环境变化）；通过环境变量 `AKIVILI_EXTRA_ORIGINS=http://<your-lan-ip>:3100` 加入 CORS 白名单
- 管理员账号：由环境变量 `AKIVILI_ADMIN_USER` / `AKIVILI_ADMIN_PASSWORD` 在首次启动时设置（缺省占位 `admin` / `changeme`，**部署务必改掉**）
- 匿名/其他同事：只读浏览项目空间、数字人才库、Skills；看不到设置，不能安排任务或改动

> ⚠️ **安全提醒**：管理员触发 Agent 执行时为放开权限模式，Agent 能在本机改文件、跑命令。请仅在可信内网开放，妥善设置并保管管理员密码。如需真正的域名，请让内网 DNS 把名称解析到本机 IP。

## 版本记录

### v0.16.11 — 2026-07-08
- 🔎 **反思沉淀失败不再沉默：三类结果全留痕**（能力 `agent-reflection`）
  - **问题（火花 task78 存量个案查出）**：`reflect_on_task_done` 用 `asyncio.gather(return_exceptions=True)` 并发让各成员复盘，但结果只挑 `>0` 的报「已沉淀」，**抛异常的成员被静默吞掉**。火花在 task78 有 16 个 succeeded run、5900+ 字高质量前端产出，却因首次验收时并发调 CLI 偶发失败（`run_oneshot` 超时/冷启动），反思异常被吞——干了活没沉淀且无人知晓，活动流只列了成功的 7 人。
  - **修**：`gather` 结果分三类留痕——成功(`>0`)列入汇总「已沉淀」；无增量(`==0`，反思按门槛回「无」)计「N 人无新增经验」不报错；**失败(抛异常)逐个记系统活动**「⚠️ X 的经验沉淀失败（错误类型）——可重跑本任务反思补上（slug=…）」，并在汇总行计失败数。让「有人没被处理」无所遁形、可人工重跑补上。
  - **存量清理**：火花 task78 的漏沉淀已补齐（5 条前端领域经验：展示位工时口径、高频渲染本地过滤、房间级状态广播补偿、多展示位工程收敛、native/web 承载形态前置）。全项目复查确认无其它漏网——task70「团队 Kickoff」的自我介绍类产出反思正确返回「无」（无技术增量），非漏沉淀。
  - 验证：新增 `TestReport/run_reflect_observability_probe.py` **5/5**（成功/无增量/失败三类留痕）；回归 `run_reflect_probe` 8/8、`run_reflect_participants_probe` 4/4。

### v0.16.10 — 2026-07-08
- 🧠 **修复「直接建卡型产出不被反思沉淀」的结构性盲区——干了活就得有价值沉淀**（能力 `agent-reflection`/`agent-memory`）
  - **问题**：`reflect._participants`（决定任务 done 时哪些成员被反思）只从 `task_runs` 圈定参与者（注释写死「有 run = 真的执行过」）。但「直接建子任务卡片」（`jian subtask --body-file`，成员把分析结论直接写成卡片正文）这类产出**不产生 task_run**——只落一个 `tasks` 行 + 一条本人 `messages` 发言。于是这类成员进不了参与者名单、**从不被反思、其分析成果无法转化成可复用 knowhow**。检查项目26 时发现火花（前端）接了最多任务、大量走建卡型产出，却几乎零沉淀，正是此盲区。
  - **修**：`_participants` 口径从「有 task_runs 的成员」扩展为「有 run 的成员 **∪** 在本任务/子任务会话里有 `author_slug=本人` assistant 发言的成员」。与下游 `_task_context`（本就按 `messages` 取产出）口径一致，无产出者在 `_reflect_one` 里 context 为空自动跳过（返回 0），不会误纳空壳成员。建卡型与执行型一视同仁走同一套反思，反思 prompt 既有的「宁缺毋滥/无专业增量回无」门槛负责过滤低质。
  - 验证：新增 `TestReport/run_reflect_participants_probe.py` **4/4**（两类成员都进参与者名单、都沉淀 knowhow）；回归 `run_reflect_probe` 8/8（真没产出的成员仍不写，口径扩展无误伤）。

### v0.16.9 — 2026-07-08
- 🐞 **修复「收尾漏交付」检测因 aiosqlite 误用而长期静默失效**（能力 `agent-execution`）
  - **现象**：`run_stdout_display_probe` 场景 A（CLI 无 jian 交付时应打「未通过 jian 提交」标记）一项 FAIL。
  - **根因**：`_has_trailing_stdout_after_deliverable` 里一处查询写成 `last_act = await (await db.execute(...))` —— `await db.execute()` 已返回 Cursor，外层再 `await` 抛 `TypeError: object Cursor can't be used in 'await' expression`（少了 `.fetchone()`，且下一行的 `await last_act.fetchone()` 永远到不了）。此异常被 v0.16.7 给收工动作加的 `try/except` 兜底**静默吞掉**。
  - **影响面（比测试更重要）**：该函数是 v0.16.7（task78 事故）引入的「收尾结论没落库」监督逻辑。由于每次 CLI 收工都在此抛异常被吞，**自 v0.16.7 起这条漏交付标记从未真正生效**——CLI Agent 若把最终结论只打在 stdout、没走 jian comment，平台本应告警却一直沉默。
  - **修**：改为与同文件其它三处一致的正确写法 `await (await db.execute(...)).fetchone()`。
  - 验证：`run_stdout_display_probe` 8/8 全绿（此前 7/8）；逐函数探测 `_persist_memory`/`_has_jian_deliverable`/`_has_trailing_stdout_after_deliverable` 均无异常。

### v0.16.8 — 2026-07-08
- ⚙️ **调度策略三项增强：并发度可配置 + 优先级排序 + 失败自动重试**（能力 `agent-collaboration`/`agent-execution`）
  - **并发度可配置**：`MAX_CONCURRENCY` 从写死 3 改为走 `Settings`（`config.json` + 环境变量 `AKIVILI_MAX_CONCURRENCY`），`start_loop` 时读取生效。多项目/多 Agent 规模化时可上调，不必改代码。同增 `AKIVILI_MAX_RETRY`（默认 2）。
  - **优先级排序**：`_claim_one` 领取顺序从纯 FIFO（`ORDER BY id`）改为 `任务优先级 high>medium>其它 降序 → 同优先级按入队 id 升序`。高优先级任务插队，同级仍先来先服务。此前 `tasks.priority` 字段存在但调度未用。
  - **失败自动重试 + 退避**：`run_queue` 增 `attempts`/`next_retry_at` 两列。区分两类失败——**瞬时可重试**（dispatch/驱动层异常、error 事件无产出，如 CLI 冷启动/限流/连接断）自动回 `queued` + 退避（30s→120s 阶梯）重试，达上限（默认 2 次）才终落 `failed`；**判定型失败**（超时无交付=真卡死、被 kill、状态分叉伪失败）**不重试**，避免烧墙钟/token。`_claim_one` 在退避窗口内不领取重试行。
  - **说明**：任务如何拆分、分派给谁仍是负责人 Agent 在 prompt 引导下自主决策（按业务域匹配成员）；本次改的是「分派之后平台如何调度执行」这一确定性层。
- 🧪 **测试脚本纳入版本控制（白名单模式）以保障平台稳定性**
  - `.gitignore` 对 `TestReport/` 从「整目录忽略」改为白名单：默认忽略所有内容（运行产物 `qa_results_*`/`collab_scenario_*`/周报/截图 `shots/` 等含真实内网地址、业务数据，绝不入公开仓），仅反选 `run_*.py`/`cleanup_test_data.py` 测试脚本入库。新增探针按 `run_*.py` 命名即自动纳入。
  - 顺带清除两处测试桩里的业务暗示文案（改中性），不影响断言。
  - 新增 `TestReport/run_scheduling_probe.py` **10/10**（并发/重试配置读取、优先级领取、FIFO、退避、异常型重试到上限、超时/error 分类）；回归 concurrency 7/7、subtask 6/6、memory-hygiene 11/11、QA 31/31。

### v0.16.7 — 2026-07-08
- 🔧 **run 状态分叉根治：`task_runs` 与 `run_queue` 不再各说各话**（能力 `agent-collaboration`/`agent-execution`）
  - **事故（task 82→78 卡死）**：子任务 82 明明已成功（`task_runs=succeeded`），但 `run_queue` 却是 `failed`，导致父任务 78 收不了尾、迟迟出不了负责人统筹汇报。根因是 `execute_dispatch` 在 `_finish_run` 落定 `task_runs` 终态**之后**的收工动作（写记忆 / 记活动 / CLI 漏交付标记）一旦抛异常，会经异步生成器冒泡到 `collab` 外层 `except`，把已成功的 run 误判成 `failed` 写进 `run_queue`——两个数据源就此分叉，`on_execution_complete` 只认 `run_queue`，任务永远推不动。
  - **修 A（runner）**：`_finish_run` 之后的整段收工动作用 `try/except` 兜底——**run 成败以 `task_runs` 落库为准**，善后失败只记日志、绝不冒泡改写 run 结果。
  - **修 A（collab 双保险）**：`_run_one` 收尾前对齐 `task_runs`——若外层判 `failed` 但该 run 的 `task_runs` 已是 `succeeded`，则纠回 `done`，堵住任何残余分叉路径。
  - **修 B（bug C·run_id 串号）**：`_process_one` 异常留痕原先把 `run_queue.id` 当作 `run_id` 写进 `run_logs`，但 `run_logs.run_id` 外键指向的是 `task_runs.id`——两者是各自独立的自增序列，同一数字在两表里指代不同 run，日志会误挂到同号的另一个 run 上（排查时「run 80 跑了 4 小时」的假象即此类串号）。且异常可能发生在 `task_run` 建立之前，根本没有可关联的 id。改为写进后端进程 stderr（带 `run_queue#N` 标识 + traceback），不再污染 `run_logs`。
  - **历史脏数据精准回填**：全库 73 条 `run_queue=failed / task_run=succeeded` 分叉（均为修复前残留、不卡任何任务推进，仅在工作区/日志面板误显「失败」），按 `task+slug+时间就近`一一配对，69 条确认对应 `succeeded` 的回填 `done`，4 条真失败/被杀（配到 `failed`/`killed`）按预期保留；改前备份 `jianagency.db`。经查历史 `run_logs` 0 条被串号污染，无需清理。
- 🔗 **子任务运行状态两处同源：详情页与工作区不再打架**（能力 `task-board`/`task-system`）
  - **问题**：任务详情页（实时）与外部工作区（看板嵌套小卡）对同一子任务显示的运行状态不一致。
  - **修**：工作区看板的子任务查询增补 `active_run`（该子任务在 `run_queue` 里 `queued/running` 的 run 数）；前端 `Workspace.vue` 新增 `subEff(s)`——`active_run>0` 时按「进行中」展示、否则用 `task.status`，与详情页 `subEffectiveStatus` 同一判据。轮询条件也纳入子任务 `active_run`，保证两处实时同步刷新。

### v0.16.6 — 2026-07-08
- 🩹 **修复孤儿回收误伤已成功任务**（能力 `agent-collaboration`/`agent-execution`）
  - **背景**：v0.16.x 的启动孤儿回收把所有 `running` 的 `task_runs` 一刀切落 `killed`。但「进程正常结束、只是 run 没落库」型孤儿也被误标，污染了**已成功任务**的真实终态。
  - **两处症状**：①工作区卡片 `run_status` 取最新 run = killed → 已完成任务显「■已终止」而非「✓执行完成」；②团队成员「已完成任务数」（`solved_tasks`，要求 `run=succeeded AND task=done`）少计，数据工程师显 3、实际 4。
  - **修**：(a) 回收改为**状态感知**——任务已 `done/reviewing` 的 running 孤儿落 `succeeded`、未收尾的才落 `killed`，且不给已完成任务记误导性「回收失败」活动；(b) 前端 `Workspace.vue` 新增 `effRunStatus`：任务 done/reviewing 时卡片一律显「执行完成」，不被单条 run 状态污染（双保险）。
  - 孤儿回收探针升级 13/13；QA 31/31、concurrency 7/7 全绿。

### v0.16.5 — 2026-07-08
- 💬 **对话框纵深放大 + 输入框 @mention 引入成员协作**（能力 `agent-collaboration`/`agent-execution`）
  - **对话区放大**：详情页 `max-width` 1280→1440；追加指令输入框 3 行→6 行（`min-height` 132px）、字号放大，多轮长对话更从容。
  - **输入框内 @mention**：移除原「@谁」下拉，改为在指令文本里输入 `@` 触发团队成员浮层补全（↑↓ 选择、Enter/Tab 选中、Esc 关闭，插入 `@昵称`）。候选来自当前项目团队。
  - **一条指令可 @ 多位成员协作**：发送时解析被 `@` 的成员——第一位作为流式主受理人即时执行，其余由后端 `dispatch` 复用 `parse_and_enqueue_mentions` 各入队一个 run，经协同后台循环串行执行（主受理人作 `author_slug` 传入，避免重复入队）。底部「将唤醒：@xx」实时提示。多轮会话可按需引入不同成员参与。
- 📊 **任务详情表格样式强化**（能力 `agent-execution`，`MarkdownView`）
  - 原细线表格视觉过弱、融进正文。重做：圆角外框+轻投影、深色渐变表头+加粗底线、斑马纹+行 hover、纯数字单元格自动右对齐（`tabular-nums`）、每个表格套可横向滚动容器（宽表在窄气泡不撑破）。数字识别与容器包裹在已消毒 HTML 上做 DOM 后处理，不影响 XSS 防护。

### v0.16.4 — 2026-07-08
- 🔧 **孤儿「执行中」回收补齐 task_runs 层**（能力 `agent-collaboration`）
  - v0.16.x 的 `reclaim_orphan_runs` 只回收了 `run_queue`，但任务详情页右侧「执行记录」列表读的是 `task_runs`（`/runs` 接口），只要有 `status='running'` 的行就显示「执行中」。多轮会话中每轮 `dispatch` 建一条 `task_runs`，生成器被取消 / 连接断开 / 进程重启时收尾路径跑不到 → `task_runs` 卡 `running` 成孤儿，即使任务已流转到「验证中」、外部卡片正常，详情页右侧仍持续显示「执行中」。
  - `reclaim_orphan_runs` 现启动时同时回收两层：`run_queue`→`failed`、`task_runs`→`killed`（被中断非正常失败，补 `ended_at`），并清理内存注册表残留。两层数据源不同必须一起清。
  - 验证：`run_orphan_reclaim_probe` 12/12（两层孤儿回收 + 非 running 不动 + 幂等）、QA 31/31、concurrency 7/7。

### v0.16.3 — 2026-07-08
- 📦 **目录型 Skill「仅集成、不下载」+ 直播营收知识库接入**（能力 `agent-skills`）
  - **`bilisc-kb-live-revenue` 接入 Skills 页**：直播营收系统知识库（大航海/PK连线/醒目留言/礼物四大业务域 + 支付/订单/交易/结算/风控/对账六大基础设施域），目录型能力包（`SKILL.md` + `scripts/revenue-kb-api` 查询 CLI）。Agent 可勾选启用、运行时注入其 `SKILL.md` 正文。
  - **「仅集成、不下载」契约**：Skill 的 frontmatter 标 `downloadable: false` 即视为「仅供 Agent 集成、不对外提供下载」。后端 `download_skill` 对此类 Skill 硬拦截返回 403（防绕过前端直接打下载接口）；前端 Skills 页隐藏下载按钮、改显「🔒 仅集成」标签。适用于知识库这类不希望被整包带走、只允许在平台内被 Agent 调用的能力。
  - **目录型 Skill 扫描**：`skills_dir` 下 `<slug>/SKILL.md` 结构（含 `scripts`/`references` 子目录）识别为「能力包」（`is_dir=1`）；允许下载的能力包打包成 zip，禁止下载的只展示 `SKILL.md` 正文。
  - 验证：DB 入库 `is_dir=1, downloadable=0`；API 实测列表返回该 Skill、`/download` 返 403「仅供 Agent 集成使用」；前端 dist 含最新代码。
- ✅ **QA 套件回归归零：从 TypeError 硬崩恢复到 31/31 全绿**（能力 `agent-collaboration`）
  - **随 v0.16.2「新项目空团队」同步测试契约**：v0.16.2 有意移除新项目自动种子 Leader（产品行为正确），但 QA 套件旧断言仍验「自动种子 Team Leader」、且后续协同块 `leader["slug"]` 无保护——`leader=None` 时抛 `TypeError` 直接中断整套件。修法：断言反转为「新项目从空团队开始（无负责人）」，并新增「显式导入项目负责人并 `PUT .../leader` 设为 Team Leader」一项，模拟用户自选负责人的新流程、拿到 `leader_slug` 供协同测试复用。
  - **定位并修复长期被误当「漂移」的协同 `order=[]` 真 bug**：QA 套件的假执行器 `fake_execute_dispatch` 签名过时（缺 `persist_user_msg`/`user_name`），而 `collab._run_one` 以 `runner.execute_dispatch(..., persist_user_msg=False)` 调用它 → 创建异步生成器时抛 `TypeError`、被外层 `except` 吞成 run failed → 采集列表空 → `order=[]`。此失败横跨 07-07 起 5 份历史报告，一直被记为「既有 harness 漂移、与改动无关」。v0.16.0 曾修过 concurrency/subtask 两个探针的同类签名漂移，但漏了 QA 套件这处，本次补齐（桩加 `persist_user_msg=True, user_name=""`）。
  - 均为测试侧修复，产品代码零改动；QA 30→31 项，31/31 全绿。

### v0.16.2 — 2026-07-08
- 🔧 **切换项目即时刷新 + 新项目从空团队开始**（OpenSpec change：`2026-07-08-empty-team-and-route-refresh`，能力 `project-management`）
  - **切项目不刷新修复**：此前从项目 A 切到项目 B（工作区/团队概览）仍显示 A 的旧数据、需手动刷新。根因是 Vue Router 切换同名路由组件时复用实例、不重新 mount，`pid` 与数据停在旧项目。修法：`App.vue` 的 `<router-view :key="$route.path">`——路由 path 变化强制重挂载，一行覆盖所有参数驱动页面（项目/任务详情/工作区）；用 `path` 非 `fullPath`，忽略 `?tab=` query 变化不打断同页 tab 切换。
  - **新项目不再默认塞固定负责人**：此前新建项目自动把「项目负责人（星）」设为 Team Leader，导致所有项目都复用同一个。移除 `create_project` 的 `_seed_leader`——新项目从空团队开始，由用户自行从人才库导入选定 Agent、再用「设为负责人」指定 Leader。既有项目不受影响。
  - 验证：隔离验证新建项目成员数=0；前端 build 通过；`openspec validate --specs` 12/12。

### v0.16.1 — 2026-07-08
- 🔄 **重跑子任务时父任务状态即时回写 + 会话结构化排版**（OpenSpec change：`2026-07-08-rerun-reactivate-and-structured-output`，能力 `agent-collaboration`/`agent-execution`/`task-system`）
  - **父状态显示滞后修复**：去子任务卡片重新触发执行时，后端 `auto_dispatch` 即时把该子任务及其父任务（若已 done/reviewing）回写 `in_progress`，前端 `rerunTask` 同步做乐观更新——不再「先显已完成、隔几秒才变进行中」。仅对已收尾任务的重跑生效，首次执行不误伤。
  - **会话字体有了主次层次**：此前 Agent 用 `━━━` 装饰线 + emoji 当章节标题、几乎不用 `##`，渲染出来扁平无层次。两侧一起改——
    - **生成侧**：`jian` CLI 使用说明 + 负责人收尾 prompt 要求用 Markdown 结构（`##`/`###` 小标题、`**粗体**` 标关键项、`-` 列表），不用装饰线冒充标题。
    - **渲染侧**：`MarkdownView` 强化标题字号/字重/颜色差 + h1/h2 底部细分隔线，粗体作为字段名标签更深，列表留白优化——结构化内容一眼看出主次。
  - 老消息（含 `━━━`）照旧渲染，新消息更结构化。
  - 验证：新增 `TestReport/run_reactivate_probe.py` 5/5（子任务重跑回写父+子、独立任务回写自身、首次执行不误伤）；前端 build 通过；回归 timeout+QA 12/12、subtask 6/6、concurrency 7/7、reflect 6/6、QA 28/30。

### v0.16.0 — 2026-07-08
- ⏱️ **执行超时重构：静默超时 + 保成果 + 收尾验收路由**（OpenSpec change：`2026-07-08-idle-timeout-and-qa-routing`，能力 `agent-collaboration`）
  - **超时策略从「固定墙钟」升级为「静默超时(A) + 宽限保成果(B) + 硬墙钟兜底(C)」**：真实案例——数据工程师经 Narya/ingest 遍历全库补数据、真在干活却撞 60 分钟墙钟被误杀、且已完成的成果被销毁。
    - **A 静默超时**：不看总耗时，看「多久没有新输出事件」——持续产出就不判超时，仅连续无事件（真卡死）才触发。默认 15 分钟，数据类角色 30 分钟。慢但在干活的任务永不被误杀。
    - **B 超时保成果**：判超时后先给 90s 宽限并轮询，若该 run 已产出真实交付（jian comment/subtask 或改过任务状态）则按**成功**处理、不 kill 不销毁；宽限内仍无交付才 kill 进程树 + 落 failed。
    - **C 硬墙钟兜底**：总时长天花板（默认 3 小时、数据类 4 小时），防既不静默也不结束的极端失控。
  - **收尾支持测试/验收路由**：父任务全子完成后唤醒负责人收尾时，指令从写死「无需 @ 任何人、直接汇总」改为「**如原计划需要验收，先 @ 相应成员验收、通过后再汇总**」；团队有测试/QA/安全成员时点名提示。修复了"用户要求找测试专员验证，却被自动收尾机制架空、测试专员从不出场"的问题。
  - **僵尸运行清理**：清理两条卡死 14 小时的 `running`（子任务已完成、进程已死），落终态 killed。
  - 验证：新增 `TestReport/run_timeout_and_qa_probe.py` 12/12；修复期间发现并修正 `_grace_then_kill` 缺 `runner` 局部导入的真 bug（否则超时路径线上会崩）；修复 3 个既有探针的假执行器签名漂移（缺 `persist_user_msg`），concurrency 2/7→7/7、subtask 2/6→6/6 恢复真实覆盖；memory-hygiene 11/11、stdout-display 8/8、reflect 6/6、QA 28/30。

### v0.15.0 — 2026-07-07
- 🧠 **记忆卫生：让 Agent「越做越强」而非「越背越沉」**（OpenSpec change：`2026-07-07-memory-hygiene-and-board-polish`，能力 `agent-execution`/`agent-memory`/`agent-reflection`/`task-board`）
  - **近期动态只存净交付（P0-1）**：收工写记忆只记本轮该 Agent 经 `jian comment`/`jian subtask` 落库的净交付，**不再拿流式 stdout 兜底**（jian.bat 调用、PYTHONUTF8、编码提示等过程碎语不再进记忆）；滚动上限 8 → 3。无净交付则不记（未走 jian 已由执行层打醒目标记）。
  - **Know-how 按相关性精选注入（P0-2）**：新增 `memory.select_relevant_knowhow`——用 jieba 分词做「当前任务 ↔ 各 Know-how 条目」关键词重叠打分，注入系统提示时只放最相关的 top-8。**文件里 Know-how 全量保留**，仅注入时精选，避免无关经验随任务增多堆积、稀释领域硬经验、撑大上下文。注入的 Know-how/近期动态一并剥离内部归属标记（省 token）。
  - **反思质量门槛（P1-3）**：反思 prompt 增「宁缺毋滥 + 只沉淀本专业领域新方法/新坑/新诀窍；常规沟通/介绍/汇报类无专业增量则回『无』、不写通用套话」——根治低含金量任务灌入同质化空话。
  - **会话历史滑动窗口（P1-4）**：回灌只保留最近 20 条消息（早期丢弃），对 CLI/API 两路径同时生效，防协同长 thread 撑爆上下文、诱发 lost-in-the-middle 幻觉。
  - 分词依赖：优先用 jieba（已装），缺失时自动退化为「英文词 + 中文 2-gram」零依赖方案。
  - 验证：新增 `TestReport/run_memory_hygiene_probe.py` 11/11（相关性精选/标记剥离/条目≤N全给/recent上限/history窗口/反思门槛）；`run_stdout_display_probe` 8/8、`run_reflect_probe` 6/6、QA 28/30 与基线一致；真实记忆冒烟：数据向任务注入的 8 条全为数据经验、前端向经验被正确过滤。
- 🗂️ **工作区看板对齐与排序**：卡片执行状态（执行中/完成/失败/终止）用 `margin-left:auto` 固定右对齐，不再因有无子任务进度而左右横跳；顶层任务列表排序由 `order_idx` 改为**创建时间倒序**（最新任务出现在每列最上方）。

### v0.14.1 — 2026-07-07
- 🧹 **会话正文只留真实交付，命令过程碎语归入日志**（OpenSpec change：`2026-07-07-cli-stdout-not-in-thread`，能力 `agent-execution`）
  - **问题**：CLI Agent（Claude/Codex）的会话正文里混进大量执行过程碎语——如「`jian` 命令通过 `jian.bat` 调用」「设 `PYTHONUTF8=1`」「连通正常（roster 已取到，仅打印 GBK 编码崩）」「结尾那句是终端编码显示问题，实际已发送成功」。正常问答/结论没问题，但命令细节不该进正文。
  - **根因**：`runner.execute_dispatch` 把后端的**流式 stdout 全文**无条件落成一条会话消息。而 CLI Agent 的**真实交付**走 `jian comment`/`jian subtask`（已单独落库、干净）——同一产出被记两遍。线上数据印证：每条噪声消息长度与对应 run 的 stdout 落库长度逐条精确相等。
  - **修复**：按后端类型分流——**CLI 后端的流式 stdout 不再落成会话正文消息**（仍全量进 `run_logs`，日志详情可排查）；**API 后端不变**（无 jian 通道，stdout 即唯一产出，照常展示）。与 `_persist_memory` 既有原则一致（jian comment 发言＝真实产出 ＞ stdout 兜底）；收工写记忆的 stdout 兜底不受影响。
  - **CLI 未走 jian 交付时打标记，不拿 stdout 兜底**：目标是让 `jian comment` 100% 出现，而非用可能错误的 stdout 结论兜底（无意义）。若某 CLI run 成功结束却无任何 jian 平台动作（`jian comment` 落消息 / `jian subtask`/`jian status` 记活动），落一条 `⚠️ …未通过 jian comment/subtask 提交交付…` 系统活动便于追查（stdout 仍在日志详情里）；`_has_jian_deliverable` 兼顾"只委派不发言"的 Leader，不误判。
  - **历史噪声清理**：全库按三重精确判据（内容==该 run stdout 拼接 + 同会话同 agent + CLI 供应商）识别出 9 条 stdout-mirror 噪声（克里珀 task53/55/56/57/58/59），删前备份 `jianagency.db` 且逐条校验同会话同 agent 另有 jian 交付（正文不空），按精确 msg id 删除、复核残留 0。
  - 验证：`TestReport/run_stdout_display_probe.py` **8/8**（CLI stdout 不落正文但进日志 / 无 jian 打标记 / jian 交付保留且不打标记 / API 照落且不打标记）；QA 套件 28/30 等回归与改动前基线逐项一致（既有 2 项协同排序失败为测试 harness 漂移、与本改动无关）。仅改 `backend/executor/runner.py`，前端无改动、无数据迁移。

### v0.14.0 — 2026-07-07
- 📜 **执行日志与历史列表重做 + 日志详情增强**（OpenSpec change：`2026-07-07-exec-log-history-and-fixes`）
  - 右侧执行日志区重做为历史运行折叠列表：进行中运行常显、历史运行折叠「显示历史运行（N）」，每行 = 状态图标（hover 看 Agent 名+状态+可点终止/重跑）+ 命令缩略版（撑满截断）+ 相对时间（刚刚/N分钟前/N小时前/N天前），行 hover 右侧换「日志详情」入口；取代原「多实例并行独立卡片」
  - `/tasks/{id}/runs` 每条附 `summary`（首条工具命令缩略、脱敏）
  - 日志详情弹窗：工具事件展开同时显示「命令/参数 + 运行结果」（补 codex 输出丢失）；Claude 的 `tool_result` 按 `tool_use_id` 回填工具名（标签显示 Bash 而非「结果」）；右侧显示执行北京时间（去序号）；顶部显示供应商名·模型；筛选去空名、助手发言项命名「发言」并带绿色标签
  - 助手发言行内显示绿色「发言」标签，与工具标签一致
- 📝 **富文本 / Markdown 渲染**：新增 `MarkdownView`（marked GFM + DOMPurify 消毒），任务描述与消息气泡按 Markdown 渲染，支持标题/粗体/列表/表格/代码块/图片/可点击链接（含裸链接自动识别），外链新标签打开；正文统一 14px
- 🔧 **人工验收闭环多处修复**：
  - 子任务 `jian status done` 不再误降级为「验证中」（子任务无验证中概念，直接完成），修复父任务卡死
  - 修复子任务全完成后父任务收尾的**核心竞态**：`on_execution_complete` 在 run 执行内调用时，触发它的队列行尚未标 done、会把自己算作 pending，导致父任务永远无法自动进验证中/唤醒汇总——`_has_pending_run` 增 `exclude_run_id` 排除自己
  - 重构后补回「全子完成自动唤醒负责人做统一汇总汇报」环节
  - 子任务被标 `reviewing` 归一为 `done`，避免卡死
  - 子任务执行/重跑时父任务处显示「进行中」而非旧的已完成状态
- 🖼️ **交互与展示细节**：项目卡片/概览展示仓库链接（不再暴露本地工作区目录）；执行状态改用 Element Plus 图标（红色圆形停止/绿勾/红叉/灰减号）；任务详情返回按钮改「返回」，子任务返回父任务；用户消息/活动按用户名显示同名头像（AgentAvatar 加载失败回退 emoji）；记忆数据仅管理员可见
- 🐛 **jian comment 支持 `--body-file`/`--stdin`**：修复多行长发言经 Windows `.bat`/`pwsh` here-string 被截断成第一行的问题（花火/三月七自我介绍只落一句即此）；系统提示要求长内容用 `--body-file`
- ⚙️ 后端默认开启热加载（`JIANAGENCY_RELOAD` 默认 1）

### v0.13.0 — 2026-07-06
- ✅ **人工验收闭环 + 执行日志详情**（OpenSpec change：`2026-07-06-human-acceptance-and-log-detail`，能力 `task-board`/`task-system`/`agent-execution`/`agent-collaboration`）
  - **人工验收（Human-in-the-loop）**：新增「验证中（reviewing）」一等状态，插在「进行中」与「已完成」之间。Agent（含 Leader）`jian status done` 一律降级为 `reviewing`，永远无法把任务标为真正完成
    - `progress.on_execution_complete`：子任务执行成功自动置 `done`；父任务全部子任务 done 后自动进 `reviewing`；独立任务完成直接进 `reviewing`。`blocking_subtasks` 改按「是否还有 queued/running run」判定
    - 经验反思与「已解决数」计数**只在人工验收触发**——管理员手动把父任务/独立任务拖入「已完成」，对父任务连同子任务一起反思；自动流程绝不触发
  - **执行日志详情**（结构化 transcript 设计）：每次 run 的工具调用完整留存工具名 + 入参（含 Bash 实际命令）+ 输出
    - `run_logs` 表新增 `tool`/`tool_input`/`tool_output` 三列（轻量迁移，向后兼容）；执行器 `ExecEvent` 扩展工具字段，claude/codex 解析保留命令与输出
    - 新增 `GET /runs/{id}/transcript` + `backend/redact.py` 服务端脱敏；前端 `RunTranscriptDialog.vue` 全屏弹窗（timeline 进度条 + 元数据 + 过滤 + 排序 + 复制 + 逐条展开）
    - SSE `dispatch` payload 带 `tool_input`/`tool_output`，执行中工具行可实时展开看命令详情；前端 `utils/redact.js` 兜底脱敏
  - **看板与子任务**：新增「验证中」独立列（浅蓝样式）；子任务以嵌套小卡片呈现在父卡片下；子任务支持描述 + 优先级、强制两级；看板 `list_tasks` 返回每个父任务的 `subtasks`
  - **打磨**：活动时间线按 slug+name 双匹配，历史 slug 记录也显示昵称/头像；`jian` 子任务活动以「昵称（角色名）」呈现；移除项目概览冗余信息卡
  - 验证：前端 `vite build` 通过；后端各模块语法校验通过；`run_logs` 迁移在既有库跑通；claude 解析器单测（text+Bash完整命令+tool_result 拆分）；`/transcript` 端到端含脱敏验证通过

### v0.12.0 — 2026-07-03
- 🧠 **任务完成经验反思**（OpenSpec change：`2026-07-03-task-completion-reflection`，能力 `agent-reflection`）——让 Agent「越做越会做」
  - 新增 `backend/reflect.py`：任务进入 `done`（拖入已完成 / `jian status done`）时，对本任务+子任务里**真正跑过 run 的每个角色**触发一次复盘，用其**自身模型+人格**提炼 3-5 条可迁移 Know-how，写入记忆受管段落 `knowhow`
  - 新增 `runner.run_oneshot`：一次性模型调用（不建 run / 不落 messages / 不碰会话），供反思、总结等"借模型想一段"的场景；超时/异常返回空串、不影响主流程
  - 产出是**要领非存档**：prompt 明确"聚焦方法/坑/诀窍/判断依据、不要复述结论数字"，无可沉淀时不写；Know-how 去重合并，超 `KNOWHOW_MAX=30` 条时调模型压缩成 Top-N，防膨胀污染人格
  - per-run 记忆职责调整：`_persist_memory` 从"无限 append 结论存档"改为滚动受管段落 `recent`（保留最新 8 条），只做轻量近期动态；学习交给反思
  - 触发点：`routes/tasks.py` + `routes/agent_cli.py` 的 `set_status(done)`，`asyncio.create_task` 后台异步不阻塞；测试项目跳过
  - 验证：新增 `TestReport/run_reflect_probe.py` 6/6（参与者写入 / 未参与不写 / 超限压缩 / 测试项目跳过）；并发探针 7/7、隔离主套件 30/30 保持通过；**真实 Claude 端到端**：完成任务后参与角色产出 5 条真实 Know-how（做事要领，非报告复述）

### v0.11.3 — 2026-07-03
- 🐛 **收工写记忆修复 + 弹窗昵称 + 看板精简 + 前端缓存自愈**
  - **做完任务不写记忆**：`append_memory` 原挂在执行生成器尾部，超时取消时收尾跑不到；且原来只记流式 stdout（过程碎语），而 CLI Agent 真实结论走 `jian comment` 落 messages 表。抽出 `runner._persist_memory`：优先取本轮 messages 里该 agent 的发言（真实交付），无则回退 stdout；`finalize_run`（超时兜底）也调它，做完的任务也能沉淀
  - **弹窗标题显示昵称**：ProjectDetail 的 Skills/人格/记忆弹窗标题从纯角色名改用 `#header` slot + 头像 + 「昵称（角色）」
  - **看板精简**：工作区去掉「验证中/阻塞」两列（暂无用，以后扩展）；后端状态机不动，前端把这两态任务并入「进行中」显示，防任务无列可显而消失
  - **前端资源缓存自愈**：`main.py` 加缓存头中间件（`/assets/*` 长缓存 immutable、index.html `no-cache`）+ `router.js` 捕获 chunk 加载失败强制整页重载一次——根治 build 后旧 index.html 引用已删 hash chunk → 404 → 点 Tab 无反应
  - 取消任务详情右上角"团队协同进行中/拖入即协同"文案（单人任务不协同、需协同时负责人自动拉人，措辞无意义）

### v0.11.2 — 2026-07-03
- ⏱️ **执行超时体系重构 + kill 竞争修复**（`collab.py` / `executor/runner.py`）
  - **超时按角色可配**：默认 360s → **1800s（30 分钟）**，数据类等长耗时角色（经外部取数服务拉数）放宽到 **3600s（60 分钟）**；`RUN_TIMEOUT_OVERRIDES` + `_run_timeout(slug)` 管理
  - **修 kill 竞争**：Windows `os.kill(SIGTERM)` 只杀父进程、子进程成孤儿继续跑 → 改用 `taskkill /F /T` 杀**整棵进程树**（POSIX 用 `killpg`）
  - **修孤儿 running**：超时时执行生成器被 `wait_for` 取消、收尾落库跑不到 → 新增 `runner.finalize_run` 主动把 `task_runs` 落成终态，杜绝永久 `running` 孤儿
  - **超时活动显示昵称**：`task_failed` 的 `actor_name` 从 slug 改用角色名，前端时间线显示「昵称（角色）执行超时…」
  - 验证：并发探针 7/7、隔离主套件 30/30

### v0.11.1（追加）— 2026-07-02
- 🔗 **父子任务闭环 + 协同健壮性**（`progress.py` / `collab.py` / `executor/claude_code.py`）
  - **子任务不结束，父任务不能结束**：`blocking_subtasks` 守卫——父任务有未完成子任务时，`jian status done`（成员）被拦截、管理员 `PUT /status` 返回 409（可 `force:true` 覆盖）
  - **全部完成→负责人汇总收尾**：`maybe_advance_parent` 在最后一个子任务 done 时把父任务置 `reviewing`，并**自动入队一条负责人总结 run**（带指令：看各子任务成果 → `jian comment` 统一汇报 → `jian status done`），幂等不重复触发
  - **防层层派生（cascade）**：只有顶层任务注入"派活"指令、子任务一律叶子工作；子任务内 @负责人不唤醒；收尾 run 不再追加派活指令。修复了「成员在子任务 @Leader → Leader 又派活 → 派生出一堆子子任务」的失控
  - **CLI 执行隔离三件套**：① `claude -p` 的 prompt 走 **stdin**（避免 Windows 超长 argv 截断致输出降级/解析不到事件，这是"负责人秒完成却啥也没干"的真根因）；② `--disallowed-tools Task,Workflow,SendMessage,TaskCreate…`（禁内置编排工具，逼走 `jian`，否则成员不被平台真正唤醒）；③ 子进程 `CLAUDE_CONFIG_DIR` 指向空目录（隔离宿主 `~/.claude/CLAUDE.md` 等全局人格污染；不用 `--safe-mode`，它会把 stream-json 降级成纯文本）
  - **跨供应商协同验证**：新增关键案例 `TestReport/run_collab_scenario.py`（真实 CLI 端到端、镜像真实团队 8 人跨 Claude+Codex 双供应商），验证 Codex 成员也能被真实唤醒、成员以昵称示人、无级联、父任务闭环
  - 验证：隔离主套件 30/30、并发探针 7/7、闭环单测（未完成不推进/全完成→reviewing+唤醒负责人/幂等）、防级联单测（子任务@负责人不唤醒/顶层@成员正常）全通过
  - ⚠️ **已知**：协同调度依赖 Leader（LLM）对协作协议+花名册的遵循程度，run-to-run 有波动（偶尔探索工作区后自由发挥、不建子任务）；负责人人格/协议提示仍在打磨

### v0.11.1 — 2026-07-02
- 🐛 验收修复（8 项）：
  - 🕐 **统一北京时间**：新增 `timeutil.to_beijing`，数据库仍存 UTC，API 边界（时间线/消息/执行日志/任务卡片）统一转 Asia/Shanghai 返回，历史数据一并正确
  - 🧹 **测试数据不入记忆**：`config.is_test_project` 判定（`__test__`/`__qa`/`__conc` 前缀）；runner 收工写记忆、`agent_memory_sync` 工作区段落均过滤测试项目；清理了负责人「星」及成员被污染的记忆文件
  - 👤 **动态显示登录用户名**：人类操作活动落库带用户名（`require_admin` 注入），旧空记录回退「管理员」；前端消息/活动统一用 `actor_display`/当前登录名，不再写死「我」
  - 📜 **修滚动条弹回**：任务详情时间线改「粘性底部」——仅当用户停在底部才自动跟随，向上翻阅时不再被 3 秒轮询强制拽回
  - 🏷️ **卡片「活动/对话」改「动态」**：字体加大加粗（16px/700）
  - 🚫 **Leader 不再 @ 自己 / 编造成员**：协作协议加铁律（只 @ 花名册真实成员、绝不 @ 自己、绝不杜撰）；`parse_and_enqueue_mentions` 强化自触发守卫，@ 不存在成员时记「⚠️ 无人被唤醒」活动（不再静默丢弃）；Leader 默认 prompt 先 `jian roster` 看真实成员
  - 🧩 **子任务联动父任务**：新增 `progress.py`——所有子任务完成后父任务自动进 `reviewing`（等负责人汇总收尾），未完成则保持进行中；新增 `GET /tasks/{id}/progress` 聚合父+子任务在跑/排队的 Agent，任务详情右侧执行日志区显示「执行中·子任务 N/M·哪些子 Agent 在跑」
  - 🗑️ 删除无引用的死代码 `TaskThread.vue`（实际生效的是 `TaskDetail.vue`）
- 验证：隔离主套件 30/30、并发探针 7/7 保持通过；新增进度聚合/父任务联动/时区/测试过滤隔离验证全通过

### v0.11.0 — 2026-07-02
- ⚡ 多 Agent 协同并发池 + 卡死兜底（`collab.py`）——取代原「单并发串行」循环
  - 并发池：`_loop` 一次性把空闲槽填满（`MAX_CONCURRENCY=3` 上限），短任务也能真正并行；慢 Agent 不再阻塞快 Agent
  - 卡死兜底：单个 Agent 执行超时（`RUN_TIMEOUT_SEC=360`，抽成模块常量）→ `runner.kill_run` 杀子进程 + 记 `task_failed` 活动 + 释放并发槽，卡死不再拖垮整条队列或留僵尸
  - 保留 `_tick()` 作为确定性单步原语（与生产 `_loop` 共用 `_claim_one`/`_process_one`），供测试逐步驱动队列
  - **修复 bug**：旧 `_loop` 每 claim 一个 run 就 `sleep(0.3)`，短任务永远填不满并发池（峰值卡在 2/3）；改为连续补槽
  - 验证：新增 `TestReport/run_concurrency_probe.py`（隔离临时库 + 假执行器）7/7 通过——超时 kill、run 落库 done、task_failed 活动、峰值达 3、并行快于串行、慢 Agent 不饿死快 Agent；隔离主套件 30/30 保持通过
  - OpenSpec：新增 change 提案 `2026-07-02-collab-concurrency-pool`，更新 `agent-collaboration` 规格（串行 → 并发池 + 卡死兜底）

### v0.10.0 — 2026-07-01
- 🪪 数字人才昵称 + 自定义头像（按身份 slug 跨项目共享，仅管理员可编辑）
  - 数据：`agent_profiles` 加 `nickname` / `avatar`（_migrate 平滑升级）
  - 后端：`agent-config/{slug}/profile` 设昵称头像（require_admin）；新增 `/api/icons` 列图 + 取图（路径穿越防护）；人才库/团队列表 join 出 nickname/avatar
  - 头像来源：项目根目录的 `icon/` 文件夹，放图即可在资料弹窗里选（含默认 emoji 选项）
  - 显示：全局统一「昵称（名字）」格式（无昵称只显名字）+ 圆形头像组件（无头像回退 emoji）；人才库、项目团队、任务面板、@选择器、对话角色全部接入
  - 前端：`AgentAvatar` / `AgentProfileDialog` 组件 + `displayName`/`avatarUrl` 辅助；修复弹窗首次打开 visible 不同步的时序 bug

### v0.9.2 — 2026-07-01
- 🔓 CLI 执行供应商默认全权限放开（效率优先，仅可信内网）——保障 Agent 不因授权/沙箱卡死
  - Claude CLI：加 `--permission-mode bypassPermissions`（与 `--dangerously-skip-permissions` 双保险）
  - Codex CLI：加 `--dangerously-bypass-approvals-and-sandbox` + `--cd`/`--add-dir`（Windows 下 Codex 沙箱会因 `CreateProcessWithLogonW failed:1385` 无法写文件，必须 bypass）
  - Codex 三处根因修复后**经 Akivili 完整跑通、真实写文件**：① 模型名对齐网关（gpt-5 → gpt-5.5）；② prompt 改用 stdin 传入（命令行传长 prompt 会被截断致 codex 只复述不执行）；③ 指令前置 + 子进程 NO_PROXY 兜底
  - 已沉淀为全局规范：新增任何 CLI 供应商默认全放开、无需授权、防卡死

### v0.9.1 — 2026-07-01
- 🐛 修复测试专员 QA 报告发现的 3 个 Bug（QA 套件从 35/38 → 38/38，协同实测得分 80 → 100）
  - P0：CLI 执行后端（claude/codex）此前忽略 `ctx.history`，协同中被 @ 的成员看不到负责人的委派与任务上下文、无法完成工作。新增 `executor.base.build_cli_prompt` 把会话历史+本轮指令拼进 CLI prompt；collab 成员 prompt 带上任务标题/描述。实测：成员现能按委派真实创建文件
  - P1：未知 `/api/*` 被 SPA 兜底返回 200 HTML → 改为返回 404 JSON（非 API 路径仍走 SPA）
  - P1：memory / skills 的 slug 含 `..` 未被拒绝 → 显式拒绝，符合 OpenSpec 安全契约
  - 回归：`py -3.12 TestReport/run_qa_suite.py --live --keep` 全绿 38/38

### v0.9.0 — 2026-06-30
- 🤝 多 Agent 协同（OpenSpec change：`2026-06-30-multi-agent-collaboration`，能力 `agent-collaboration`）——Team Leader 调度机制
  - 核心思想：调度智能外包给 LLM（注入协作协议+团队花名册）；@mention 是 Agent 间唯一通信原语；事件驱动队列串行调度；多层防死循环
  - 后端：新增 `run_queue` 表 + `collab.py`（asyncio 后台循环串行领取执行 + @mention 解析入队 + pending 去重 + Leader 自触发守卫 + 协同深度上限 20）；`TEAM_LEADER_PROTOCOL` 协作协议 + 动态团队花名册（成员名/技能/@语法）注入 Leader 系统提示；复用 is_leader（Team Leader 即 squad leader）与 agent_skills
  - 触发：任务拖到「进行中」/「▶ 启动团队协同」按钮 → 唤醒团队负责人统筹；负责人 @mention 委派 → 成员入队执行 → 完成回报 → 负责人汇总收尾
  - 前端：任务详情右栏加「启动团队协同」按钮；协同全过程经活动时间线/执行日志区/对话区实时呈现（3 秒轮询）
  - 验证通过（真实 Claude + 无头环境）：run_queue 串行、Leader 读任务后 @前端开发者 委派（未自己动手）、@解析自动触发成员执行、成员被唤醒干活、活动时间线完整记录 task_started/completed 与委派

### v0.8.0 — 2026-06-30
- 📋 升级任务体系（OpenSpec change：`2026-06-30-task-system-upgrade`，能力 `task-system`）——看板式两栏布局，配色沿用 Akivili 星穹风
  - 数据：tasks 加 `priority`（紧急/高/中/低/无）、`parent_task_id`（子任务）；状态扩展 待办(backlog)/阻塞(blocked)；新增 `activities` 表（活动时间线）
  - 后端：关键动作埋点写活动（created/status_changed/priority_changed/task_started·completed·failed/commented）；新增 activities（活动+对话合并时间线）/subtasks/priority 接口；看板列表带子任务 done/total 进度
  - 前端：TaskThread 重构为**两栏大弹窗**——中间（描述 + 子任务区 + 活动/对话时间线 + @输入）+ 右侧属性栏（状态/优先级/负责人 picker + 执行日志区可展开每次 run 的日志）；看板卡片加优先级圆点 + 子任务进度；看板列适配 待办/阻塞
  - 验证通过（无头 Chrome 自测）：两栏布局渲染、时间线混排、右栏状态/优先级 picker、子任务进度、执行日志区、活动埋点全部正常

### v0.7.0 — 2026-06-30
- 🔐 内网访问 + 登录鉴权（OpenSpec change：`2026-06-30-intranet-auth-rbac`，能力 `auth-rbac`）
  - 认证：PBKDF2 加盐哈希（hmac.compare_digest 常量时间校验）、token + httponly cookie；新增 `auth.py` / `routes/auth.py`（login/logout/me）；`users` 表 + startup 播种管理员
  - 管理员账号：环境变量 `AKIVILI_ADMIN_USER` / `AKIVILI_ADMIN_PASSWORD`（role=admin，缺省占位 `admin` / `changeme`）
  - 权限：管理员可写可执行（全部 POST/PUT/DELETE 加 `Depends(require_admin)`）；匿名/其他用户只读浏览项目空间、数字人才库、Skills，看不到设置，不能安排任务、不能增改 Agent/Skill
  - 前端：登录态全局注入（provide currentUser/isAdmin），按角色 v-if 隐藏设置 Tab 与所有写按钮；右上角身份与登录/退出；axios withCredentials
  - 内网开放：后端 host=0.0.0.0，前端 vite host=true，CORS 加内网 IP
  - 验证通过：播种管理员、登录拿 cookie、管理员可写、登出/匿名写 401、匿名 GET 只读 200，经内网 IP 全链路验证
  - ⚠️ 安全提醒：管理员 @ Agent 仍是放开权限执行（能在主机改文件跑命令），管理员账号权限大，密码请妥善保管；仅限可信内网使用

### v0.6.0 — 2026-06-30
- ✅ 完成 P4 工作区看板 + 单 Agent 真实执行（OpenSpec change：`2026-06-30-task-board-and-execution`）
  - 数据：新增 `tasks` / `task_runs` / `run_logs` 三表；复用 `conversations`/`messages` 作任务对话 Thread
  - 执行引擎 `executor/`：`base`（抽象+流式事件）、`claude_code`（claude -p stream-json，放开权限+--add-dir 锁目录）、`codex`（codex exec --json）、`api_llm`（httpx 流式双格式）、`runner`（组装人格+记忆+Skills+会话历史→选后端→PID 注册表→收工写记忆）
  - 子进程列表传参防注入、stdin=DEVNULL、to_thread 卸载阻塞、SSE 检测断开
  - 后端：`routes/tasks.py`（CRUD+状态流转+看板分组）、`routes/runs.py`（@分派 SSE、kill、日志、消息历史）
  - 前端：`Workspace.vue`（5 列看板+新建+移动）、`TaskThread.vue`（任务内对话终端：@选择器、流式输出、Kill、日志面板）；项目页加「进入工作区」入口
  - 验证通过：任务 CRUD+5 态流转+看板分组、@分派流式执行（自动 Leader 用人格回复）、消息落库、run 状态、54 条日志、收工写记忆、kill 真实终止子进程；错误流（如 codex 网关断连）如实呈现不崩溃
  - 说明：codex-cli 连 B 站网关有环境问题（模型元数据/流式断连），非 Akivili bug；执行引擎正确捕获展示其错误

### v0.5.2 — 2026-06-30
- 👑 团队总负责人（Team Leader）角色
  - 新建项目时自动把「项目负责人」拉入团队并设为 Team Leader，排序置顶
  - 可手动把任意成员「设为负责人」，自动取消原负责人（每项目至多一个）
  - Leader 卡片金色高亮 + 👑标识，团队列表 Leader 始终排第一位；Leader 负责调度顶层任务与分发
  - 后端：`project_agents` 增 `is_leader` 列（含轻量迁移 ALTER）；列表按 `is_leader DESC, id` 排序；新增 `PUT /projects/{pid}/agents/{id}/leader`；创建项目时 `_seed_leader` 自动配置
  - 措辞：「Agent 库」→「数字人才库」；项目内「Agent 团队」→「团队」；空状态「还没有人才加入」

### v0.5.1 — 2026-06-30
- ✨ Agent 记忆自动同步「工作区」与「Skills 使用说明」（受管段落机制）
  - 加入项目时，自动在该 Agent 记忆里写入🗂️工作区段落：列出其所在的**所有**项目（项目名→本地路径）并约束「只在对应项目路径内操作、不得越界」；多项目累加，移除项目自动回退
  - 配置 Skills 时，自动写入🧩可用 Skills 段落：每个启用 Skill 的名称、描述、使用要领，提示 Agent「遇到对应场景主动调用」
  - 受管段落用锚点（`<!-- akivili:managed:* -->`）包裹，从数据库真实状态实时重建，幂等；**完全不影响用户手写的记忆内容**
  - 后端：新增 `agent_memory_sync.py`；`memory.py` 增 `upsert_managed_section`；在 project_agents 的导入/自建/移除、agent_config 的设置 Skills 处触发同步
  - 修复 `upsert_managed_section` 用 `re.sub` 替换时，Windows 路径反斜杠被当正则转义导致的崩溃（改用函数式替换）

### v0.5.0 — 2026-06-30
- ✅ 完成 P3.5 Agent 配置 + Skills（OpenSpec change：`2026-06-30-agent-config-and-skills`）
  - 数据：新增 `agent_profiles`（按 slug 存接入模型）/ `skills` / `agent_skills`（按 slug 存启用技能）三表
  - 后端：`skills.py`（扫描 `skills/` 目录入库，仿 agents.py）+ `routes/skills.py`（列表/详情/重扫/新建）；`routes/agent_config.py`（读写某 slug 的接入模型与启用 Skills，upsert）；`config.py` 增 `skills_dir`；startup 建目录+扫描
  - 前端：新增「Skills」导航与 `Skills.vue`（库浏览/搜索/详情/新建/重扫）；`ProjectDetail.vue` 的 Agent 卡片增「接入模型」下拉 +「Skills」勾选弹窗 + 跨项目共享提示
  - 核心机制：模型 / 记忆 / Skills 均绑定在 Agent 身份（slug）上 → 同一 Agent 无论进哪个项目都互通；人格（persona）仍各项目独立可改造
  - 验证通过：skill 扫描入库、选模型勾 Skills 保存、**跨项目互通**（项目甲配、项目乙读到一致）、不同 Agent 配置独立、新建 skill 落盘、路径穿越拦截、前后端代理打通、build 成功

### v0.4.1 — 2026-06-30
- 🪐 项目正式定名 **Akivili（阿基维利）**，Slogan：**愿此行，终抵群星！**
  - 首页新增星空 Hero 横幅（深空渐变 + 星点 + 金色流光 Slogan）；侧边栏品牌、页面标题、后端 app title、启动脚本、OpenSpec 文档同步更名
  - 物理标识保持不变（数据库文件 `jianagency.db`、`JIANAGENCY_RELOAD` 环境变量），仅更新展示层与文档，避免数据/路径迁移风险

### v0.4.0 — 2026-06-30
- ✅ 完成 P3 项目管理 + Agent 记忆机制（OpenSpec change：`2026-06-30-projects-and-memory`）
  - 后端：`projects.py` + `routes/projects.py`（项目 CRUD，创建/更新校验 local_path 为已存在目录）；`routes/project_agents.py`（从模版导入 / 列表 / 自建 / 改造 persona / 移除，导入继承模版 slug 使同一 Agent 跨项目共用记忆，自建生成全局唯一 slug）；`memory.py` + `routes/memory.py`（读/写/追加，白名单正则防路径穿越）；`config.py` 增 `memory_dir`；startup 建 memory 目录 + README
  - 前端：重写 `Dashboard.vue`（项目卡片网格 + 统计 + 新建对话框）；新增 `ProjectDetail.vue`（项目信息 + Agent 团队管理 + 改造人格抽屉 + 记忆查看/编辑抽屉）
  - 验证通过：项目 CRUD（无效路径 400）、导入两个新 Agent、改造 persona 持久化、**同一 Agent 跨项目共用记忆**（项目甲写、项目乙读到）、自建 Agent 独立且唯一 slug、路径穿越拦截（`../`/绝对路径/子目录全拒绝）、前后端代理打通、build 成功
  - 记忆约定：`memory/<agent-slug>.md` 每个文件是一个 Agent 的跨项目持久记忆；Agent 开工先读、收工写回（自动闭环 P4 落地）

### v0.3.0 — 2026-06-30
- ✅ 完成 P2 Agent 模版库（OpenSpec change：`2026-06-30-agent-template-library`）
  - 后端：`agents.py`（扫描 Agent 库目录、手解析 frontmatter + 正文、排除 examples/integrations/strategy 等非角色目录、幂等 upsert）、`routes/agents.py`（列表 + division/关键词过滤、详情含 body、分类统计、重新扫描）；`config.py` 增 `agent_library_dir`；`main.py` startup 空库自动扫描
  - 前端：`Agents.vue` 库浏览页（搜索 + 分类筛选 + 卡片网格 + 人格详情抽屉 + 重新扫描）
  - 验证通过：扫描入库 268 个 Agent（19 分类，跳过 6 个非角色文件）、搜到「项目负责人」「测试专员」并查看完整人格正文、分类筛选数量正确、幂等重扫（updated=268 总数不变）、前后端代理链路打通、build 成功

### v0.2.0 — 2026-06-30
- ✅ 完成 P1 地基（OpenSpec change：`2026-06-30-foundation-config-settings`）
  - 后端：`config.py`（多供应商模型 + config.json 持久化 + 密钥脱敏）、`database.py`（SQLite 7 张基线表）、`main.py`（FastAPI + CORS 白名单 + 启动建库）、`routes/settings.py`（读取/保存/连通性测试）
  - 前端：Vue3 + Vite + Element Plus 骨架（侧边导航、路由、API 封装），`Settings.vue` 多供应商配置页（API + CLI 两类、7 个 API 预设、密钥 password、连通测试、默认供应商）
  - 验证通过：保存→读回脱敏、Claude Code CLI 检测到 `claude 2.1.170`、API 连通测试返回真实状态码、前端构建成功、前后端代理链路（3100→8100）打通
- 🧭 确立首批 Agent 团队（模版位于 Agent 库目录）
  - 新造「项目负责人」（顶层管理者，为结果负责，可调度下属 Agent 协同）
  - 新造「测试专员」（验收 + 接口测试 + 代码/业务/风控安全把控与兜底，证据驱动、默认不通过）
  - 研发角色用库内现成中文版：前端开发者、后端架构师、软件架构师、高级开发者、代码审查员

### v0.1.0 — 2026-06-30
- 🎉 项目初始化：确立项目定位、技术方案与四项地基决策
  - 执行引擎：CLI 执行器（Claude Code / Codex）+ 纯 LLM API 三选一
  - Agent 来源：从 Agent 库目录 中文 Agent 库导入
  - 工作流形态：可视化展示 + 配置编排
  - 技术栈：FastAPI + Vue3 + Element Plus + SQLite
- 📁 创建项目骨架：README、OpenSpec 目录、前后端目录结构
- 📋 建立 OpenSpec 基线与首个变更提案（P1 地基）

## 许可与安全

- 许可协议：[MIT License](./LICENSE)。
- 安全策略与部署须知：见 [SECURITY.md](./SECURITY.md)。⚠️ 本平台 Agent 以放开权限执行，**仅限可信内网/本机**运行，切勿暴露公网。

---

> 本 README 是项目的唯一事实源，记录版本记录、技术方案与功能列表。功能迭代与需求澄清由 `openspec/` 管理。

