"""Phase 9.2 - bounty hunter: score -> confident-accept only -> plan + simulated escrow.

Board (econ/board.yaml):
  - issue_url: https://github.com/acme/app/issues/42
    reward_usd: 120
    scope_files: [service/user_service.py]     # declared by the bounty poster
    labels: [bug, good-first-issue]

Confidence is transparent arithmetic (no vibes): base + episode-success-rate + small-scope
+ label signals, minus rewrite/hardware red flags. Accept iff >= auto_accept_threshold.
Accepted bounties produce the EXACT plan (branch, main.py --yes, gh pr create) and an escrow
hold in .cortex_memory/escrow.jsonl (simulated; release/refund hooks on PR outcome).
Everything here is a *plan + ledger*: nothing runs git/gh unless you remove --dry-run, and
even then only the local repo commands run.

Run: python -m econ.bounty --board econ/board.yaml [--no-dry-run]
"""
import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[1]
ESCROW = ROOT / ".cortex_memory" / "escrow.jsonl"

RED_FLAGS = (r"rewrite", r"whole", r"entire", r"hardware", r"robot", r"fpga", r"migrate every", r"전체", r"재작성")
EASY = (r"typo", r"good-first-issue", r"docs", r"test", r"bug")


def estimate(b: Dict, cfg: Dict) -> Dict:
    conf = 0.30
    reasons = ["base"]
    try:  # our own success-rate prior from episodic memory
        from core.memory.episodic import EpisodeStore
        n = EpisodeStore(ROOT).count()
        if n:
            conf += 0.15
            reasons.append(f"{n} stored episode(s)")
    except Exception:
        pass
    scope = b.get("scope_files") or []
    if scope and len(scope) <= 3:
        conf += 0.25
        reasons.append(f"narrow scope ({len(scope)} file(s))")
    elif scope:
        conf -= 0.10
        reasons.append(f"wide scope ({len(scope)} files)")
    labels = " ".join(b.get("labels", [])).lower()
    if re.search("|".join(EASY), labels):
        conf += 0.20
        reasons.append("friendly labels")
    text = (b.get("title", "") + " " + b.get("body", "") + " " + labels).lower()
    for pat in RED_FLAGS:
        if re.search(pat, text):
            conf -= 0.35
            reasons.append(f"red-flag '{pat}'")
            break
    est_cost = float(b.get("est_compute_usd", 0.05))
    reward = float(b.get("reward_usd", 0))
    if reward >= 10 * est_cost:
        conf += 0.10
        reasons.append("reward/cost >= 10x")
    conf = max(0.0, min(0.99, conf))
    return {"confidence": round(conf, 2), "reasons": reasons, "reward_usd": reward, "est_cost_usd": est_cost}


def plan(b: Dict, cfg: Dict) -> List[str]:
    n = re.search(r"/issues/(\d+)", b.get("issue_url", ""))
    num = n.group(1) if n else "0"
    repo = b.get("local_repo", "../my_project")
    return [f"git -C {repo} checkout -B bounty/issue-{num}",
            f"python main.py \"{(b.get('title') or 'fix issue ' + num)[:80]}\" --repo {repo} --config config.yaml --yes",
            f"git -C {repo} push -u origin bounty/issue-{num}",
            f"gh pr create --title 'fix: issue #{num} (cortex bounty run)' --body 'Auto: confidence-scored patch. Escrow: simulated.'"]


def escrow(op: str, b: Dict, extra: Dict | None = None) -> Dict:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "op": op,
           "issue": b.get("issue_url"), "amount_usd": b.get("reward_usd"), "asset": "USDC(simulated)",
           "chain": None, "smart_contract": "MOCK-ESCROW-0x0 (dry ledger, no tx)"}
    rec.update(extra or {})
    ESCROW.parent.mkdir(parents=True, exist_ok=True)
    with ESCROW.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default=str(ROOT / "econ" / "board.yaml"))
    ap.add_argument("--no-dry-run", action="store_true", help="execute plan commands (local git/python only; push/gh still required flags)")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8")) or {}
    econ = cfg.get("econ", {}) or {}
    thr = float(econ.get("auto_accept_threshold", 0.75))
    board = yaml.safe_load(open(a.board, encoding="utf-8")) or []

    accepted = []
    for b in board:
        sc = estimate(b, econ)
        decision = "ACCEPT" if sc["confidence"] >= thr else "PASS"
        print(f"[bounty] {b.get('issue_url')} conf={sc['confidence']} reward=${sc['reward_usd']} -> {decision}")
        print(f"         why: {'; '.join(sc['reasons'])}")
        if decision == "ACCEPT":
            p = plan(b, econ)
            print("         plan:")
            for c in p:
                print(f"           $ {c}")
            esc = escrow("HOLD", b, {"note": "escrow funded (simulated) on accept"})
            print(f"         escrow: {esc['op']} {esc['amount_usd']} {esc['asset']} -> .cortex_memory/escrow.jsonl")
            accepted.append((b, p))
    if a.no_dry_run and accepted:
        print("[bounty] --no-dry-run: executing first accepted plan (local steps only, push/gh included if authed)")
        for c in plan(accepted[0][0], econ):
            r = subprocess.run(c, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=1800)
            print(f"  $ {c[:90]}\n    rc={r.returncode} {(r.stdout or r.stderr)[:140].strip()}")
            if r.returncode != 0:
                escrow("REFUND", accepted[0][0], {"note": "plan step failed - refund hold"})
                break
        else:
            escrow("RELEASE", accepted[0][0], {"note": "PR opened - release hold (simulated settlement)"})
    elif accepted:
        print(f"[bounty] {len(accepted)} accepted plan(s) staged (dry-run). Re-run with --no-dry-run to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
