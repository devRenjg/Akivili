# 安全策略 Security Policy

## 报告漏洞 Reporting a Vulnerability

如果你发现了安全漏洞，请**不要**公开提交 Issue。请通过 GitHub 的私有漏洞报告通道
（仓库 **Security → Report a vulnerability**）提交，我们会尽快跟进。

请在报告中尽量包含：复现步骤、影响范围、相关版本/提交，以及可能的修复建议。

## 部署安全须知（务必阅读）

Akivili 是一个**本地优先**的多 Agent 工作平台，其设计定位是**在可信内网/本机**运行。
它有几个高风险特性，公开部署前必须理解：

- **Agent 以放开权限执行**：CLI 执行后端（Claude Code / Codex）默认跳过授权与沙箱确认，
  Agent 能在宿主机**读写文件、执行任意命令**。这是为了让协同不因交互提示卡死而做的取舍。
  **切勿将本服务直接暴露到公网。**
- **单管理员 + 匿名只读模型**：管理员可写、可触发 Agent 执行；匿名用户只读。任何能以管理员身份
  登录的人，都能借 Agent 在宿主机上执行命令。
- **绑定 `0.0.0.0`**：默认监听所有网卡，仅应在可信内网使用；对公网需有防火墙/反向代理保护。

## 配置与密钥

以下均**不入版本库**，通过本地配置或环境变量提供：

- `backend/config.json`：供应商 API Key 等（已在 `.gitignore`）。
- 管理员账号：环境变量 `AKIVILI_ADMIN_USER` / `AKIVILI_ADMIN_PASSWORD`
  （缺省占位 `admin` / `changeme`，**部署务必修改**）。
- 目录/网络：`AKIVILI_AGENT_LIBRARY_DIR`、`AKIVILI_MEMORY_DIR`、`AKIVILI_SKILLS_DIR`、
  `AKIVILI_EXTRA_ORIGINS`（CORS 白名单）、`AKIVILI_NO_PROXY_EXTRA` 等。

密码以 PBKDF2-HMAC-SHA256 加盐哈希存储，绝不存明文；校验用常量时间比较。

## 支持范围

本项目为开源示例/工具，按 [MIT License](./LICENSE) “按现状提供、不含任何担保”。
安全修复仅在 `master` 主线跟进。
