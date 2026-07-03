## Why

要把 Akivili 开放给公司同事内网访问（`http://<your-lan-ip>:3100`）。但当前无登录、Agent 执行放开权限（能在主机改文件跑命令），直接暴露风险极高。需要一层轻量认证 + 角色权限：管理员可写可执行，匿名/其他用户只读浏览。

认证采用成熟模式：PBKDF2 加盐哈希、token + httponly cookie、require_admin 依赖、前端 provide('currentUser') + v-if 角色控制，精简为「单管理员 + 匿名只读」，密码比较用常量时间（hmac.compare_digest）。

## What Changes

- `database.py` 新增 `users` 表；startup 自动播种管理员（环境变量 AKIVILI_ADMIN_USER / AKIVILI_ADMIN_PASSWORD / role=admin），已存在则跳过
- 新增 `auth.py`：PBKDF2 哈希（hmac.compare_digest 常量时间校验）、token、`_user_from_token`、`require_admin`（非管理员 403）、`current_user`（含匿名）
- 新增 `routes/auth.py`：`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`
- 给所有写/执行端点加 `Depends(require_admin)`：任务派发 dispatch/kill、任务 CRUD/状态、项目 CRUD、团队增删改/leader、Agent 配模型/Skills、Skill 新建/重扫、设置保存/测试、记忆写入、模版重扫；GET 一律放行（只读）
- 前端：`api` 加 `withCredentials`；新增 authApi 与登录弹窗；右上角显示身份（管理员/访客）；`provide('currentUser')`，按 role 用 v-if 隐藏「设置」Tab 与所有写操作按钮（新建/编辑/删除/@分派/配置）
- 内网绑定：后端 host=0.0.0.0，前端 vite --host，CORS 白名单加 `http://<your-lan-ip>:3100`；start.ps1 更新

## Capabilities

### New Capabilities
- `auth-rbac`: 登录认证与基于角色的访问控制。管理员可执行全部写操作与 Agent 派发；匿名/普通用户只读浏览项目空间、数字人才库、Skills，看不到设置，不能安排任务、不能增改 Agent/Skill。

## Impact

- 后端：新增 `auth.py` / `routes/auth.py`；database 加 users 表 + 播种；各写端点加守卫；main.py 改 host/CORS
- 前端：登录态全局注入 + 角色驱动 UI；带 cookie
- 安全：密码 PBKDF2 加盐 + 常量时间校验、token httponly cookie；匿名只读挡住写面。**保留提醒**：管理员 @ Agent 仍是放开权限执行，管理员账号权限大，密码需妥善保管
