# auth-rbac

## Purpose

为内网开放提供登录认证与角色权限。管理员可写可执行，匿名/普通用户只读浏览，保护放开权限的 Agent 执行不被任意访客触发。

## Requirements

### Requirement: 管理员认证

系统 SHALL 提供登录，并在首次启动播种一个管理员账号。

#### Scenario: 播种管理员
- **WHEN** 系统启动且无管理员
- **THEN** 创建管理员账号（用户名由环境变量配置），密码以加盐哈希存储，绝不存明文

#### Scenario: 登录
- **WHEN** 用户以正确的管理员用户名密码登录
- **THEN** 系统签发 token 并以 httponly cookie 返回；密码校验使用常量时间比较

#### Scenario: 登出
- **WHEN** 管理员登出
- **THEN** 服务端 token 失效，cookie 清除

### Requirement: 匿名只读

系统 SHALL 允许未登录用户只读访问。

#### Scenario: 匿名浏览
- **WHEN** 未登录用户访问项目、数字人才库、Skills 的读取接口
- **THEN** 正常返回数据

#### Scenario: 匿名禁止写
- **WHEN** 未登录或非管理员用户调用任何写/执行接口（建项目、派发任务、配置 Agent/Skill、改设置等）
- **THEN** 系统返回 403

### Requirement: 角色驱动界面

前端 SHALL 按当前用户角色控制可见与可操作元素。

#### Scenario: 非管理员界面
- **WHEN** 当前为匿名/非管理员
- **THEN** 隐藏「设置」入口与所有写操作按钮（新建/编辑/删除/@分派/配置），仅保留只读浏览

#### Scenario: 管理员界面
- **WHEN** 当前为管理员
- **THEN** 显示设置入口与全部写操作能力
