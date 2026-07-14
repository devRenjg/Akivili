# agent-execution

## Purpose

@ 分派任务给团队成员后，该 Agent 按其接入模型真实执行，流式输出全过程，执行前读记忆与会话历史恢复上下文、执行后写回记忆；执行可暂停 / kill，状态与日志可监控。本期聚焦单个 Agent 执行，Agent 间协同留待后续。

## Requirements

### Requirement: @ 分派与真实执行

用户 SHALL 能在任务对话中 @ 负责人下达指令，触发该 Agent 真实执行；会话正文 SHALL 只呈现 Agent 的真实交付与结论，不混入执行过程碎语。

#### Scenario: 分派执行
- **WHEN** 用户在任务 Thread 中 @ 某成员并下达指令
- **THEN** 系统按该 Agent 的接入模型启动执行（CLI 在项目目录内可改文件/跑命令，API 为纯对话），流式返回过程

#### Scenario: 上下文恢复
- **WHEN** Agent 开始执行
- **THEN** 系统读取其记忆与该任务的会话历史组装进上下文：记忆里的 Know-how 按**与当前任务的相关性精选**注入若干条（非全量），近期动态与工作区约束一并注入，注入文本剥离内部归属标记；会话历史按**滑动窗口只回灌最近若干条**，避免长 thread 撑爆上下文

#### Scenario: CLI 交付经 jian、过程仅入日志
- **WHEN** CLI 后端（claude/codex）的 Agent 执行完毕，流式 stdout 里含执行过程碎语（如 jian 命令用法、环境变量、终端编码提示等）
- **THEN** 该 stdout 全量记入 run_logs（供日志详情排查），但**不**落成会话正文消息；Agent 的真实交付经 `jian comment` / `jian subtask` 单独落库并在正文展示

#### Scenario: CLI 未产出 jian 交付时打标记而非兜底
- **WHEN** CLI 后端的 Agent 本轮成功结束，却没有任何 jian 平台动作（未 `jian comment` 落本会话消息、也未 `jian subtask`/`jian status` 记本人活动）
- **THEN** 系统落一条醒目的系统活动标记（`⚠️ …未通过 jian comment/subtask 提交交付…`）便于发现追查，而**不**把流式 stdout 当作结论兜底展示（stdout 仍完整保留在 run_logs / 日志详情里）

#### Scenario: API 交付即 stdout
- **WHEN** API 后端的 Agent 执行完毕
- **THEN** 其 stdout 最终文本即该 Agent 的产出，落成 assistant 会话消息在正文展示（API 无 jian CLI 通道，不打上述标记）

#### Scenario: 收工写记忆
- **WHEN** 一次执行结束
- **THEN** 系统把关键结论写回该 Agent 记忆的「近期动态」段落：只取本轮该 Agent 经 jian comment/subtask 落库的净交付，无净交付则不写（不拿流式 stdout 兜底）

### Requirement: 执行控制

用户 SHALL 能终止（kill）正在运行的执行。

#### Scenario: Kill 运行
- **WHEN** 用户对运行中的执行点击 Kill
- **THEN** 系统终止其子进程（含整棵子进程树，Windows `taskkill /F /T`，避免子进程成孤儿），执行状态置为 killed

#### Scenario: 陈旧 pid 不得被误杀（pid 复用防护）
- **WHEN** 系统在 kill 前发现目标 pid 对应的进程已退出，或该 pid 已被操作系统复用给另一个进程（进程创建时间与注册时记录的指纹不符）
- **THEN** 系统 SHALL 拒绝执行 kill 并清除该陈旧登记，绝不对被复用 pid 的进程（及其子进程树）动手
- **注**：run 注册 pid 时同时记录 `(pid, 进程创建时间)` 双因子指纹；run 无论正常收尾还是超时兜底，SHALL 无条件清除 pid 登记，杜绝收工善后异常导致陈旧 pid 残留

### Requirement: CLI 子进程输出完整捕获

对 CLI 执行后端（claude / codex），系统 SHALL 完整、无死锁地捕获子进程的 stdout 与 stderr，绝不因两个管道的读取次序而互相阻塞。

#### Scenario: stderr 大量输出不阻塞 stdout 读取
- **WHEN** CLI 子进程同时向 stdout 与 stderr 写出，且 stderr 输出量超过操作系统管道缓冲区（如 codex `--json` 把大量日志打到 stderr）
- **THEN** 系统 SHALL 用独立读者并发抽干 stderr，使子进程不会因 stderr 缓冲写满而阻塞、进而拖住 stdout；stdout 事件 SHALL 被完整、及时地捕获，不出现「进程实际已完成却收不到其输出、被误判静默超时」

### Requirement: 执行终态不泄漏（孤儿回收）

每次执行 SHALL 最终落到明确终态（succeeded / failed / killed），绝不长期滞留 `running`。系统 SHALL 通过多道防线保证：即使执行流被中断（客户端断连、异步任务取消、进程被硬杀），执行记录也不会永久卡在「执行中」。

#### Scenario: 流式执行被中断时补落终态
- **WHEN** 一次流式执行的生成器在产出中途被关闭或取消（如客户端断开 SSE 连接、承载任务被取消）
- **THEN** 系统 SHALL 在中断传播前把该执行补落为终态（killed）并清理其 pid 登记，不留 `running` 孤儿

#### Scenario: 运行期孤儿巡检
- **WHEN** 某执行记录仍为 `running`，但其最后一条日志距今静默时长已超过阈值（默认 30 分钟，且 ≥ 最长静默超时，避免误伤仍在持续产出的慢任务）
- **THEN** 后台巡检 SHALL 周期性（默认每 120 秒）将其补落终态——所属任务已 `done`/`reviewing` 的落 succeeded（保其成果）、否则落 killed——并尝试推进父任务状态；无需等待下次进程重启的启动回收
- **注**：巡检为幂等，只对仍 `running` 的记录动手（`WHERE status='running'` 条件更新），绝不覆盖已定终态；间隔与静默阈值可配（`orphan_sweep_interval_sec` / `orphan_sweep_idle_sec`）

### Requirement: 状态与日志监控

系统 SHALL 为每次执行结构化记录全过程，并以「历史运行列表 + 日志详情」两级呈现；对外文本 SHALL 统一脱敏。

#### Scenario: 执行历史列表
- **WHEN** 用户查看任务的执行日志区
- **THEN** 进行中的运行常显，历史运行折叠在「显示历史运行（N）」开关后、展开列出全部；每行含状态图标、命令缩略、相对时间（刚刚/N分钟前/N小时前/N天前）

#### Scenario: 图标看 Agent、hover 查详情
- **WHEN** 鼠标悬停某运行行的状态图标
- **THEN** 显示该运行的 Agent 与状态（管理员可点图标终止/重跑）；悬停整行时右侧出现「日志详情」入口

#### Scenario: 日志详情还原命令与结果
- **WHEN** 用户点击某次运行的「日志详情」
- **THEN** 系统返回结构化事件序列；工具事件展开同时呈现完整命令/参数与运行结果，工具结果行标注其工具名（如 Bash），每条含执行时间，顶部标注供应商与模型，可按类型过滤/排序/复制

#### Scenario: 实时命令可见
- **WHEN** 一次执行正在流式进行
- **THEN** 流式事件带上工具名与完整入参/输出，执行中的工具行可展开查看命令详情

#### Scenario: 敏感信息脱敏
- **WHEN** 日志详情或流式事件返回工具命令/输出/摘要
- **THEN** 系统先抹除密钥、token、私钥、带密码连接串等敏感信息再返回（服务端为主、前端兜底）

### Requirement: 平台操作命令（jian CLI）

Agent SHALL 通过 jian CLI 在平台上发言/建卡/改状态，且多行长内容 SHALL 能完整传入、不被命令行转义截断；发言/汇报/交付内容 SHALL 使用 Markdown 结构化排版，以便平台渲染出清晰的信息层次。

#### Scenario: 多行长发言
- **WHEN** Agent 需要发布多行/长正文（如自我介绍、报告）
- **THEN** 先写入文件再用 `jian comment --body-file <文件>`（或 `--stdin`），完整内容落库，不因命令行/批处理转义被截断成第一行

#### Scenario: 结构化排版
- **WHEN** Agent 产出汇报/交付/结论
- **THEN** 用 Markdown 结构组织——`##`/`###` 小标题分章节、`**粗体**` 标关键项/字段名、`-`/有序列表、必要时表格与反引号代码，**不使用 `━━━` 等装饰线或纯 emoji 行冒充标题**（那样渲染为扁平正文、无层次）

### Requirement: 执行安全

系统 SHALL 安全地调用执行器。

#### Scenario: 防注入与目录限定
- **WHEN** 启动 CLI 子进程
- **THEN** 一律列表传参（不 shell 拼接），工作目录 / --add-dir 限定在该项目的本地路径
