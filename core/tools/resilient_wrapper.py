"""Phase 15.4 - Resilient tool execution (bounded auto-recovery).

Wraps BaseTool execution with per-tool retry strategies so transient failures
(flaky test runs, timeouts, sandbox hiccups) don't burn a whole agent iteration:

  attempt 0  -> call as-is
  failed & retryable -> backoff, re-call with (optionally mutated) kwargs up to
                        the strategy budget, then return the LAST real failure.

Hard rules:
- NEVER fabricate success: after the budget the original failing ToolResult is returned
  (augmented with a `[recovery]` note), and `retryable=False` marks can short-circuit.
- Security/blocking paths must never be retried (guardrail/preflight verdicts):
  NON_RETRYABLE markers below make "denied stays denied" explicit.
"""
import time
from typing import Callable, Dict, List, Optional

from core.tools.base import BaseTool, ToolResult

# Substrings that mark a failure as POLICY or DETERMINISTIC - retrying is pointless or unsafe.
NON_RETRYABLE_MARKERS = (
    "preflight", "guardrail", "not allowed", "sensitive path", "redacted",
    "not enough budget", "rejected", "rollback", "invalid signature", "unknown tool",
)

# Per-tool default recovery plans (action names interpreted by _apply_strategy).
RECOVERY_STRATEGIES: Dict[str, List[Dict]] = {
    "edit_file":        [{"action": "retry_with_read_hint", "max": 1}],   # context drift: re-read then retry
    "apply_patch_set":  [{"action": "retry_with_read_hint", "max": 1}],
    "run_tests":        [{"action": "retry_flaky", "max": 2}],            # flake insurance
    "default":          [{"action": "retry", "max": 1}],
}


class ResilientToolWrapper(BaseTool):
    """Transparent BaseTool proxy: same name/description/parameters, extra recovery layer."""

    def __init__(self, tool: BaseTool, strategies: Optional[List[Dict]] = None,
                 backoff_sec: float = 0.2, kwargs_mutator: Optional[Callable[[dict, str], Optional[dict]]] = None):
        self._tool = tool
        self.name = getattr(tool, "name", "wrapped")
        self.description = getattr(tool, "description", "")
        self.parameters = getattr(tool, "parameters", {})
        self.strategies = (strategies if strategies is not None
                           else RECOVERY_STRATEGIES.get(self.name, RECOVERY_STRATEGIES["default"]))
        self.backoff_sec = backoff_sec
        self.kwargs_mutator = kwargs_mutator      # tool-specific retry tweak (e.g. fresh content)
        self.recovery_log: List[str] = []

    # ------------------------------------------------------------------ execution

    def execute(self, **kwargs) -> ToolResult:
        budget = sum(int(s.get("max", 1)) for s in self.strategies) if self._retry_allowed() else 0
        attempt, last = 0, None
        while True:
            try:
                result = self._tool.execute(**kwargs)
            except Exception as e:                      # tool crashed - treat as retryable failure
                result = ToolResult(False, error=f"tool raised: {type(e).__name__}: {e}")
            last = result
            if result.success:
                if attempt:
                    result.summary = f"[recovery: succeeded on retry {attempt}] {result.summary}"
                return result
            err = (result.error or "") + " " + (result.summary or "")
            low = err.lower()
            if getattr(result, "retryable", None) is False or any(m in low for m in NON_RETRYABLE_MARKERS):
                return result                            # policy/deterministic: fail fast, no noise
            if attempt >= budget:
                if attempt:
                    last.summary = (f"[recovery: {attempt} retries exhausted] " + (last.summary or "")).strip()
                return last
            attempt += 1
            self.recovery_log.append(f"{self.name} attempt {attempt}: {err[:120]}")
            if self.kwargs_mutator:
                try:
                    tweaked = self.kwargs_mutator(dict(kwargs), err)
                    if tweaked:
                        kwargs = tweaked
                except Exception:
                    pass
            time.sleep(self.backoff_sec * attempt)

    def _retry_allowed(self) -> bool:
        # guardrails wrap tool lookup upstream; here we only gate by config presence
        return True

    def to_openai_function(self) -> Dict:
        return self._tool.to_openai_function()
