## 1. 执行日志区重做为历史运行折叠列表

- [x] 1.1 新增 RunRow 组件：状态图标(hover 显示 Agent)+命令缩略(截断)+相对时间/hover 日志详情
- [x] 1.2 TaskDetail：进行中运行常显 + 历史运行折叠「显示历史运行（N）」展开全部
- [x] 1.3 相对时间 relTime：刚刚/N分钟前/N小时前/N天前
- [x] 1.4 后端 /tasks/{id}/runs 每条附 summary（首条工具命令缩略、脱敏）

## 2. 日志详情增强

- [x] 2.1 工具事件展开同时显示「命令/参数 + 运行结果」（补 codex 输出丢失）
- [x] 2.2 Claude tool_result 按 tool_use_id 跨行回填工具名（标签显示 Bash）
- [x] 2.3 每条右侧显示执行北京时间（去序号）
- [x] 2.4 顶部显示供应商名·模型（去 provider_id）
- [x] 2.5 筛选去空名、助手发言命名「发言」并带绿色点；行内绿色「发言」标签

## 3. Markdown 富文本

- [x] 3.1 新增 MarkdownView（marked GFM + DOMPurify 消毒）+ utils/redact.js 兜底
- [x] 3.2 任务描述、消息气泡按 Markdown 渲染；图片、可点击链接（含裸链接）；正文统一 14px

## 4. 人工验收闭环修复

- [x] 4.1 子任务 jian status done 不降级；reviewing 归一为 done
- [x] 4.2 _has_pending_run 增 exclude_run_id，修父任务收尾竞态
- [x] 4.3 on_execution_complete 透传 exclude_run_id（collab/agent_cli 传自身 run 行）
- [x] 4.4 补回 _advance_and_summarize_parent：全子完成→父进验证中+唤醒负责人汇总
- [x] 4.5 子任务执行/重跑时父任务处按有效状态显示「进行中」
- [x] 4.6 手动修复线上任务53（负责人补做汇总）、54/56/60（被截断内容补回）

## 5. jian comment 多行修复

- [x] 5.1 comment 加 --body-file/--stdin，绕开命令行/`.bat` %* 截断
- [x] 5.2 系统提示要求长内容用 --body-file

## 6. 交互与展示细节

- [x] 6.1 项目卡片/概览展示 git_url（不暴露本地路径）；projects 加 git_url 列
- [x] 6.2 执行状态改 Element Plus 图标（停止/勾/叉/减号）
- [x] 6.3 返回按钮改「返回」，子任务回父任务
- [x] 6.4 用户消息/活动按用户名显示同名头像；AgentAvatar 加载失败回退 emoji
- [x] 6.5 记忆 GET 加 require_admin（仅管理员可见）
- [x] 6.6 后端默认热加载 JIANAGENCY_RELOAD=1

## 7. 验证与文档

- [x] 7.1 前端 vite build 通过；后端各模块语法/import 通过
- [x] 7.2 更新 README（能力概览/版本 v0.14.0）
- [x] 7.3 更新 specs：agent-execution / task-system / agent-collaboration
- [x] 7.4 归档本 change
