"""Phase 6 compatibility shim.

`AgentLoop` was grown into `core.orchestrator.Orchestrator` (an orchestrator that commands a
society of specialist A2A sub-agents). Existing imports keep working unchanged:

    from core.loop import AgentLoop   # == Orchestrator
"""
from core.orchestrator import Orchestrator  # noqa: F401  (re-export)

AgentLoop = Orchestrator

__all__ = ["AgentLoop", "Orchestrator"]
