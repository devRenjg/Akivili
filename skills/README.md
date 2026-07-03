# Skill 库目录

每个 `<skill-slug>.md` 是一个 Skill —— 一段能力说明 / 规范 / 操作要领（纯文本）。

## 约定

- frontmatter 至少含 `name`，建议含 `description`；`---` 之后是能力正文。
- Agent 可在项目里勾选启用若干 Skill；运行时这些 Skill 的正文会注入到该 Agent 的能力上下文。
- Skill 的启用按 Agent 身份（slug）跨项目共享。

可直接往本目录丢 `.md`，或在平台「Skills」页新建。
