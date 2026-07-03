## 1. 后端：认证

- [x] 1.1 `database.py` 增 `users` 表（id, username, password_hash, password_salt, role, token, last_seen）
- [x] 1.2 startup 播种管理员：<管理员账号，由环境变量配置> / admin（存在则跳过）
- [x] 1.3 `auth.py`：_hash_password(PBKDF2 加盐)、_verify_password(hmac.compare_digest)、_user_from_token、require_admin、current_user(含匿名 None)
- [x] 1.4 `routes/auth.py`：login（校验→生成 token→set httponly cookie）、logout（清 token）、me（返回当前用户或 null）

## 2. 后端：写操作守卫

- [x] 2.1 settings：保存/测试 加 require_admin
- [x] 2.2 agents：rescan 加 require_admin
- [x] 2.3 projects：create/update/delete 加 require_admin
- [x] 2.4 project_agents：import/create/update/delete/leader 加 require_admin
- [x] 2.5 agent_config：set model/skills 加 require_admin
- [x] 2.6 skills：create/rescan 加 require_admin
- [x] 2.7 memory：write/append 加 require_admin
- [x] 2.8 tasks：create/update/status/delete 加 require_admin
- [x] 2.9 runs：dispatch/kill 加 require_admin
- [x] 2.10 GET 一律不加守卫（匿名只读）；main.py 注册 auth 路由

## 3. 前端：登录与角色 UI

- [x] 3.1 `api/index.js`：axios withCredentials: true；authApi（login/logout/me）
- [x] 3.2 App.vue：启动拉 /auth/me；provide('currentUser')；右上角身份 + 登录/退出
- [x] 3.3 登录弹窗组件
- [x] 3.4 设置 Tab 仅 admin 可见（v-if）
- [x] 3.5 各页写操作按钮（新建/编辑/删除/@分派/配置/移动/设负责人/导入/重扫）仅 admin 可见
- [x] 3.6 TaskThread 发送/Kill 仅 admin；非 admin 只读看消息

## 4. 内网绑定

- [x] 4.1 main.py：host=0.0.0.0；CORS 加 http://<your-lan-ip>:3100
- [x] 4.2 vite.config.js / start.ps1：前端 --host 暴露 0.0.0.0
- [x] 4.3 README 标注访问地址与安全提醒

## 5. 验证（遵守测试安全规则）

- [x] 5.1 播种管理员存在；<管理员账号，由环境变量配置> 能登录拿到 cookie
- [x] 5.2 管理员可写（建测试项目成功）；登出后同操作 403
- [x] 5.3 匿名 GET 项目/Agents/Skills 正常返回
- [x] 5.4 匿名 POST 写操作被 403 拦截
- [x] 5.5 前端非 admin 看不到设置 Tab 与写按钮；build 成功
- [x] 5.6 清理测试数据；更新 README、归档 change
