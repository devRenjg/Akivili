# OpenSpec — Akivili 规格驱动开发

本目录用 OpenSpec 管理功能迭代与需求澄清。

## 结构

- `specs/<capability>/spec.md` — **已实现能力**。每个能力一份，含 Purpose 与 Requirement / Scenario。
- `changes/<change-id>/` — **待实现提案**。变更落地、验证通过后归档到 `changes/archive/`。
  - `proposal.md` — Why / What Changes / Capabilities / Impact
  - `tasks.md` — 实施任务清单（`- [ ]` / `- [x]` 跟踪进度）
  - `design.md` — 设计与技术方案（可选）
  - `specs/<capability>/spec.md` — 该变更涉及的能力规格增量
  - `.openspec.yaml` — 元数据（schema、created）

## 工作流

1. 新需求 → 在 `changes/` 下新建 change，写 `proposal.md` + `tasks.md`
2. 实施中 → 勾选 `tasks.md` 的复选框
3. 完成并验证 → 把该 change 的能力规格固化进 `specs/`，change 目录移入 `changes/archive/`

## 命名约定

- change-id 格式：`<YYYY-MM-DD>-<kebab-case-简述>`，如 `2026-06-30-foundation-config-settings`
- capability 名用 kebab-case，如 `llm-provider-config`、`agent-library`、`agent-chat`
