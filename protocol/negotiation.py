"""Phase 6.5d - Negotiation Protocol: structured positions -> counters -> compromise.

When specialists disagree (e.g. tests are green but security vetoes), the orchestrator's
mediator opens a negotiation round instead of a bare veto. Every exchange is typed so the
whole argument lands in the Decision Log (core.mediator) as auditable JSON - the spec's
"Architect: go with this API / Security: then RBAC is mandatory / Orchestrator: compromise"
conversation, machine-readable.
"""
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class Position(BaseModel):
    agent: str
    stance: str = "accept"                 # accept | block | advisory
    weight: float = 0.0                    # from consensus policy weights
    rationale: str = ""
    asks: List[str] = Field(default_factory=list)      # concrete blocking findings


class CounterOffer(BaseModel):
    from_agent: str
    to_agent: str
    concession: str
    requires: str = ""


class NegotiationRound(BaseModel):
    round_no: int = 1
    positions: List[Position] = Field(default_factory=list)
    counters: List[CounterOffer] = Field(default_factory=list)
    resolved: bool = False


class Compromise(BaseModel):
    adopted: str
    interim_guard: str = ""
    tickets: List[str] = Field(default_factory=list)   # follow-up ticket files created
    rationale: str = ""


class DecisionRecord(BaseModel):
    decision_id: str
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    task: str
    iteration: int = 0
    accepted: bool = False
    weighted_score: float = 0.0
    blocked_by: List[str] = Field(default_factory=list)
    rounds: List[NegotiationRound] = Field(default_factory=list)
    compromise: Optional[Compromise] = None
    outcome: str = ""

    @staticmethod
    def new_id() -> str:
        return "DEC-" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
