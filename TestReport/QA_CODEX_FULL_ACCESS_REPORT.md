# Codex CLI Full Access Acceptance Report

## Request

Codex CLI used by Akivili should be fully authorized by default, should not wait for per-command approval, and should remember this policy for future sessions.

## Implementation

Updated `backend/executor/codex.py` so Akivili-spawned Codex CLI runs now default to:

```text
codex exec --json \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  --cd <project_dir> \
  --add-dir <project_dir> \
  [prompt]
```

This matches the user's trusted local-first workflow and avoids Windows sandbox failures such as `CreateProcessWithLogonW failed: 1385`.

## Persistent Project Rule

Added `AGENTS.md` at the repository root. Future Codex/Agent sessions that read project instructions should follow the Codex CLI full-access policy by default.

Note: this project rule applies to Akivili-spawned Codex CLI runs. It cannot override the outer chat/session approval system controlled by the host environment.

## Verification

### Static checks

- `py -3.12 -m compileall backend`: PASS
- `py -3.12 -m py_compile TestReport\run_codex_cli_smoke.py`: PASS

### CodexBackend smoke

Command:

```powershell
py -3.12 TestReport\run_codex_cli_smoke.py
```

Result: PASS

Latest report:

- `TestReport/codex_cli_smoke_20260701-160205.md`
- `TestReport/codex_cli_smoke_20260701-160205.json`

Evidence:

- Workspace: `C:\tmp\akivili-codex-smoke-wonx89a2`
- File created: `CODEX_CLI_OK.txt`
- Expected content: `codex-cli-ok`

### Regression suite

Command:

```powershell
py -3.12 TestReport\run_qa_suite.py --keep
```

Result: PASS, 30/30.

Latest report:

- `TestReport/qa_results_20260701-160214.md`
- `TestReport/qa_results_20260701-160214.json`

## Acceptance

Codex CLI full-access execution is accepted for Akivili backend usage.
