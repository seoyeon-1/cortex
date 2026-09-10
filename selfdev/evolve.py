"""Phase 8.2 - architecture evolution PROPOSALS (never auto-applied).

Scans a target codebase (default: cortex itself - self-hosting) for rewrite candidates:
  * Rust/pyo3+maturin: pure compute functions (no I/O, hot loops) above a size bar
  * sync -> async: repeated blocking subprocess tool wrappers
  * monolith -> modules: files with N+ top-level defs and fan-in>2 clusters
Each proposal: rationale, estimated gain, migration checklist, rollback plan - written to
docs/selfdev/EVOLUTION-*.md. Merging stays a human (or the loop with tests+consensus) decision.
"""
import argparse
import ast
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

IO_CALLS = ("open(", "requests.", "urllib", "subprocess", "socket", "Client(")


def candidates(root: Path) -> List[Dict]:
    out = []
    for py in sorted(root.rglob("*.py")):
        s = str(py)
        if any(x in s for x in (".venv", "__pycache__", "test", "demo/", "site-packages")):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        rel = py.relative_to(root)
        # pure compute fns -> rust candidate
        for n in tree.body:
            if isinstance(n, ast.FunctionDef) and n.end_lineno - n.lineno >= 22:
                body = ast.dump(n)
                if not any(k in body for k in ("Call(func=Name(id='open'")) and not any(t in ast.unparse(n) for t in IO_CALLS) \
                        and re.search(r"(for |while |range\()", ast.unparse(n)):
                    out.append({"file": str(rel), "line": n.lineno, "kind": "rust-pyo3",
                                "target": n.name,
                                "why": f"{n.end_lineno - n.lineno + 1} LOC compute loop, no I/O",
                                "gain": "5-10x hot path; ship via maturin wheel, keep python fallback"})
        if len(re.findall(r"^def |^class ", src, re.M)) >= 12 and src.count("self.") < 30:
            out.append({"file": str(rel), "line": 1, "kind": "module-split", "target": rel.name,
                        "why": f"{len(re.findall(r'^def |^class ', src, re.M))} top-level defs in one file",
                        "gain": "auto-boundary split by import-cluster; each extracted module keeps API surface"})
        n_sync = len(re.findall(r"subprocess\.(?:run|check_output)\(", src))
        if n_sync >= 4:
            out.append({"file": str(rel), "line": 1, "kind": "sync-to-async", "target": rel.name,
                        "why": f"{n_sync} blocking subprocess calls",
                        "gain": "asyncio.subprocess + structured concurrency; parallelize independent tools"})
    return out[:12]


def render(root: Path, cands: List[Dict]) -> str:
    h = hashlib.sha1(str(datetime.now()).encode()).hexdigest()[:6]
    L = [f"# Architecture evolution proposals ({datetime.now(timezone.utc).isoformat(timespec='seconds')})",
         f"target: {root} | proposals: {len(cands)} | hash {h}", "",
         "_Proposals only - every migration below needs PR + tests + consensus vote._", ""]
    for i, c in enumerate(cands, 1):
        L += [f"## {i}. `{c['kind']}` - {c['file']}:{c['line']}  `{c['target']}`",
              f"- why: {c['why']}", f"- expected gain: {c['gain']}",
              "- checklist: [ ] micro-benchmark baseline  [ ] port  [ ] parity tests  [ ] flag/kill-switch  [ ] rollback plan", ""]
    if not cands:
        L.append("(no rewrite candidates above the bar - the codebase is already shaped fine)")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="target repo (default: cortex itself)")
    a = ap.parse_args()
    cands = candidates(Path(a.root))
    out = Path(__file__).resolve().parents[1] / "docs" / "selfdev"
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"EVOLUTION-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
    p.write_text(render(Path(a.root), cands), encoding="utf-8")
    print(render(Path(a.root), cands)[:1200])
    print(f"\n[saved] {p.relative_to(Path(__file__).resolve().parents[1])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
