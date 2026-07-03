## 1. 后端地基

- [x] 1.1 `config.py`：pydantic Settings + `config.json` 读写；支持多供应商列表（providers）与默认供应商指定
- [x] 1.2 `database.py`：SQLite 连接 + 表结构基线（projects / agents / conversations / messages / workflows / workflow_runs）
- [x] 1.3 `main.py`：FastAPI 入口、CORS 显式白名单、注册路由、启动入口（host 默认 127.0.0.1，reload 由 env 控制）
- [x] 1.4 `requirements.txt`：fastapi / uvicorn / pydantic-settings / aiosqlite / httpx

## 2. 供应商配置接口

- [x] 2.1 `routes/settings.py`：GET 读取供应商列表与默认项（密钥脱敏返回）
- [x] 2.2 保存供应商配置（写 `config.json`）
- [x] 2.3 连通性测试接口：API 类型发一次最小请求；CLI 类型检测可执行文件存在与版本

## 3. 前端设置页

- [x] 3.1 `Settings.vue`：多供应商配置 Tab，新增 / 编辑 / 删除供应商
- [x] 3.2 供应商类型预置：claude-cli / codex-cli / api（api 下含 Deepseek / OpenAI / Anthropic / Ollama 预设 base_url）
- [x] 3.3 api_key 用 password 字段；测试按钮调用连通性接口
- [x] 3.4 指定「默认供应商」并持久化
- [x] 3.5 `api/index.js` + `router.js` + `App.vue` 导航接入

## 4. 启动与验证

- [x] 4.1 `start.ps1`：装依赖 + 起后端(8100) + 起前端(3100)
- [x] 4.2 验证：设置页可新增 Deepseek 供应商并测试连通成功
- [x] 4.3 验证：可新增 Claude Code CLI 供应商，测试检测到 `claude --version`
- [x] 4.4 验证：刷新页面配置保持；`config.json` 已被 git 忽略
