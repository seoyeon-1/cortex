"""Phase 6.1 - A2A (Agent-to-Agent) protocol.

The contract every specialist speaks. Pydantic v2 models, JSON round-trippable,
so agents could run in-process (default) or behind HTTP later without schema change.

Flow: Orchestrator -> Task -> A2AAgent.handle_task() -> AsyncIterator[Event]
      terminal Event is kind=VERDICT carrying an Artifact (the report).
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str = ""


class AgentCard(BaseModel):
    """Self-description a specialist advertises to the orchestrator (A2A discovery)."""
    name: str
    version: str = "1.0"
    description: str = ""
    skills: List[AgentSkill] = Field(default_factory=list)
    input_modes: List[str] = Field(default_factory=lambda: ["application/json"])
    output_modes: List[str] = Field(default_factory=lambda: ["application/json"])

    def has_skill(self, skill_id: str) -> bool:
        return any(s.id == skill_id for s in self.skills)


class MessagePart(BaseModel):
    type: Literal["text", "data", "file_ref"] = "text"
    text: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    path: Optional[str] = None


class Message(BaseModel):
    role: Literal["user", "agent", "orchestrator"]
    parts: List[MessagePart] = Field(default_factory=list)
    ts: str = Field(default_factory=_now)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.text)


class Task(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    summary: str                      # human/LLM-readable objective
    skill: str = ""                   # requested skill id (e.g. "scan_security")
    payload: Dict[str, Any] = Field(default_factory=dict)   # {files: [...], diff: str, root: str, ...}
    parent_run: str = ""              # orchestrator session run_id
    status: TaskStatus = TaskStatus.SUBMITTED
    created: str = Field(default_factory=_now)


class Artifact(BaseModel):
    """Report attached to a VERDICT event. `pass` semantics: True = approve."""
    name: str
    media_type: str = "application/json"
    passed: bool = True
    score: float = 1.0                      # agent confidence 0..1
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)


class EventKind(str, Enum):
    ACCEPTED = "accepted"
    PROGRESS = "progress"
    VERDICT = "verdict"
    ERROR = "error"


class Event(BaseModel):
    from_agent: str
    task_id: str
    kind: EventKind
    message: str = ""
    artifact: Optional[Artifact] = None
    ts: str = Field(default_factory=_now)


def verdict_event(agent: str, task_id: str, art: Artifact, message: str = "") -> Event:
    return Event(from_agent=agent, task_id=task_id, kind=EventKind.VERDICT, message=message, artifact=art)


def finding(severity: str, rule: str, file: str, line: int, desc: str, fix: str = "") -> Dict[str, Any]:
    return {"severity": severity.upper(), "rule": rule, "file": file, "line": line, "description": desc, "suggested_fix": fix}


SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
