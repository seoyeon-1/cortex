"""Phase 6.2 - A2AAgent: base class for specialist sub-agents.

Contract: `handle_task(task) -> AsyncIterator[Event]` (A2A protocol). Specialists emit
PROGRESS events while working and exactly one terminal VERDICT event carrying an Artifact
(passed/score/findings). Orchestrator consumes via `run_task()` (async) or
`run_task_sync()` (blocking wrapper) and feeds verdicts into its consensus policy.

Agents are degradation-first: every external tool is optional; if absent, the agent says
so in the artifact details and falls back to its builtin analyzer instead of failing the run.
"""
import asyncio
import shutil
from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, List, Optional

from protocol.a2a import AgentCard, Artifact, Event, EventKind, Task, verdict_event


class A2AAgent(ABC):
    card: AgentCard
    name: str = "agent"

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    # ----------------------------------------------------------------- api
    @abstractmethod
    async def handle_task(self, task: Task) -> AsyncIterator[Event]:
        """Yield PROGRESS events, end with exactly one VERDICT event."""
        raise NotImplementedError
        yield  # pragma: no cover

    async def run_task(self, task: Task, on_event=None) -> Event:
        """Drain the async iterator; guarantee a terminal verdict even on internal errors.

        on_event: optional sync callback invoked for every event (progress logging)."""
        last: Optional[Event] = None
        try:
            async for ev in self.handle_task(task):
                last = ev
                if on_event is not None:
                    try:
                        on_event(ev)
                    except Exception:
                        pass
        except Exception as e:  # a broken specialist must never kill the society
            last = verdict_event(self.name, task.id, Artifact(
                name=f"{self.name}-error", passed=True, score=0.0,
                summary=f"agent crashed (ignored, treated as abstain): {e}", details={"error": str(e)}))
        if last is None or last.kind != EventKind.VERDICT:
            last = verdict_event(self.name, task.id, Artifact(
                name=f"{self.name}-abstain", passed=True, score=0.0, summary="no verdict produced"))
        return last

    def run_task_sync(self, task: Task, on_event=None) -> Event:
        return asyncio.run(self.run_task(task, on_event=on_event))

    # --------------------------------------------------------------- utils
    def tool(self, binary: str) -> Optional[str]:
        return shutil.which(binary)

    @staticmethod
    def progress(task: Task, agent: str, msg: str) -> Event:
        return Event(from_agent=agent, task_id=task.id, kind=EventKind.PROGRESS, message=msg)

    @staticmethod
    def verdict(task: Task, agent: str, *, name: str, passed: bool, summary: str,
                findings: Optional[List[Dict]] = None, score: float = 1.0,
                details: Optional[Dict] = None) -> Event:
        art = Artifact(name=name, passed=passed, score=round(score, 3), summary=summary,
                       findings=findings or [], details=details or {})
        return verdict_event(agent, task.id, art, message=summary)
