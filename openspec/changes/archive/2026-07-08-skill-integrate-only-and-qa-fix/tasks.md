## 1. Skill「仅集成、不下载」+ 目录型能力包

- [x] 1.1 `skills.py::scan_from_disk` 扫描目录型 Skill（`<slug>/SKILL.md`，`is_dir=1`，source_path 指向目录）
- [x] 1.2 `skills.py::_parse_downloadable` 解析 frontmatter `downloadable`（false/no/0/off→0，缺省/其它→1）
- [x] 1.3 `routes/skills.py::download_skill` 对 `downloadable=0` 硬拦截返回 403（防绕过前端直连）
- [x] 1.4 允许下载的目录型能力包打包为 zip；单文件型下 `.md`
- [x] 1.5 `Skills.vue` 按 `downloadable !== 0` 隐藏下载按钮、显「🔒 仅集成」；目录型显「📦 能力包」

## 2. 接入直播营收知识库

- [x] 2.1 `bilisc-kb-live-revenue`（目录型：`SKILL.md` + `scripts/revenue-kb-api`）标 `downloadable: false`
- [x] 2.2 重扫入库；API 实测列表返回、`/download` 返 403

## 3. QA 套件回归修复

- [x] 3.1 断言「新项目自动种子 Team Leader」反转为「新项目从空团队开始（无负责人）」
- [x] 3.2 新增「显式导入 specialized-project-owner 并 PUT leader 设为 Team Leader」，拿 leader_slug 供协同块用
- [x] 3.3 `leader_slug` 加兜底，不再无保护取 `leader["slug"]`
- [x] 3.4 假执行器 `fake_execute_dispatch` 补 `persist_user_msg=True, user_name=""`，对齐真实签名

## 4. 验证与文档

- [x] 4.1 `run_qa_suite.py` 31/31 全绿（原 TypeError 硬崩 → 修 leader；原 order=[] 2 项 → 修桩后转绿）
- [x] 4.2 README 升 v0.16.3；同步 `agent-skills` spec；`openspec validate --specs`
