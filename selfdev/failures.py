"""Phase 8.3b - failure mining: every failed run becomes a preference pair (local only).

.cortex_memory/feedback/dpo_pairs.jsonl lines:
  {prompt_tail, rejected:{patches, verdicts}, chosen: null (until a later success on the same
  task closes the pair), ts, run}. When a SUCCESS run's first-accepted patch differs from a
  stored rejected attempt of a similar task, we close the pair (chosen = accepted patch) -
  that's exactly the DPO/GRPO training signal, ready to consume offline.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
PAIRS = ROOT / ".cortex_memory" / "feedback" / "dpo_pairs.jsonl"


def record_failure(prompt_tail: str, rejected_patches: List[Dict], verdicts: Dict[str, Any], task: str) -> None:
    PAIRS.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": round(time.time(), 1), "task": task[:160], "prompt_tail": prompt_tail[-900:],
           "rejected": {"patches": rejected_patches[:2], "verdicts": verdicts}, "chosen": None, "closed": False}
    with PAIRS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def close_open_pair(task_keywords: List[str], accepted_patches: List[Dict]) -> Optional[Dict]:
    if not PAIRS.exists():
        return None
    rows = [json.loads(l) for l in PAIRS.read_text(encoding="utf-8").splitlines() if l.strip()]
    idx = None
    for i, r in enumerate(rows):
        if not r.get("closed") and any(k in r["task"].lower() for k in task_keywords):
            idx = i
            break
    if idx is None:
        return None
    rows[idx]["chosen"] = {"patches": [str(p.get("path")) for p in accepted_patches[:4]], "source": "later success"}
    rows[idx]["closed"] = True
    PAIRS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return rows[idx]
