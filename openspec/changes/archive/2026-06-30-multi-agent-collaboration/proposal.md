## Why

单 Agent 执行已跑通，但真实工作往往需要一个团队协作：负责人（Team Leader）读懂任务后拆解、按成员技能分派、成员干完回报、负责人汇总收尾。引入 Team Leader 调度机制，为 Akivili 加入**事件驱动的多 Agent 协同**：调度智能外包给 LLM（注入协作协议+团队花名册），@mention 作为唯一的 Agent 间通信原语，配合任务队列与多层防死循环，让一个任务能被整个团队协作完成。

## What Changes

- 新增 `run_queue` 表：待执行队列（task_id/agent_slug/trigger/is_leader/prompt/status）
- 新增 `collab.py`：asyncio 后台循环（串行单并发）轮询队列 → 领取 → 后台跑 CLI → 落库消息/活动 → 解析发言里的 @mention 再入队
- 复用现有 `project_agents.is_leader`（Team Leader）与 `agent_skills`（成员技能）；不新建 squad 表——项目团队即 squad
- 注入 `TEAM_LEADER_PROTOCOL`（协作协议）+ 动态花名册到 Leader 系统提示
- @mention 解析：Agent 发言里 `@成员名` → 触发该成员入队执行
- 防死循环：协议教育 + Leader 自触发守卫 + pending 去重 + 协同深度上限
- 触发入口：任务拖到「进行中」唤醒 Leader / 对话 @项目负责人 / 详情「启动协同」按钮
- 前端：执行日志区与活动时间线呈现多 Agent 协同全过程；加「启动协同」入口

## Capabilities

### New Capabilities
- `agent-collaboration`: 多 Agent 事件驱动协同。任务可由 Team Leader 统筹——Leader 读任务、按成员技能 @mention 委派、成员执行后回报、Leader 汇总收尾；@mention 触发执行，任务队列串行调度，多层机制防死循环。

## Impact

- 后端：新增 `run_queue` 表、`collab.py`（后台循环+队列+@解析+防循环）、Leader 协议与花名册；runner 复用；main.py 启动后台循环
- 前端：任务详情加「启动协同」；协同过程经活动时间线/执行日志区/对话区呈现
- 数据：run_queue 开始写入；复用 is_leader / agent_skills / activities / task_runs
- 安全/成本：协同串行单并发；深度上限防失控；放开权限执行不变（仅可信内网）
