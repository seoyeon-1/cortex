"""Phase 8.1 - self-audit: read our own session logs, find waste, propose the fix.

    python -m selfdev.audit [--limit 12]

Metrics per run: est tokens/iteration, duplicate-retry rate (same file+thought re-patched),
prompt-prefix reuse across iterations (the prompt_caching headroom), cost via dashboard
pricing. Every finding carries a machine-parsable `-> PROPOSE:` line so cortex can pick the
audit itself up as a task (self-hosting loop).
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / ".cortex_memory" / "sessions"
OUT = ROOT / "docs" / "selfdev"


def _pricing() -> Dict[str, Dict[str, float]]:
    try:
        import yaml
        cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8")) or {}
        return (cfg.get("dashboard", {}) or {}).get("pricing", {}) or {}
    except Exception:
        return {}


def analyze_run(path: Path) -> Dict:
    evs = [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]
    req = [e for e in evs if e["kind"] == "llm_request"]
    resp = [e for e in evs if e["kind"] == "llm_response"]
    patches = [e for e in evs if e["kind"] == "patch"]
    dup = 0
    seen = set()
    for p in patches:
        key = hashlib.sha1(json.dumps(sorted(p["data"].get("files", []))).encode()).hexdigest()[:12]
        if key in seen:
            dup += 1
        seen.add(key)
    pin = sum(r["data"].get("est_prompt_tokens", 0) for r in req)
    pout = sum(r["data"].get("est_completion_tokens", 0) for r in resp)
    # prefix reuse proxy: iterations re-send the same initial context -> cacheable fraction
    reuse = (len(req) - 1) / max(len(req), 1)
    return {"run": path.stem, "iters": len(req), "tok_in": pin, "tok_out": pout,
            "dup_patch_files": dup, "cacheable_prompt_frac": round(reuse, 2),
            "total_tok": pin + pout,
            "tok_per_iter": round((pin + pout) / max(len(req), 1))}


def cost(rows: List[Dict], model: str = "gpt-4o-mini") -> float:
    pr = _pricing().get(model, {"prompt": 0.15, "completion": 0.60})
    return round(sum(r["tok_in"] for r in rows) / 1e6 * pr.get("prompt", 0)
                 + sum(r["tok_out"] for r in rows) / 1e6 * pr.get("completion", 0), 6)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    a = ap.parse_args()
    files = sorted(SESSIONS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[: a.limit]
    rows = [analyze_run(f) for f in files]
    waste_iters = [r for r in rows if r["dup_patch_files"] > 0]
    heavy = max(rows, key=lambda r: r["tok_per_iter"]) if rows else None
    total_cost = cost(rows)
    lines = [f"# Cortex self-audit ({datetime.now(timezone.utc).isoformat(timespec='seconds')})", "",
             f"runs analyzed: {len(rows)} | est cost ${total_cost}", "", "| run | iters | tok/iter | dup-patch | cacheable prompt |",
             "|---|---|---|---|---|"]
    for r in rows[:10]:
        lines.append(f"| {r['run'][-8:]} | {r['iters']} | {r['tok_per_iter']} | {r['dup_patch_files']} | {r['cacheable_prompt_frac']:.0%} |")
    lines.append("")
    if rows and any(r["cacheable_prompt_frac"] > 0.4 for r in rows):
        lines.append(f"-> PROPOSE: enable prompt_caching - {sum(r['iters']-1 for r in rows)} repeat-send iteration contexts "
                     f"(~{int(sum(r['tok_in'] for r in rows)*0.4):,} prompt tokens cacheable)")
    if waste_iters:
        lines.append(f"-> PROPOSE: patch-dedup guard - {len(waste_iters)} run(s) re-submitted identical file sets; "
                     f"block a patch whose target-file hash already failed this run")
    if heavy:
        lines.append(f"-> PROPOSE: context budget clamp - worst run {heavy['run'][-8:]} spent {heavy['tok_per_iter']} tok/iter; "
                     f"trim extra_context before resending full file bodies")
    if not rows:
        lines.append("(no sessions yet - run the demo first)")
    OUT.mkdir(parents=True, exist_ok=True)
    md = OUT / f"AUDIT-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[saved] {md.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
