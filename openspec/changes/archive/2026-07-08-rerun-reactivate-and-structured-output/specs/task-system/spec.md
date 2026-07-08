# task-system

## MODIFIED Requirements

### Requirement: 富文本渲染

任务描述与对话消息 SHALL 按 Markdown 渲染，并对渲染结果消毒防 XSS；渲染 SHALL 呈现清晰的信息层次（标题与正文、关键项与普通文本有明显主次区分）。

#### Scenario: Markdown 展示
- **WHEN** 展示任务描述或一条消息正文
- **THEN** 按 Markdown 渲染标题、粗体、列表、表格、代码块、图片与可点击链接（含裸链接自动识别，外链新标签打开）

#### Scenario: 层次与主次
- **WHEN** 内容含多级标题（`##`/`###`）与加粗关键项
- **THEN** 标题以递减的字号/字重/颜色呈现、章节标题带分隔感，加粗项作为字段名/关键项比正文更突出，使读者一眼分辨主次

#### Scenario: 消毒
- **WHEN** 渲染 Agent/LLM 产出的内容
- **THEN** 经 DOMPurify 消毒，剔除脚本与事件处理器等 XSS 载荷后再显示
