# Akivili QA Test Report

## Run Artifacts

- Evaluation plan: `TestReport/QA_EVALUATION_PLAN.md`
- QA runner: `TestReport/run_qa_suite.py`
- Latest full live report: `TestReport/qa_results_20260701-131448.md`
- Latest full live JSON: `TestReport/qa_results_20260701-131448.json`
- Latest isolated live workspace: `C:\tmp\akivili-qa-yy94lre3`

## Scope

This run covered functional, security, performance, and multi-agent collaboration tests against an isolated database/config/workspace. The live collaboration probe used Claude CLI in a temporary workspace and did not use the real Akivili database.

## Summary

- Total cases: 38
- Passed: 35
- Failed: 3
- Deterministic collaboration: PASS
- Live collaboration score: 80/100
- API performance: PASS, task list p95 = 3.94 ms
- Deterministic queue performance: PASS, 3-round queue = 140.91 ms
- Live collaboration elapsed: 68.8 s, ticks = 7.1 s / 32.2 s / 29.4 s

## High-Signal Findings

### 1. CLI-backed collaboration loses conversation context for member Agents

Severity: P0 for collaboration quality.

Evidence from live run:

- Leader correctly delegated: `@后端开发者 ... 创建 QA_COLLAB_RESULT.md ... 完成后 @测试专员 验证文件内容`.
- Queue correctly triggered: `specialized-project-owner -> qa-backend-developer -> qa-tester`.
- All runs ended as `succeeded`.
- Expected file `QA_COLLAB_RESULT.md` was not created.
- Backend and tester both replied that the workspace had no concrete task, despite the task description and Leader message existing in DB.

Likely root cause:

- `executor.runner.build_context()` passes `history` into `ExecContext`.
- `ApiLlmBackend` uses `ctx.history`.
- `ClaudeCodeBackend` and `CodexBackend` ignore `ctx.history`; they only pass `ctx.prompt` plus system prompt to CLI.
- In `collab._run_one`, mention-triggered member runs use generic prompt `请根据上文完成分派给你的工作...`; with CLI backends there is no actual history injected, so members cannot see the Leader assignment or original task.

Impact:

- Multi-Agent collaboration appears to trigger correctly, but CLI-backed members may not know what they were asked to do.
- This directly reduces task completion and makes Leader value only partially realized.

### 2. Unknown `/api/*` route is swallowed by SPA fallback and returns 200 HTML

Severity: P1 security/API correctness.

Evidence: `GET /api/__qa_unknown_endpoint__` returned HTTP 200 with HTML beginning `<!DOCTYPE html>`.

Impact:

- API clients may treat unknown endpoints as success.
- Security scanners and contract tests get misleading results.
- Recommended behavior: unknown `/api/*` should return 404 JSON, while non-API paths can fall back to SPA.

### 3. Memory slug `..` is accepted despite OpenSpec saying slug containing `..` should be rejected

Severity: P1 spec/security consistency.

Evidence: module-level read for slug `..` did not raise `ValueError`; implementation maps it to `memory/...md`, which stays inside memory dir.

Impact:

- No observed directory escape for `..`, because `.md` suffix keeps it inside root.
- Still violates OpenSpec and weakens the security contract. The slug regex should reject `..` explicitly if that is the intended rule.

## Collaboration Assessment

### Deterministic Engine

Result: PASS.

- Leader order: `specialized-project-owner -> qa-backend-developer -> qa-tester`
- @mention parsing: PASS
- queued/running dedupe: PASS
- depth limit: PASS
- roster includes precise `@成员名` syntax and skills: PASS
- queue performance: 140.91 ms for 3 deterministic rounds

### Live Claude CLI Probe

Result: PARTIAL PASS, product issue found.

- Leader role/value: PASS. Leader did not do the work directly and selected backend developer correctly.
- Person selection: PASS. Backend was the right assignee for `owner=backend` file creation.
- Trigger chain: PASS. Backend then tester were triggered.
- Task completion: FAIL. Required file was not created.
- Safety: PASS in observed workspace; only temp `README.md` existed after run.
- Performance: PASS for small live probe, 68.8 s total.

Final live score: 80/100 by the current scoring rubric, but this score is misleadingly generous because completion failed. For release gating, treat this as not releasable for CLI-backed collaboration until context propagation is fixed.

## Recommended Next Actions

1. Fix CLI executor context propagation: include task title/description and conversation history in the CLI prompt or system prompt for Claude/Codex backends.
2. Add a regression test asserting a mention-triggered CLI member sees the Leader assignment.
3. Change SPA fallback to skip `/api/*` paths and return JSON 404 for unknown API routes.
4. Tighten memory slug validation to reject `..` explicitly, or update OpenSpec if accepting `..` as `...md` is intentional.
5. After fixes, rerun: `py -3.12 TestReport\run_qa_suite.py --live --keep`.
