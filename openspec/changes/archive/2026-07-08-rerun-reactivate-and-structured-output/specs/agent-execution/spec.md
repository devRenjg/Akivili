# agent-execution

## MODIFIED Requirements

### Requirement: 平台操作命令（jian CLI）

Agent SHALL 通过 jian CLI 在平台上发言/建卡/改状态，且多行长内容 SHALL 能完整传入、不被命令行转义截断；发言/汇报/交付内容 SHALL 使用 Markdown 结构化排版，以便平台渲染出清晰的信息层次。

#### Scenario: 多行长发言
- **WHEN** Agent 需要发布多行/长正文（如自我介绍、报告）
- **THEN** 先写入文件再用 `jian comment --body-file <文件>`（或 `--stdin`），完整内容落库，不因命令行/批处理转义被截断成第一行

#### Scenario: 结构化排版
- **WHEN** Agent 产出汇报/交付/结论
- **THEN** 用 Markdown 结构组织——`##`/`###` 小标题分章节、`**粗体**` 标关键项/字段名、`-`/有序列表、必要时表格与反引号代码，**不使用 `━━━` 等装饰线或纯 emoji 行冒充标题**（那样渲染为扁平正文、无层次）
