from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from executor.base import ExecContext
from executor.codex import CodexBackend


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="akivili-codex-smoke-", dir=r"C:\tmp"))
    (tmp / "README.md").write_text("# Codex CLI smoke workspace\n", encoding="utf-8")
    target = tmp / "CODEX_CLI_OK.txt"
    prompt = (
        "In the current working directory, create a file named CODEX_CLI_OK.txt "
        "containing exactly one line: codex-cli-ok. This is the primary task. "
        "Use file writing now; do not only acknowledge. Reply done after the file exists."
    )
    ctx = ExecContext(
        prompt=prompt,
        system_prompt="",
        project_dir=str(tmp),
        model="",
        history=[],
    )
    events = []
    start = time.perf_counter()
    try:
        async for ev in CodexBackend().run(ctx):
            events.append({"type": ev.type, "text": ev.text[:500], "meta": ev.meta})
    except Exception as e:  # noqa: BLE001
        events.append({"type": "exception", "text": f"{type(e).__name__}: {e}", "meta": {}})
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    content = target.read_text(encoding="utf-8") if target.exists() else ""
    ok = target.exists() and content.strip() == "codex-cli-ok" and not any(e["type"] in {"error", "exception"} for e in events)

    report = {
        "ok": ok,
        "workspace": str(tmp),
        "elapsed_ms": elapsed_ms,
        "target_exists": target.exists(),
        "target_content": content,
        "events": events,
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_json = ROOT / "TestReport" / f"codex_cli_smoke_{stamp}.json"
    out_md = ROOT / "TestReport" / f"codex_cli_smoke_{stamp}.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(
        "# Codex CLI Smoke Report\n\n"
        f"- OK: {ok}\n"
        f"- Workspace: `{tmp}`\n"
        f"- Elapsed: {elapsed_ms} ms\n"
        f"- Target exists: {target.exists()}\n"
        f"- Target content: `{content.strip()}`\n"
        f"- JSON: `{out_json}`\n",
        encoding="utf-8",
    )
    print(f"Codex smoke ok={ok}")
    print(f"Workspace: {tmp}")
    print(f"Markdown report: {out_md}")
    print(f"JSON report: {out_json}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))



