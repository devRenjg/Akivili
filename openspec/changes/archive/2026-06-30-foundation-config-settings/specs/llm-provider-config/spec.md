# llm-provider-config

## Purpose

让用户在设置页集中配置多个大模型 / CLI 执行器供应商，作为 JianAgency 执行引擎的统一输入。配置持久化到本地 `config.json`，实时生效，无需改代码。

## Requirements

### Requirement: 多供应商配置

用户 SHALL 能配置一个或多个供应商，每个供应商包含：类型（`claude-cli` / `codex-cli` / `api`）、名称、以及类型相关字段。

#### Scenario: 新增 API 供应商
- **WHEN** 用户在设置页新增一个 `api` 类型供应商，填写 api_key、base_url、model、api_format（openai / anthropic）
- **THEN** 系统保存该供应商到 `config.json`，下次启动仍在

#### Scenario: 新增 CLI 供应商
- **WHEN** 用户新增 `claude-cli` 或 `codex-cli` 类型供应商
- **THEN** 系统记录该供应商，并可检测对应 CLI 可执行文件是否可用

### Requirement: 默认供应商

用户 SHALL 能指定一个默认供应商，Agent 运行时未显式指定时使用它。

#### Scenario: 设置默认供应商
- **WHEN** 用户将某供应商标记为默认
- **THEN** 系统持久化该选择，并在读取配置时返回默认项标识

### Requirement: 连通性测试

系统 SHALL 提供供应商连通性测试。

#### Scenario: 测试 API 供应商
- **WHEN** 用户点击 API 供应商的测试按钮
- **THEN** 系统发起一次最小请求，返回成功或带原因的失败

#### Scenario: 测试 CLI 供应商
- **WHEN** 用户点击 CLI 供应商的测试按钮
- **THEN** 系统检测可执行文件存在并返回其版本，或返回未找到

### Requirement: 密钥安全

系统 SHALL 保护 api_key。

#### Scenario: 密钥脱敏与忽略
- **WHEN** 前端读取供应商列表
- **THEN** api_key 以脱敏形式返回；`config.json` 被 git 忽略，密钥不入库不入仓
