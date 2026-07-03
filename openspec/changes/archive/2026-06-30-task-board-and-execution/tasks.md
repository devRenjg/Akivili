## 1. 数据模型

- [x] 1.1 database 增 `tasks`（project_id, title, description, status, assignee_slug, conversation_id, order_idx, 时间戳）
- [x] 1.2 增 `task_runs`（task_id, conversation_id, agent_slug, status, provider_id, pid, started_at, ended_at）
- [x] 1.3 增 `run_logs`（run_id, ts, channel, content）
- [x] 1.4 _migrate 兼容旧库（建表幂等即可，无需 ALTER）

## 2. 执行引擎 executor/

- [x] 2.1 `base.py`：ExecutorBackend 抽象 + 流式事件（text/tool/done/error）
- [x] 2.2 `claude_code.py`：claude -p --output-format stream-json --add-dir <项目路径> --append-system-prompt <...> --model <别名> --dangerously-skip-permissions；解析 stream-json
- [x] 2.3 `codex.py`：codex exec --json -m <model>，cwd=项目路径
- [x] 2.4 `api_llm.py`：httpx 流式（复用 Bearer 逻辑，纯对话）
- [x] 2.5 `runner.py`：组装系统提示（人格+记忆+Skills正文+会话历史+任务上下文），按 provider 选后端，记录 PID，to_thread 卸载阻塞，收工写记忆
- [x] 2.6 子进程列表传参、绝不 shell=True；保存 PID 供 kill；进程注册表（run_id→process）

## 3. 后端接口

- [x] 3.1 `routes/tasks.py`：创建/列表（按 status 分组）/详情/更新/状态流转/归档/删除
- [x] 3.2 创建任务时建关联 conversation
- [x] 3.3 `routes/runs.py`：POST /tasks/{id}/dispatch（@分派，SSE 流式执行）
- [x] 3.4 POST /runs/{id}/kill（终止子进程，状态置 killed）
- [x] 3.5 GET /runs/{id}/logs、GET /tasks/{id}/messages、GET /tasks/{id}/runs
- [x] 3.6 main.py 注册路由

## 4. 前端：看板

- [x] 4.1 `Workspace.vue`：5 列（规划中/进行中/验证中/已完成 + 归档区），任务卡片
- [x] 4.2 新建任务、改状态（下拉或拖拽）、归档
- [x] 4.3 卡片显示负责人、运行中⚙️标识
- [x] 4.4 项目内加「工作区」Tab/入口；router + api（tasksApi/runsApi）

## 5. 前端：任务对话终端

- [x] 5.1 `TaskThread.vue`：消息流 + @选择器（从团队选负责人）+ 输入框
- [x] 5.2 分派后 SSE 流式渲染 Agent 输出
- [x] 5.3 运行中显示「Kill」按钮；可展开日志面板
- [x] 5.4 历史消息加载（独立调用+记忆恢复，多轮靠历史回灌）

## 6. 验证（遵守测试安全规则：__test__ 项目 + 临时空目录 + 精确 id 清理）

- [x] 6.1 建任务、5 态流转 + 归档正常
- [x] 6.2 @ 一个配 Claude 的 Agent，在临时目录让它创建文件 → 流式输出可见 → 文件真被创建
- [x] 6.3 kill 运行中的 run → 进程终止、状态 killed
- [x] 6.4 日志可查；收工后记忆被更新；多轮追问能延续上下文
- [x] 6.5 更新 README（v0.6.0）、归档 change
