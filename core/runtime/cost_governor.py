"""Phase 10.2 - CostGovernor: HARD limits on tokens / USD / wall-time / rate.

Fixes vs the spec draft: usage lives in a per-task_id registry (thread-locals leak when an
executor runs the loop in a worker thread and make cross-checking impossible), limits are
checked FAIL-FAST right after each record (not only at context exit), and there is a
pre-call reservation check so the *next* request is refused before it is paid for.

BudgetExceededError is raised at the call site - the Orchestrator treats it like the econ
budget: graceful stop, run marked failed, nothing half-paid except what was already spent.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


class BudgetExceededError(Exception):
    pass


@dataclass
class Budget:
    max_tokens: Optional[int] = None          # combined prompt+completion, hard
    max_usd: Optional[float] = None           # per-task cost cap
    max_wall_time_sec: Optional[int] = None
    max_calls_per_min: Optional[int] = None   # crude rate limiter
    # (input, output) USD per 1M tokens; merged over by config cost.pricing
    pricing: Dict[str, Tuple[float, float]] = field(default_factory=lambda: {
        "gpt-4o": (5.0, 15.0),
        "gpt-4o-mini": (0.15, 0.60),
        "claude-3.5-sonnet": (3.0, 15.0),
    })

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Budget":
        pr = {k: tuple(v) for k, v in (cfg.get("pricing") or {}).items()
              if isinstance(v, (list, tuple)) and len(v) == 2}
        return cls(max_tokens=cfg.get("max_tokens"), max_usd=cfg.get("max_usd"),
                   max_wall_time_sec=cfg.get("max_wall_time_sec"),
                   max_calls_per_min=cfg.get("max_calls_per_min"),
                   pricing={**cls().pricing, **pr})


class CostGovernor:
    def __init__(self, budget: Budget):
        self.budget = budget
        self.lock = threading.Lock()
        self._tasks: Dict[str, dict] = {}

    # ------------------------------------------------------------------ api
    def usage(self, task_id: str) -> dict:
        with self.lock:
            return self._tasks.setdefault(task_id, {"prompt": 0, "completion": 0,
                                                    "start": time.time(), "calls": [], "model": "?"})

    def cost_usd(self, task_id: str, model: Optional[str] = None) -> float:
        u = self.usage(task_id)
        p_in, p_out = self.budget.pricing.get(model or u["model"], (0.0, 0.0))
        return u["prompt"] / 1e6 * p_in + u["completion"] / 1e6 * p_out

    def pre_check(self, task_id: str, est_prompt_tokens: int, model: str) -> None:
        """refuse the request BEFORE sending it if it can break a cap"""
        u = self.usage(task_id)
        u["model"] = model
        now = time.time()
        with self.lock:
            u["calls"] = [t for t in u["calls"] if now - t < 60]
            if self.budget.max_calls_per_min and len(u["calls"]) >= self.budget.max_calls_per_min:
                raise BudgetExceededError(f"rate limit: >= {self.budget.max_calls_per_min} calls/min")
            if self.budget.max_wall_time_sec and now - u["start"] > self.budget.max_wall_time_sec:
                raise BudgetExceededError(f"time limit: {now - u['start']:.0f}s > {self.budget.max_wall_time_sec}s")
        if self.budget.max_tokens:
            projected = u["prompt"] + u["completion"] + est_prompt_tokens  # completion floor unknown
            if projected > self.budget.max_tokens:
                raise BudgetExceededError(f"token limit: projected {projected} > {self.budget.max_tokens}")
        if self.budget.max_usd:
            est = self.cost_usd(task_id, model) + est_prompt_tokens / 1e6 * self.budget.pricing.get(model, (0, 0))[0]
            if est > self.budget.max_usd:
                raise BudgetExceededError(f"cost limit: projected ${est:.4f} > ${self.budget.max_usd}")

    def record(self, task_id: str, prompt_tokens: int, completion_tokens: int, model: str) -> None:
        u = self.usage(task_id)
        with self.lock:
            u["prompt"] += max(int(prompt_tokens), 0)
            u["completion"] += max(int(completion_tokens), 0)
            u["calls"].append(time.time())
            u["model"] = model
        # fail-fast on the recorded truth (pre_check only projected)
        total = u["prompt"] + u["completion"]
        if self.budget.max_tokens and total > self.budget.max_tokens:
            raise BudgetExceededError(f"token limit: {total} > {self.budget.max_tokens}")
        if self.budget.max_usd:
            c = self.cost_usd(task_id, model)
            if c > self.budget.max_usd:
                raise BudgetExceededError(f"cost limit: ${c:.4f} > ${self.budget.max_usd}")

    def snapshot(self, task_id: str) -> dict:
        u = self.usage(task_id)
        return {"prompt": u["prompt"], "completion": u["completion"], "calls": len(u["calls"]),
                "elapsed_s": round(time.time() - u["start"], 1), "usd": round(self.cost_usd(task_id, u["model"]), 6)}
