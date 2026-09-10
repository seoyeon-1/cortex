#!/usr/bin/env python3
"""Phase 15.5 - local agent-quality harness (CI-friendly JSON metrics).

Runs the full agent on one task (main.py, sandboxed) and reduces the console log to
machine-readable metrics. Exit code 0 only on verified success (all tests green in
sandbox), so it composes as a CI step next to `python -m eval.benchmarker`.

  python scripts/verify_agent.py --task "Fix div by zero" --repo ../my_project [--dry-run]
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def run_verification(task: str, repo: str, config: str = "config.yaml", dry_run: bool = False,
                     timeout: int = 600) -> dict:
    root = Path(__file__).resolve().parents[1]
    cmd = [sys.executable, str(root / "main.py"), task, "--repo", repo, "--config", config, "--yes"]
    if dry_run:
        cmd.append("--dry-run")
    start = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout)
        out, err, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, rc = "TIMEOUT", 124
    dur = round(time.time() - start, 2)
    log = ANSI_RE.sub("", out)

    metrics = {
        "task": task,
        "success": "TASK COMPLETED IN SANDBOX" in log,
        "patch_generated": "TASK COMPLETED IN SANDBOX" in log or "Generated Patch" in log or "diff" in log.lower(),
        "duration_sec": dur,
        "iterations": len(re.findall(r"\[iter|ITERATION", log, re.IGNORECASE)),
        "tool_calls": log.count("TOOL CALL:"),
        "recovered_tool_calls": log.count("[recovery: succeeded on retry"),
        "preflight_blocks": log.count("Preflight failed") + log.count("preflight: Preflight failed"),
        "guardrail_blocks": log.count("GUARDRAILS REFUSED"),
        "structured_recoveries": log.count("Structured recovery:"),
        "episode_recorded": "Episode recorded" in log,
        "exit_code": rc,
        "error_tail": ANSI_RE.sub("", err)[-500:] if err else "",
    }
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    return metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run cortex once and emit quality metrics as JSON")
    ap.add_argument("--task", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="no apply to the main repo")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    m = run_verification(a.task, a.repo, a.config, a.dry_run, a.timeout)
    sys.exit(0 if m["success"] else 1)
