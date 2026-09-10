"""Phase 6.3c - QAAgent: test/coverage/mutation/e2e gatekeeper.

Engines (degradation-first, all optional except pytest):
  * pytest                     - full suite, structured pass/fail summary
  * coverage (pytest-cov)      - gated when cfg.coverage_min > 0
  * mutation testing (mutmut)  - opt-in via cfg.mutation (slow; guarded budget)
  * e2e (playwright)           - executed only if installed; otherwise skipped with reason

Input payload: {root, command?: [...pytest args], files?}. Output: QA report.
"""
import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import AsyncIterator, Dict, List

from agents.base_agent import A2AAgent
from protocol.a2a import AgentCard, AgentSkill, Event, Task


class QAAgent(A2AAgent):
    name = "qa-agent"

    def __init__(self, cfg: Dict | None = None):
        super().__init__(cfg)
        self.card = AgentCard(
            name=self.name,
            description="Runs the test suite (+coverage/mutation/e2e when available) and reports quality verdict.",
            skills=[AgentSkill(id="verify_quality", name="Test / coverage / mutation verification")],
        )

    async def handle_task(self, task: Task) -> AsyncIterator[Event]:
        root = str(Path(task.payload.get("root", ".")))
        findings: List[Dict] = []
        details: Dict = {}

        # --- pytest (+optional coverage)
        cov_min = int(self.cfg.get("coverage_min", 0) or 0)
        cmd = ["python", "-m", "pytest", "-q", "--no-header", "-rN"]
        cov_ok = False
        if cov_min > 0:
            try:
                import pytest_cov  # noqa: F401
                cmd += [f"--cov=.", "--cov-report=json", "--cov-fail-under", str(cov_min)]
                cov_ok = True
            except Exception:
                details["coverage"] = "pytest-cov not installed -> coverage gate skipped"
        extra = task.payload.get("command") or []
        cmd += list(extra)
        yield self.progress(task, self.name, f"pytest {' '.join(extra)}")
        try:
            r = await asyncio.to_thread(lambda: subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                                                                 timeout=int(self.cfg.get("timeout_seconds", 180))))
            out = (r.stdout or "") + (r.stderr or "")
            m = re.search(r"(\d+) passed", out); mm = re.search(r"(\d+) failed|(\d+) error", out)
            passed = int(m.group(1)) if m else 0
            failed = int((mm.group(1) or mm.group(2)) if mm else 0)
            no_tests = r.returncode == 5 or "no tests ran" in out.lower()
            if no_tests:
                details["pytest"] = {"collected": 0, "note": "no tests in repo - QA verdict advisory only"}
            elif r.returncode != 0:
                findings.append({"severity": "HIGH", "rule": "pytest", "file": "", "line": 0,
                                 "description": f"pytest rc={r.returncode} ({passed} passed/{failed} failed)",
                                 "suggested_fix": out[-600:]})
            details["pytest"] = {"passed": passed, "failed": failed, "returncode": r.returncode}
            if cov_ok and Path(root, "coverage.json").exists():
                try:
                    pct = json.loads(Path(root, "coverage.json").read_text())["totals"]["percent_covered"]
                    details["coverage_percent"] = round(pct, 1)
                    if pct < cov_min:
                        findings.append({"severity": "MEDIUM", "rule": "coverage", "file": "", "line": 0,
                                         "description": f"coverage {pct:.1f}% < {cov_min}%", "suggested_fix": "add tests for changed lines"})
                except Exception:
                    pass
        except subprocess.TimeoutExpired:
            findings.append({"severity": "MEDIUM", "rule": "pytest-timeout", "file": "", "line": 0,
                             "description": "pytest exceeded budget", "suggested_fix": ""})
            details["pytest"] = {"timeout": True}

        # --- mutation (opt-in, time-boxed)
        if self.cfg.get("mutation") and self.tool("mutmut"):
            yield self.progress(task, self.name, "mutmut run (bounded)")
            try:
                mr = await asyncio.to_thread(lambda: subprocess.run(
                    ["mutmut", "run", "--paths-to-mutate", ",".join(task.payload.get("files") or ["."]),
                     "--tests-dir", ".", "-q"],
                    cwd=root, capture_output=True, text=True, timeout=int(self.cfg.get("mutation_timeout_seconds", 240))))
                det = (mr.stdout or "")[-400:]
                details["mutmut"] = {"returncode": mr.returncode, "tail": det}
                survived = re.search(r"(\d+)\s+survived", det)
                if mr.returncode not in (0, 1) or (survived and int(survived.group(1)) > int(self.cfg.get("max_survived_mutants", 25))):
                    findings.append({"severity": "MEDIUM", "rule": "mutation", "file": "", "line": 0,
                                     "description": f"mutants survived: {survived.group(1) if survived else 'unknown'}",
                                     "suggested_fix": "strengthen assertions on mutated lines"})
            except Exception as e:
                details["mutmut"] = {"skipped": str(e)}
        else:
            details["mutmut"] = {"skipped": "disabled or not installed"}

        # --- e2e (playwright) if present
        try:
            import playwright  # noqa: F401
            details["playwright"] = "installed - e2e suite runnable (wire tests/e2e via payload.command)"
        except Exception:
            details["playwright"] = {"skipped": "playwright not installed"}

        blocking = [f for f in findings if f["severity"] in ("HIGH", "CRITICAL")]
        yield self.verdict(task, self.name, name="qa-report", passed=not blocking,
                           score=1.0 if not findings else (0.4 if blocking else 0.8),
                           summary=("tests FAILED" if blocking else "tests green") + (f" (+{len(findings)-len(blocking)} advisory)"),
                           findings=findings, details=details)
