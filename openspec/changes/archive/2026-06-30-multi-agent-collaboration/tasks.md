## 1. 数据模型

- [x] 1.1 新增 `run_queue` 表（task_id, agent_slug, trigger, is_leader, prompt, status, created_at）

## 2. 后端：协同引擎 collab.py

- [x] 2.1 enqueue_run(task_id, agent_slug, prompt, trigger, is_leader) + pending 去重
- [x] 2.2 asyncio 后台循环：轮询 queued → 领取(标 running) → 跑 → 标 done/failed（串行单并发）
- [x] 2.3 后台执行封装：复用 runner.execute_dispatch 消费到底，落库消息/活动/run
- [x] 2.4 @mention 解析：从 Agent 发言提取 @成员名（匹配项目成员），逐个 enqueue_run
- [x] 2.5 Leader 自触发守卫（最近 run is_leader 则不因自身发言唤醒自己）
- [x] 2.6 协同深度上限（单任务累计 run 数上限，超则停并记活动）
- [x] 2.7 main.py 启动时拉起后台循环

## 3. 后端：Leader 协议与花名册

- [x] 3.1 TEAM_LEADER_PROTOCOL 常量（中文协作协议）
- [x] 3.2 build_roster(project_id)：成员名/角色/技能/@语法 花名册
- [x] 3.3 runner.build_context：Leader（is_leader）注入 协议+花名册

## 4. 触发入口

- [x] 4.1 auto-dispatch 改：拖到「进行中」优先唤醒 Leader（有 leader 时），否则描述首个@
- [x] 4.2 新增 POST /tasks/{id}/collaborate：显式唤醒 Leader 启动协同
- [x] 4.3 对话 @项目负责人 走同一入队通道

## 5. 前端

- [x] 5.1 任务详情加「▶ 启动协同」按钮（唤醒 Leader）
- [x] 5.2 执行日志区/活动时间线/对话区呈现多 Agent 协同（已有轮询，确认刷新）
- [x] 5.3 collaborateApi

## 6. 验证（无头 Chrome + 真实 Claude + __test__ 项目临时目录）

- [x] 6.1 队列串行：入队两个 run 依次执行不并发
- [x] 6.2 启动协同：Leader 读任务→@某成员→成员执行→活动时间线出现委派与完成
- [x] 6.3 @mention 解析正确触发对应成员；pending 去重生效
- [x] 6.4 Leader 自触发守卫、深度上限生效（不死循环）
- [x] 6.5 UI 呈现协同全过程；清理测试数据
- [x] 6.6 更新 README、归档 change
