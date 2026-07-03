# Akivili Fix Acceptance Report

## Scope

验收研发修复后的整体质量，重点覆盖：

- CLI 协同上下文传递修复
- `/api/*` unknown route 404 修复
- memory slug `..` 拒绝修复
- Claude CLI 真实多 Agent 协同
- Codex CLI 执行能力 smoke

## Artifacts

- Full isolated report: `TestReport/qa_results_20260701-141303.md`
- Full Claude live report: `TestReport/qa_results_20260701-141422.md`
- Codex smoke failed report: `TestReport/codex_cli_smoke_20260701-141933.md`
- Codex direct bypass evidence: command output in session, temp dir `C:\tmp\akivili-codex-bypass-692e13818b7d41e98156474d45c6880d`

## Results

### Static / Build

- Backend compileall: PASS
- QA scripts py_compile: PASS
- Frontend `npm run build`: PASS
  - Existing warnings only: bundle chunk size and Rollup pure annotation warnings.
- `claude --version`: PASS, `2.1.170 (Claude Code)`
- `codex --version`: PASS, `codex-cli 0.142.5`

### Isolated Functional / Security Suite

Command:

```powershell
py -3.12 TestReport\run_qa_suite.py --keep
```

Result: PASS, 30/30.

Confirmed fixed:

- Memory slug containing `..` is rejected.
- Unknown `/api/*` returns JSON 404 instead of SPA HTML 200.
- Deterministic collaboration queue, mention parsing, dedupe and depth limit remain passing.

### Claude CLI Live Multi-Agent Collaboration

Command:

```powershell
py -3.12 TestReport\run_qa_suite.py --live --keep
```

Result: PASS, 38/38.

Key metrics:

- Live collaboration score: 100/100
- Live collaboration order: `specialized-project-owner -> qa-backend-developer -> qa-tester -> qa-backend-developer`
- Live elapsed: 55.4s
- Task list p95: 3.49ms
- Deterministic queue 3 rounds: 142.12ms

Evidence:

- Leader selected backend developer correctly.
- Backend developer created `QA_COLLAB_RESULT.md`.
- Tester was triggered and validated.
- Final workspace file content:

```text
Akivili live collaboration QA
owner=backend
verified=pending
```

Conclusion: Claude CLI backed multi-Agent collaboration is accepted.

## Remaining Finding

### CodexBackend write execution does not pass with current backend parameters

Severity: P1 for Codex provider readiness. Not blocking Claude CLI collaboration acceptance, but blocks claiming Codex CLI provider is fully usable for file-writing Agent tasks.

Evidence:

1. Platform `CodexBackend` smoke failed:
   - Report: `TestReport/codex_cli_smoke_20260701-141933.md`
   - Codex returned text but did not create `CODEX_CLI_OK.txt`.

2. Direct Codex CLI with `--sandbox workspace-write` also failed in this Windows environment:
   - Error: `windows sandbox: CreateProcessWithLogonW failed: 1385`
   - File was not created.

3. Direct Codex CLI with `--dangerously-bypass-approvals-and-sandbox` succeeded:
   - File `CODEX_BYPASS_OK.txt` created.
   - Content: `codex-bypass-ok`

Likely cause:

- `backend/executor/codex.py` currently runs:

```text
codex exec --json [-m model] --skip-git-repo-check <prompt>
```

- It does not pass `--cd`, `--add-dir`, `--sandbox workspace-write`, or a configurable bypass flag.
- On this Windows environment, Codex's sandboxed tool execution fails unless bypass is enabled.

Recommendation:

- Add Codex provider config for execution mode, at minimum one of:
  - `sandbox=workspace-write` for normal environments.
  - explicit admin-controlled `dangerously_bypass_approvals_and_sandbox=true` for trusted local-only use.
- Pass `--cd <project_dir>` and optionally `--add-dir <project_dir>` explicitly.
- Add a Codex provider acceptance case to `run_qa_suite.py` after this is fixed.

## Final Acceptance

- Overall product after fixes: ACCEPTED for Claude CLI primary workflow.
- Multi-Agent collaboration with Claude CLI: ACCEPTED.
- Security regressions from prior report: FIXED.
- Codex CLI provider: PARTIALLY ACCEPTED only for CLI availability; not accepted for platform file-writing execution until backend passes required Codex permission/sandbox flags.
