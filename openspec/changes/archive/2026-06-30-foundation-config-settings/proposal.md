## Why

JianAgency 需要一个稳固的后端地基，才能在其上叠加 Agent 库、项目管理、对话与工作流。地基的第一要件，是**大模型 / CLI 多供应商配置**——这是整个执行引擎的输入。参照 Qlipoth 的 `config.py` + `config.json` + 设置页模式，但 JianAgency 的配置面更宽：不仅是 API（Deepseek/OpenAI/Anthropic），还包括本地 CLI 执行器（Claude Code、Codex）。用户需要在设置页配置多个供应商，并指定 Agent 默认用哪个。

## What Changes

- 后端新增 `config.py`：pydantic Settings + `config.json` 持久化，支持**多供应商列表**（每条含类型、api_key、base_url、model、api_format）
- 后端新增 `database.py`：SQLite 初始化与表结构基线（projects / agents / conversations / messages / workflows）
- 后端新增 `main.py`：FastAPI 应用入口、CORS（显式白名单，不用 wildcard+credentials）、路由注册
- 后端新增 `routes/settings.py`：供应商配置的读取 / 保存 / 连通性测试接口
- 前端新增设置页 `Settings.vue`：多供应商配置 Tab（仿 Qlipoth），预置 Claude Code / Codex / Deepseek / OpenAI / Anthropic / Ollama 选项
- 新增 `start.ps1`：一键安装依赖并启动前后端（端口 8100 / 3100）

## Capabilities

### New Capabilities
- `llm-provider-config`: 用户在设置页配置多个大模型 / CLI 供应商（类型、密钥、base_url、模型、API 格式），持久化到 `config.json`，并可指定 Agent 默认供应商；配置实时生效，无需改代码。

## Impact

- 后端：新增 `config.py` / `database.py` / `main.py` / `routes/settings.py`
- 前端：新增 `Settings.vue` 与对应路由、API 封装
- 数据：新增 `config.json`（git 忽略，含密钥）；SQLite 建库
- 安全：API key 仅存本地 `config.json` 并 git 忽略；CLI 执行器后续调用一律列表传参；CORS 显式白名单
