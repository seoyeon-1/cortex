"""Phase 7.3d - the mini AI-Scientist loop:
read -> hypothesize -> experiment (drone twin, real numbers) -> track -> LaTeX draft.

Run: python -m research.report "quadrotor PID tuning" [--query-only]
"""
import argparse
import json
from pathlib import Path

from research.crawler import fetch, hypotheses
from research.latex import render, write
from research.tracker import Run, live_backend
from skills.hardware.twin import DroneTwin

OUT = Path(__file__).resolve().parent / "out"


def run_experiment() -> list:
    """baseline vs tuned gains on the twin -> metrics rows for the report."""
    grid = [{"kp": 4.0, "ki": 0.0, "kd": 0.02}, {"kp": 2.2, "ki": 0.8, "kd": 0.72}]
    base = DroneTwin.tune(grid[:1], iters=400)["best"]["mean_rms_vibration"]
    tuned = DroneTwin.tune(grid, iters=400)["best"]["mean_rms_vibration"]
    return [{"name": "pid_gain_search", "metric": "rms_vibration", "baseline": base, "result": tuned,
             "improvement_pct": round((1 - tuned / max(base, 1e-9)) * 100, 1)}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="quadrotor PID vibration reduction")
    ap.add_argument("--skip-exp", action="store_true")
    a = ap.parse_args()

    lit = fetch(a.query)
    hyps = hypotheses(lit["papers"])
    exps = [] if a.skip_exp else run_experiment()

    run = Run("auto_" + a.query[:24].replace(" ", "_"), root=OUT.parent)
    run.log(lit_source=lit["source"], papers=len(lit["papers"]), hypotheses=len(hyps), backend=live_backend())
    for e in exps:
        run.log(experiment=e["name"], metric=e["metric"], baseline=e["baseline"], result=e["result"])
    d = run.finish(experiments=len(exps))

    tex = render(f"Cortex Research Note: {a.query.title()}", lit["papers"], exps,
                 hypothesis=hyps[0] if hyps else "")
    p = write(tex, OUT / "techreport.tex")
    (OUT / "report_meta.json").write_text(json.dumps(
        {"query": a.query, "source": lit["source"], "run_dir": str(d.relative_to(OUT.parent)),
         "hypotheses": hyps, "experiments": exps}, indent=1))
    print(f"[research] literature={lit['source']}({len(lit['papers'])}) hyp={len(hyps)} exp={len(exps)}")
    if exps:
        print(f"[research] {exps[0]['name']}: rms {exps[0]['baseline']} -> {exps[0]['result']} "
              f"(-{exps[0]['improvement_pct']}%)")
    print(f"[research] draft: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
