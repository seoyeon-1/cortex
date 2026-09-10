"""Phase 8.3 - Model routing: task -> cheapest model that can still do the job.

config llm.models is OPTIONAL; when absent the router no-ops (back-compat). Example:

  llm:
    model: gpt-4o-mini
    models:
      fast: gpt-4o-mini          # edits of trivial size
      coding: deepseek-coder     # multi-file implementation
      reasoning: o1-preview      # architecture / security judgment calls
"""
from typing import Dict, Optional, Tuple

_CODING = ("implement", "fix", "refactor", "add ", "write", "patch", "migration", "리팩", "구현", "추가")
_REASON = ("architecture", "security", "threat", "consensus", "design", "review", "audit", "설계", "보안")


class ModelRouter:
    def __init__(self, llm_cfg: Dict):
        self.models = (llm_cfg or {}).get("models") or {}
        self.default = (llm_cfg or {}).get("model")

    def classify(self, task: str, files: Optional[list] = None) -> str:
        hay = (task or "").lower()
        if any(k in hay for k in _REASON):
            return "reasoning"
        if len(files or []) >= 3 or any(k in hay for k in _CODING):
            return "coding"
        return "fast"

    def route(self, task: str, files: Optional[list] = None) -> Tuple[Optional[str], str]:
        if not self.models:
            return None, "no llm.models configured - default model stays"
        cls = self.classify(task, files)
        m = self.models.get(cls) or self.default
        return m, f"class={cls}"

    def cheapest(self) -> Optional[str]:
        return self.models.get("fast") or None
