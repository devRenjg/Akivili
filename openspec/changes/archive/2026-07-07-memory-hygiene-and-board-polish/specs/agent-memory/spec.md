# agent-memory

## ADDED Requirements

### Requirement: 近期动态只存净交付、滚动少量

系统 SHALL 把每次执行的「近期动态」记入记忆的受管段落，只保留该 Agent 本轮经平台命令（jian comment/subtask）落库的**净交付**，不记录执行过程碎语（命令用法、环境变量、终端编码提示等）；该段落 SHALL 滚动保留最新少量条目，避免无限膨胀。

#### Scenario: 只记净交付
- **WHEN** 一次执行结束、该 Agent 本轮有经 jian 落库的净交付
- **THEN** 系统把该净交付（截断）记入「近期动态」段落，不写入流式 stdout 里的过程碎语

#### Scenario: 无净交付不记
- **WHEN** 一次执行本轮没有任何经 jian 落库的净交付
- **THEN** 系统不向「近期动态」写入内容（不拿流式 stdout 兜底）

#### Scenario: 滚动上限
- **WHEN** 「近期动态」条目超过保留上限
- **THEN** 系统只保留最新的若干条，更早的滚动丢弃

### Requirement: 注入按相关性精选

系统 SHALL 在把记忆注入 Agent 系统提示时，按**与当前任务的关键词相关性**从 Know-how 段落精选最相关的 top-N 条注入（文件内 Know-how 全量保留、不删除），并剥离条目内部的归属标记；条目数不超过上限或无有效任务关键词时全量注入。

#### Scenario: 相关性精选
- **WHEN** 某 Agent 的 Know-how 条目多于注入上限、且当前任务有可提取的关键词
- **THEN** 系统按关键词重叠度打分，只注入最相关的 top-N 条，其余保留在文件中但本轮不注入

#### Scenario: 条目少时全给
- **WHEN** Know-how 条目数不超过注入上限，或当前任务无可提取关键词
- **THEN** 系统注入全部 Know-how 条目

#### Scenario: 剥离内部标记
- **WHEN** 注入 Know-how 与近期动态到系统提示
- **THEN** 系统剥离条目内部的归属标记（`<!-- akivili:task:ID -->`），不将其暴露给模型
