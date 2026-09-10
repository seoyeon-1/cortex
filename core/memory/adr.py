"""Phase 3.2 - Decision Log (ADR) auto-generation.

Triggered on a successful `edit_file` / `apply_patch_set`: bundles thought + diff +
test_result into docs/adr/YYYYMMDD_HHMMSS_<task_slug>.md (Context / Decision /
Consequences / Alternatives), human-readable AND re-injectable as LLM context.
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


def slugify(text: str, max_len: int = 40) -> str:
    ascii_only = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if not ascii_only:  # fully non-ascii (e.g. Korean) task -> hash slug, still deterministic
        import hashlib
        ascii_only = "task-" + hashlib.sha1(text.encode()).hexdigest()[:8]
    return ascii_only[:max_len].rstrip("-")


class ADRGenerator:
    def __init__(self, workspace_root: str, adr_dir: Optional[str] = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.adr_dir = Path(adr_dir) if adr_dir else self.workspace_root / "docs" / "adr"

    def record(self, task: str, thought: str, patches: List[Dict], test_result: Optional[Dict],
               reflection: str = "", alternatives: Optional[List[str]] = None,
               git_commit_hash: str = "") -> Optional[Path]:
        """Write one ADR for a successful change. Returns the file path, or None on failure
        (ADR writing must never break the agent run)."""
        try:
            now = datetime.now(timezone.utc)
            fname = f"{now.strftime('%Y%m%d_%H%M%S')}_{slugify(task)}.md"
            self.adr_dir.mkdir(parents=True, exist_ok=True)
            path = self.adr_dir / fname

            files = [p.get("path", "?") for p in patches] or ["(no patches)"]
            diff_block = "\n\n".join(
                f"### {p.get('path', '?')}\n```diff\n{p.get('unified_diff', '').strip()}\n```" for p in patches) or ""
            tr = test_result or {}
            outcome = "PASS" if (tr.get("failed", 0) == 0 and tr.get("errors", 0) == 0) else "PARTIAL/FAIL"

            alts = alternatives or []
            alt_lines = "\n".join(f"- {a}" for a in alts) if alts else f"- (not recorded) Reflection at decision time: {reflection.strip() or 'n/a'}"

            body = f"""# ADR {now.strftime('%Y%m%d_%H%M%S')}: {task.strip()[:120]}

- **Date (UTC):** {now.isoformat(timespec='seconds')}
- **Decision status:** ACCEPTED ({outcome})
- **Files touched:** {', '.join(f'`{f}`' for f in files)}
- **Base commit:** `{git_commit_hash or 'n/a'}`

## Context
{thought.strip() or '(no thought recorded)'}

Test baseline after change: passed={tr.get('passed', '?')}, failed={tr.get('failed', '?')}, errors={tr.get('errors', '?')}.

## Decision
Apply unified diff patch set across {len(files)} file(s):

{diff_block}

## Consequences
- Test suite after patch: {outcome}.
- Reversible: patch is atomic via git sandbox; rollback = `git apply -R` of the diff above.

## Alternatives
{alt_lines}
"""
            path.write_text(body, encoding="utf-8")
            return path
        except Exception as e:
            print(f"[ADR] skipped (non-fatal): {e}")
            return None

    def recent(self, n: int = 3) -> List[Dict]:
        """Newest ADRs as re-injectable context."""
        if not self.adr_dir.exists():
            return []
        files = sorted(self.adr_dir.glob("*.md"), reverse=True)[:n]
        return [{"file": f.name, "content": f.read_text(encoding="utf-8")[:1500]} for f in files]
