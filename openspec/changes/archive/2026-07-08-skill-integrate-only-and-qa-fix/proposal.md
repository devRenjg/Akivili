## Why

1. **知识库类 Skill 需要「只在平台内被 Agent 调用、不允许被整包下载带走」**：接入直播营收知识库 `bilisc-kb-live-revenue`（目录型能力包，含查询 CLI）时，希望它能被 Agent 勾选启用、运行时注入，但不对外提供下载——避免知识库内容被打包带出平台。原 Skill 库默认所有 Skill 都可下载，缺少「仅集成」这一档。

2. **QA 套件对 v0.16.2「新项目空团队」失配、且掩盖了一个真 bug**：v0.16.2 有意移除新项目自动种子 Leader，但 QA 套件仍断言「自动种子 Team Leader」、且后续协同块无保护地取 `leader["slug"]`——`leader=None` 时抛 `TypeError` 直接中断整个套件。修复过程中进一步定位到一个长期被误记为「无关漂移」的真 bug：假执行器 `fake_execute_dispatch` 签名过时（缺 `persist_user_msg`/`user_name`），被 `collab._run_one` 以关键字参数调用时抛 `TypeError` 被吞、导致协同 run 全 failed、`order=[]`，横跨 5 份历史报告一直为红。

## What Changes

- **Skill 支持「仅集成、不下载」标记**：Skill frontmatter 的 `downloadable: false`（或 `no`/`0`/`off`）表示「仅供 Agent 集成、不对外提供下载」。后端下载接口对该类 Skill 硬拦截返回 403（防绕过前端直接调接口）；前端隐藏下载按钮、改显「🔒 仅集成」标签。缺省或其它值视为允许下载。
- **目录型 Skill（能力包）**：`skills_dir` 下 `<slug>/SKILL.md` 结构（可含 `scripts`/`references` 子目录）识别为目录型 Skill（`is_dir=1`）。允许下载的能力包打包为 zip 下载；禁止下载的只在详情页展示 `SKILL.md` 正文。
- **接入 `bilisc-kb-live-revenue`**：直播营收知识库作为目录型、`downloadable: false` 的 Skill 入库，供 Agent 集成。
- **QA 套件同步新契约并修桩**：断言由「新项目自动种子 Team Leader」反转为「新项目从空团队开始」，新增「显式导入负责人并设为 Team Leader」一项；`leader_slug` 加兜底；假执行器 `fake_execute_dispatch` 补齐 `persist_user_msg`/`user_name` 参数，与真实 `runner.execute_dispatch` 签名对齐。

## Capabilities

### Modified Capabilities
- `agent-skills`：新增「仅集成、不下载」标记与目录型能力包（含下载接口的服务端拦截）。

## Impact

- 后端：`skills.py`（`scan_from_disk` 扫目录型 Skill、`_parse_downloadable` 解析标记）、`routes/skills.py`（`download_skill` 对 `downloadable=0` 返回 403、目录型打包 zip）；`skills` 表已含 `is_dir`/`downloadable` 列。
- 前端：`Skills.vue`（按 `downloadable` 隐藏下载按钮、显「🔒 仅集成」；目录型显「能力包」）。build 后生效。
- 数据：`bilisc-kb-live-revenue` 入库（`is_dir=1, downloadable=0`），无迁移。
- 测试：`TestReport/run_qa_suite.py`（断言反转 + 新增设 leader 项 + 修桩签名）。QA 30→31 项、31/31 全绿。产品代码在 QA 修复中零改动。
- 验证：API 实测列表返回该 Skill、`/download` 返 403；`openspec validate --specs`。
