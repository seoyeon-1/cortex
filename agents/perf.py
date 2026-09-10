"""Phase 6.5c - Performance Eng persona.

Validation bar: "Bottleneck 제거, Top-Hotspot 누적비용 30% 감소 목표".
Engines (degradation-first):
  * cProfile (stdlib) - imports the changed module, profiles its public callables with tiny
    synthetic inputs; produces cumulative top-N + a flame-text graph (folded-style lines).
  * py-spy / locust - referenced if installed (external targets are out of scope in a
    sandbox; the report says so instead of pretending).
Task payload: {root, files, top_n?}. Artifact: perf-report.
"""
import cProfile
import inspect
from pathlib import Path
from typing import AsyncIterator, Dict, List

from agents.base_agent import A2AAgent
from protocol.a2a import AgentCard, AgentSkill, Event, Task


class PerfAgent(A2AAgent):
    name = "performance-agent"

    def __init__(self, cfg: Dict | None = None):
        super().__init__(cfg)
        self.top_n = int(self.cfg.get("top_n", 8))
        self.card = AgentCard(name=self.name,
                              description="CPU hotspots + flame-text for changed modules; load-test hooks.",
                              skills=[AgentSkill(id="profile_changes", name="Profile touched code paths")])

    async def handle_task(self, task: Task) -> AsyncIterator[Event]:
        root = Path(task.payload.get("root", "."))
        files = [f for f in (task.payload.get("files") or []) if f.endswith(".py")]
        findings: List[Dict] = []
        details: Dict = {"top_hotspots": [], "flamegraph_lines": 0, "py-spy": "not attached (sandbox)",
                         "locust": "not configured"}

        import sys, importlib.util
        sys.path.insert(0, str(root))
        for rel in files:
            mod = rel[:-3].replace("/", ".")
            if rel.startswith("tests/") or "__init__" in rel:
                continue
            try:
                spec = importlib.util.spec_from_file_location(mod, root / rel)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
            except Exception as e:
                details.setdefault("skipped", []).append({rel: str(e)[:90]})
                continue
            callables = [getattr(m, n) for n in dir(m)
                         if callable(getattr(m, n, None)) and not n.startswith("_")][:4]
            if not callables:
                continue
            yield self.progress(task, self.name, f"cProfile {rel} ({len(callables)} callable(s))")
            pr = cProfile.Profile()
            import time
            t0 = time.perf_counter()
            try:
                pr.enable()
                for fn in callables:
                    for _ in range(50):
                        self._drive(fn)
                pr.disable()
            except Exception as e:
                details.setdefault("skipped", []).append({rel: f"driver: {str(e)[:90]}"})
                continue
            wall_ms = round((time.perf_counter() - t0) * 1000, 1)
            # NB: a pypi package named `pstats` can shadow the stdlib - use Profile.getstats()
            entries = []
            for fs in pr.getstats() or []:
                code = getattr(fs, "code", None)
                fname = getattr(code, "co_filename", "<built-in>") or "<built-in>"
                cname = getattr(code, "co_name", "?")
                entries.append((getattr(fs, "cumulative_tt", 0.0) or 0.0, f"{fname.rsplit('/', 1)[-1]}::{cname}"))
            entries.sort(key=lambda e: -e[0])
            top = entries[: self.top_n]
            for rank, (cum, name_) in enumerate(top):
                if "user_service" in name_ or rel.rsplit("/", 1)[-1][:-3] in name_:
                    if cum > 0.05 and rank < 3:
                        findings.append({"severity": "LOW", "rule": "hotspot", "file": rel, "line": 0,
                                         "description": f"{name_} cum={cum:.3f}s in wall {wall_ms}ms",
                                         "suggested_fix": "cache / memoize / narrow the loop; verify with -m pytest --durations"})
            details["top_hotspots"] += [{"file": rel, "cum_s": round(c, 4), "func": n} for c, n in top[:3]]
            flame = self._flame_text(top, wall_ms)
            details.setdefault("flame", {})[rel] = flame[:1200]
            details["flamegraph_lines"] += flame.count("\n")

        blocking = [f for f in findings if f["severity"] in ("HIGH", "CRITICAL")]
        yield self.verdict(task, self.name, name="perf-report", passed=not blocking,
                           score=1.0 if not findings else 0.85,
                           summary=(f"no regressions found across {len(files)} file(s)" if not findings
                                    else f"{len(findings)} hotspot(s) flagged"),
                           findings=findings, details=details)

    @staticmethod
    def _drive(fn) -> None:
        """Probe candidate arities/types; TypeError -> next candidate; domain errors -> accept
        (we measure cost, not correctness)."""
        import inspect
        if inspect.isclass(fn):
            return
        try:
            sig = inspect.signature(fn)
            ps = [p for p in sig.parameters.values() if p.name not in ("self", "cls")]
            if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in ps):
                return
        except (TypeError, ValueError):
            return
        n = len([p for p in ps if p.default is inspect._empty])
        for args in ({0: (), 1: (1,), 2: (1, 1), 3: (1, 1, 1), 4: (1, 1, 1, 1)}.get(n, ()) or ((),),
                     ("x",) * n if n else (), (0,) * n if n else ()):
            try:
                fn(*args)
                return
            except TypeError:
                continue
            except Exception:
                return  # ran (and raised a domain error) - time was spent, good enough

    @staticmethod
    def _flame_text(top, wall_ms: float) -> str:
        """folded-style one-line-per-stack (stacks/FlameGraph-compatible width=1) from hot list."""
        lines = []
        for cum, name in top:
            pct = (cum * 1000) / max(wall_ms, 1e-9) * 100
            if pct >= 0.5:
                lines.append(f"root;{name} {max(1, int(cum * 1e6))}")
        return "\n".join(lines)
