# task-system

## ADDED Requirements

### Requirement: 富文本渲染

任务描述与对话消息 SHALL 按 Markdown 渲染，并对渲染结果消毒防 XSS。

#### Scenario: Markdown 展示
- **WHEN** 展示任务描述或一条消息正文
- **THEN** 按 Markdown 渲染标题、粗体、列表、表格、代码块、图片与可点击链接（含裸链接自动识别，外链新标签打开）

#### Scenario: 消毒
- **WHEN** 渲染 Agent/LLM 产出的内容
- **THEN** 经 DOMPurify 消毒，剔除脚本与事件处理器等 XSS 载荷后再显示

## MODIFIED Requirements

### Requirement: 子任务

任务 SHALL 可拆分子任务（带描述与优先级）并展示完成进度；子任务强制两级。看板与详情 SHALL 在父任务下展示子任务，子任务执行/重跑期间父任务处按有效状态显示「进行中」。

#### Scenario: 子任务进度
- **WHEN** 任务有若干子任务
- **THEN** 展示 done/total 进度；子任务可独立改状态

#### Scenario: 执行中有效状态
- **WHEN** 某子任务在 run_queue 里仍有 queued/running 的运行（含 done 后被重新触发）
- **THEN** 父任务的子任务列表处按「进行中」展示该子任务，而非其残留的旧状态

#### Scenario: 返回父任务
- **WHEN** 用户在子任务详情页点「返回」
- **THEN** 回到其父任务详情；顶层任务则回工作区
