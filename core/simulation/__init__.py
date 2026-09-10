"""Phase 15 - Static simulation & pre-flight verification (LLM-cost-free).

Runs a whole candidate patch THROUGH the compiler/linter/import pipeline *in memory*
before anything is written to disk or any further LLM turn is paid for.
"""
from core.simulation.preflight import PreflightResult, PreflightSimulator  # noqa: F401

__all__ = ["PreflightResult", "PreflightSimulator"]
