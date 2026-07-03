# Akivili Agent Operating Rules

## Codex CLI Policy

- Akivili is a trusted local-first Agent workbench. When Akivili invokes Codex CLI as an execution backend, it should run with full permissions by default and must not pause for per-command approval.
- The project default for `codex exec` is:
  - `--dangerously-bypass-approvals-and-sandbox`
  - `--skip-git-repo-check`
  - `--cd <project_dir>`
  - `--add-dir <project_dir>`
- This policy applies to Akivili-spawned Codex CLI runs inside the user-selected project workspace. It does not override the outer Codex/chat session approval system, which is controlled by the host environment.
- Keep all Agent file operations scoped to the active Akivili project directory unless the user explicitly instructs otherwise.
