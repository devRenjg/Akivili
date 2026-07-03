## 1. 后端：项目管理

- [x] 1.1 `projects.py`：项目 CRUD 数据访问（create/list/get/update/delete）
- [x] 1.2 `routes/projects.py`：POST 创建（校验 local_path 存在）、GET 列表、GET 详情、PUT 更新、DELETE 删除
- [x] 1.3 创建时 title 必填、local_path 必填且须为已存在目录，否则 400
- [x] 1.4 删除项目级联清理其 project_agents（外键 ON DELETE CASCADE 已建）

## 2. 后端：项目内 Agent 团队

- [x] 2.1 `routes/project_agents.py`：POST 从模版导入（template_id → 复制 name/emoji/color/body 为 persona，归属 project_id）
- [x] 2.2 GET 列出项目的 Agent 团队
- [x] 2.3 POST 自建 Agent（无 template_id，直接填 name/persona）
- [x] 2.4 PUT 改造（修改 persona / provider_id / enabled）、DELETE 移除
- [x] 2.5 在 main.py 注册路由

## 3. 后端：Agent 记忆基础设施

- [x] 3.1 `config.py` 增 `memory_dir`，默认 `C:\Code\JianAgency\memory`
- [x] 3.2 `memory.py`：read(slug) / write(slug, content) / append(slug, text)；路径用 slug 拼接并校验 resolve 后仍在 memory_dir 内（防穿越）
- [x] 3.3 read 不存在时返回空串（不报错），write 自动建目录
- [x] 3.4 `routes/memory.py`：GET 读取某 Agent 记忆、PUT 覆盖写、POST 追加
- [x] 3.5 startup 确保 memory_dir 存在；放一份 README.md 说明记忆约定（agent 开工读、收工写、记做事要领）

## 4. 前端：主页与项目详情

- [x] 4.1 重写 `Dashboard.vue`：项目卡片网格（标题/状态/路径/Agent 数）+ 新建项目对话框 + Agents 总览统计
- [x] 4.2 `ProjectDetail.vue`：项目信息 + 绑定文件夹 + Agent 团队区（从库导入弹窗 / 自建 / 改造 persona / 移除）
- [x] 4.3 Agent 团队卡片可打开「记忆」抽屉：查看并编辑该 Agent 的 memory/<slug>.md
- [x] 4.4 `api/index.js` 增 projectsApi / projectAgentsApi / memoryApi；`router.js` 增 /projects/:id；导航调整

## 5. 验证

- [x] 5.1 创建项目（绑定一个真实存在的本地文件夹）成功；绑定不存在路径报 400
- [x] 5.2 主页显示项目卡片与状态
- [x] 5.3 从库导入「项目负责人」「测试专员」到项目，团队列表出现
- [x] 5.4 改造某 Agent 的 persona 并保存，重新打开保持
- [x] 5.5 写入并读回某 Agent 的记忆文件；确认文件落在 memory/<slug>.md
- [x] 5.6 memory 路径穿越尝试（slug 含 ../）被拒绝
