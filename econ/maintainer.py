"""Phase 9.3 - maintainer agent: triage, dependabot gating, community replies (draft-only).

Classifies incoming issues (bug/feature/question), drafts reply templates with runnable
snippet stubs, and builds a dependabot merge-plan (verify -> merge if green). All outputs
are DRAFTS/JOURNALS by default: posting to Discord/Slack/GitHub needs an explicit webhook
url + maintainer.post_replies=true; otherwise entries land in .cortex_memory/maintainer.jsonl.
"""
import json
import re
import time
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / ".cortex_memory" / "maintainer.jsonl"

_SIGNALS = {
    "bug": (r"crash|error|exception|traceback|fails?|repro|\bbug\b|오류|터진다|실패"),
    "feature": (r"feature|request|would be nice|add support|新功能|추가"),
    "question": (r"\?\b|how do|how to|why is|question|docs\?|문의|어떻게"),
}


def classify(title: str, body: str = "") -> Dict:
    text = f"{title} {body}".lower()
    scores = {k: len(re.findall(p, text)) for k, p in _SIGNALS.items()}
    top = max(scores, key=lambda k: scores[k]) if any(scores.values()) else "question"
    return {"labels_suggested": [top], "signals": scores,
            "needs_repro": top == "bug" and not re.search(r"repro|step", text)}


def reply_draft(cls: Dict, title: str) -> str:
    kind = cls["labels_suggested"][0]
    if kind == "bug":
        return ("Thanks for the report! Cortex reproduced nothing yet - minimal repro steps would "
                "unblock an automated fix PR. I'll attach a `cortex:fix` label when one lands.")
    if kind == "feature":
        return "Noted as an enhancement. Accepting a design-first PR: interface sketch + tests, then implementation."
    return ("Short answer scaffold:\n```python\n# cortex draft reply - verify before posting\n```\n"
            "Link the relevant module once confirmed.")


def dependabot_plan(pr: Dict) -> Dict:
    return {"action": "verify-then-merge", "pr": pr.get("number"), "pkg": pr.get("title", ""),
            "commands": ["git fetch origin pull/%s/head:dependabot-check" % pr.get("number", 0),
                         "python -m pytest -q  # inside a worktree of the PR head",
                         "gh pr merge --squash  # ONLY if green and patch bumps within minor range"],
            "auto_merge_allowed": False, "note": "merge decision stays human unless config flips both gates"}


def record(event: str, payload: Dict, post: Optional[bool] = None) -> Dict:
    rec = {"ts": round(time.time(), 1), "event": event, **payload}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if post:  # never reached by default
        raise RuntimeError("network posting is disabled in this build - wire a webhook deliberately")
    return rec
