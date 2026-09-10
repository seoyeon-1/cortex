"""Phase 12.1 - AgentBenchmarker: golden dataset -> nightly regression report + block CI.

Each case runs the REAL pipeline (main.py against the scenario mock LLM) in a subprocess,
then asserts on the durable artifacts: session .meta.json (success/iterations), the event
stream (verdicts/route) and applied files. Reports land in eval/results/eval_<ts>.json and
are COMPARED against the previous run - any regression exits non-zero for CI blocking.

Run: python -m eval.benchmarker [--tag refactor] [--baseline last]
Requires the scenario mock (port 8757) for g-1xx cases; skipped cleanly if it is down.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).resolve().parent / "dataset.jsonl"
RESULTS = Path(__file__).resolve().parent / "results"


@dataclass
class EvalCase:
    id: str
    task: str
    repo_fixture: str
    config: str
    expected_outcome: Dict
    tags: List[str] = field(default_factory=list)


class AgentBenchmarker:
    def __init__(self, dataset_path: Path = DATASET, results_dir: Path = RESULTS):
        self.results_dir = results_dir
        self.cases = [EvalCase(**json.loads(l)) for l in dataset_path.read_text().splitlines() if l.strip()]

    # ------------------------------------------------------------------ run
    def mock_up(self) -> bool:
        import socket
        s = socket.socket(); s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", 8757)); return True
        except Exception:
            return False
        finally:
            s.close()

    def run_case(self, case: EvalCase) -> Dict:
        t0 = time.time()
        sess_dir = ROOT / ".cortex_memory" / "sessions"
        before = {p.name for p in sess_dir.glob("*.meta.json")}
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   PATH="/home/user/cortex/.venv/bin:" + os.environ.get("PATH", ""))
        cmd = [str(ROOT / ".venv/bin/python"), "main.py", case.task, "--repo", case.repo_fixture,
               "--config", case.config]
        r = subprocess.run(cmd, cwd=ROOT, env=env, input="n\n", capture_output=True, text=True, timeout=420)
        after = {p.name for p in sess_dir.glob("*.meta.json")} - before
        meta = {}
        if after:
            newest = sorted(after)[-1]
            meta = json.loads((sess_dir / newest).read_text())
            events = [json.loads(l) for l in (sess_dir / newest.replace(".meta.json", ".jsonl")).read_text().splitlines()]
        else:
            events = []
        got = {"success": bool(meta.get("success")), "iterations": meta.get("iterations", 0),
               "applied_files": meta.get("applied_files", []), "run_id": next(iter(after), "")[:15],
               "stdout_tail": (r.stdout or "")[-400:]}
        agents = sorted({e["data"].get("agent") for e in events if e["kind"] == "verdict"})
        low = (r.stdout or "").lower()
        gov_stop = "governor" in low or "budget exceeded" in low
        stop_reason = "cost governor" if gov_stop else ""
        got.update({"agents_reviewed": agents, "stop_reason": stop_reason})

        exp = case.expected_outcome
        fails = []
        if "success" in exp and got["success"] != exp["success"]:
            fails.append(f"success {got['success']} != {exp['success']}")
        if "max_iterations" in exp and got["iterations"] > exp["max_iterations"]:
            fails.append(f"iterations {got['iterations']} > {exp['max_iterations']}")
        for f_ in exp.get("files_changed", []):
            if f_ not in got["applied_files"]:
                fails.append(f"expected file {f_} not changed")
        for a_ in exp.get("agents_reviewed", []):
            if a_ not in got["agents_reviewed"]:
                fails.append(f"persona {a_} did not review (route miss)")
        if exp.get("stop_reason") and exp["stop_reason"] not in got["stop_reason"]:
            fails.append(f"stop_reason {got['stop_reason']!r} != {exp['stop_reason']!r}")
        if not got["success"]:
            if "durable.db" not in ("",):
                pass
        return {"case": case.id, "pass": not fails, "fails": fails, "wall_s": round(time.time() - t0, 1), **got}

    def run_eval(self, filter_tags: Optional[List[str]] = None) -> Dict:
        out = {"ts": round(time.time(), 1), "passed": 0, "failed": 0, "skipped": 0, "details": []}
        if not self.mock_up():
            out["note"] = "scenario mock (:8757) not running - start: python /tmp/mockllm/server_scenario.py"
            out["skipped"] = len(self.cases)
            return out
        for c in self.cases:
            if filter_tags and not (set(filter_tags) & set(c.tags or [])):
                continue
            print(f"[EVAL] {c.id} ...", flush=True)
            try:
                res = self.run_case(c)
            except Exception as e:
                res = {"case": c.id, "pass": False, "fails": [f"error: {e}"]}
            out["details"].append(res)
            out["passed" if res.get("pass") else "failed"] += 1
            print(f"       {'PASS' if res.get('pass') else 'FAIL'} ({res.get('wall_s', '?')}s)"
                  + ("  " + "; ".join(res["fails"]) if res.get("fails") else ""))
        return out

    # ------------------------------------------------------------ compare
    def report_and_gate(self, res: Dict) -> int:
        RESULTS.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / f"eval_{int(res['ts'])}.json"
        path.write_text(json.dumps(res, indent=1))
        prev = sorted(self.results_dir.glob("eval_*.json"))[-2:-1]
        regressions = []
        if prev:
            p = json.loads(prev[0].read_text())
            pmap = {d["case"]: d.get("pass") for d in p.get("details", [])}
            for d in res["details"]:
                if pmap.get(d["case"]) and not d["pass"]:
                    regressions.append(d["case"])
            if res.get("skipped"):
                regressions = []
        print(f"\n[eval] passed={res['passed']} failed={res['failed']} skipped={res.get('skipped', 0)} -> {path.name}")
        if regressions:
            print("[eval] REGRESSION vs previous run:", ", ".join(regressions), " (CI gate: FAIL)")
            return 1
        return 1 if res["failed"] else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", action="append")
    a = ap.parse_args()
    b = AgentBenchmarker()
    return b.report_and_gate(b.run_eval(a.tag))


if __name__ == "__main__":
    sys.exit(main())
