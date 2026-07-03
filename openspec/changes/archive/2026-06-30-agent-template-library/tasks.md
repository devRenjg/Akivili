## 1. 后端：模版扫描与解析

- [x] 1.1 `config.py` 增 `agent_library_dir`，默认 Agent 库目录
- [x] 1.2 `agents.py`：递归扫描库目录下 `.md`，解析 frontmatter（name/description/emoji/color）+ 正文 body
- [x] 1.3 排除非角色目录：examples / integrations / strategy / assets / scripts / .git
- [x] 1.4 slug 取自相对路径（去扩展名、目录分隔转 `-`），division 取顶层目录名
- [x] 1.5 幂等 upsert 到 `agent_templates`（按 slug 唯一，重扫更新 name/desc/body 等）
- [x] 1.6 无 frontmatter 或解析失败的文件跳过并计数，不中断整体扫描

## 2. 后端：库接口

- [x] 2.1 `routes/agents.py`：GET 模版列表（query 支持 division、关键词 q 过滤），返回精简字段（不含 body）
- [x] 2.2 GET 模版详情（按 id 或 slug，含 body 人格正文）
- [x] 2.3 GET 分类列表（division + 各自数量）
- [x] 2.4 POST 重新扫描（触发 agents.py 扫描，返回新增/更新/跳过计数）
- [x] 2.5 在 `main.py` 注册路由；startup 时若库为空则自动扫描一次

## 3. 前端：Agent 库页

- [x] 3.1 `Agents.vue`：顶部搜索框 + 分类筛选（el-select 或 tabs）
- [x] 3.2 卡片网格：emoji + 名称 + 描述截断 + 分类标签
- [x] 3.3 点击卡片打开详情抽屉/弹窗：展示完整描述与人格正文（body）
- [x] 3.4 「重新扫描」按钮，调用扫描接口并刷新
- [x] 3.5 `api/index.js` 增 agentsApi；`router.js` + `App.vue` 导航增「Agent 库」

## 4. 验证

- [x] 4.1 启动后自动扫描，库中出现 300+ 个 Agent（排除目录后）
- [x] 4.2 能搜到「项目负责人」「测试专员」并查看完整人格正文
- [x] 4.3 按分类筛选（如 engineering / testing）数量正确
- [x] 4.4 重新扫描幂等：重复扫描不产生重复记录
