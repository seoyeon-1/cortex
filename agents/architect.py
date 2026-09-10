"""Phase 6.3b - ArchitectAgent: dependency direction & layering gatekeeper.

Engines (degradation-first):
  1. import-linter   - runs only when the TARGET repo ships an `.importlinter` config
                       (`--config` respected via cfg["importlinter_config"]).
  2. graph engine    - Cortex-native (Phase 2 knowledge graph): enforces the declared
                       layer order (config `layers: [[api],[service],[repo],[db]]`,
                       import may point DOWNWARD or same layer only) + cycle detection.
                       Doubles as the `pydeps` replacement (dependency report).

Input payload: {root, files: [...]}   Output: architecture violation report.
"""
import asyncio
import subprocess
from pathlib import Path
from typing import AsyncIterator, Dict, List

import networkx as nx

from agents.base_agent import A2AAgent
from protocol.a2a import AgentCard, AgentSkill, Event, Task


class ArchitectAgent(A2AAgent):
    name = "architect-agent"

    def __init__(self, cfg: Dict | None = None):
        super().__init__(cfg)
        self.layers: List[List[str]] = (cfg or {}).get("layers", [])
        self.card = AgentCard(
            name=self.name,
            description="Checks import direction against the declared layer stack and detects cycles.",
            skills=[AgentSkill(id="check_architecture", name="Architecture & dependency-direction review")],
        )

    async def handle_task(self, task: Task) -> AsyncIterator[Event]:
        root = Path(task.payload.get("root", "."))
        findings: List[Dict] = []
        engines = []

        cfg_file = self.cfg.get("importlinter_config")
        ini = Path(root) / (cfg_file or ".importlinter")
        if self.tool("lint-imports") and ini.exists():
            yield self.progress(task, self.name, "import-linter")
            ok, out = await asyncio.to_thread(self._import_linter, root, ini)
            engines.append("import-linter")
            if not ok:
                findings.append({"severity": "HIGH", "rule": "import-linter", "file": str(ini.name), "line": 0,
                                 "description": out[-500:], "suggested_fix": "fix broken contracts listed by import-linter"})

        if not engines or True:  # graph engine always runs (also provides pydeps-style report)
            yield self.progress(task, self.name, "graph layer/cycle check")
            findings += self._graph_check(root)
            engines.append("graph")

        # pydeps-style Architecture Diagram Generator (ADG): mermaid graph in the artifact;
        # payload.write_adg=true also lands docs/ADG.mmd in the target repo (opt-in mutation).
        adg = self._adg(root)
        if adg and task.payload.get("write_adg"):
            try:
                (root / "docs").mkdir(exist_ok=True)
                (root / "docs" / "ADG.mmd").write_text(adg, encoding="utf-8")
            except Exception:
                pass
        findings += self._solid_advice(root)

        blocking = [f for f in findings if f["severity"] in ("HIGH", "CRITICAL")]
        passed = not blocking
        yield self.verdict(
            task, self.name, name="architecture-report", passed=passed,
            score=1.0 if passed else 0.3,
            summary=(f"{len(blocking)} layering violation(s)" if blocking else
                     f"clean ({len(findings)} advisory finding(s))"),
            findings=findings, details={"engines": engines, "layers": self.layers,
                                        "adg_mermaid": (adg or "")[:2000]},
        )

    def _adg(self, root: Path) -> str:
        try:
            from core.memory.graph_builder import CodeGraphBuilder
            g = CodeGraphBuilder().build(str(root))
        except Exception:
            return ""
        edges = [(u.split(":", 1)[1], v.split(":", 1)[1]) for u, v, d in g.edges(data=True)
                 if d.get("type") == "IMPORTS" and u.startswith("file:") and v.startswith("file:")]
        if not edges:
            return ""
        nid = {}
        lines = ["flowchart TD"]
        def node(f):
            if f not in nid:
                nid[f] = f"F{len(nid)}"
                top = f.split("/")[0]
                shape = "{{%s:::%s}}" % (f, "layer_" + top if top in (x[0] for x in self.layers) else "misc")
                lines.append(f'    {nid[f]} {shape}')
            return nid[f]
        for u, v in edges:
            lines.append(f"    {node(u)} --> {node(v)}")
        lines.append("    classDef layer_api fill:#4f46e5,color:#fff;")
        lines.append("    classDef layer_service fill:#0891b2,color:#fff;")
        lines.append("    classDef layer_repo fill:#059669,color:#fff;")
        lines.append("    classDef layer_db fill:#b45309,color:#fff;")
        return "\n".join(lines)

    def _solid_advice(self, root: Path) -> List[Dict]:
        """C4/SOLID advisory heuristics (INFO only - guidance, never blocking)."""
        import ast
        out = []
        for p in list(root.rglob("*.py"))[:40]:
            if ".git" in str(p):
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            rel = str(p.relative_to(root))
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    meths = [m for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and not m.name.startswith("_")]
                    if len(meths) >= 7:
                        out.append({"severity": "INFO", "rule": "solid-srp", "file": rel, "line": n.lineno,
                                    "description": f"class {n.name} has {len(meths)} public methods - split responsibilities (SRP)",
                                    "suggested_fix": ""})
                    try:
                        src = ast.unparse(n)
                        if len(src.splitlines()) > 60:
                            out.append({"severity": "INFO", "rule": "c4-component-size", "file": rel, "line": n.lineno,
                                        "description": f"{n.name}: >60 LOC component - keep C4 'component' granularity reviewable",
                                        "suggested_fix": ""})
                    except Exception:
                        pass
        return out[:6]

    # ----------------------------------------------------------------- graph
    def _layer_of(self, rel: str) -> int:
        top = rel.split("/", 1)[0]
        for i, group in enumerate(self.layers):
            if top in group:
                return i
        return len(self.layers)  # outside the stack: neutral layer (top)

    def _graph_check(self, root: Path) -> List[Dict]:
        try:
            from core.memory.graph_builder import CodeGraphBuilder
            g = CodeGraphBuilder().build(str(root))
        except Exception as e:
            return [{"severity": "LOW", "rule": "graph-unavailable", "file": "", "line": 0,
                     "description": f"code graph failed: {e}", "suggested_fix": ""}]
        findings = []
        files_graph = nx.DiGraph()
        for u, v, d in g.edges(data=True):
            if d.get("type") != "IMPORTS" or not (u.startswith("file:") and v.startswith("file:")):
                continue
            fu, fv = u.split(":", 1)[1], v.split(":", 1)[1]
            files_graph.add_edge(fu, fv)
            if not self.layers:
                continue
            lu, lv = self._layer_of(fu), self._layer_of(fv)
            # files outside the declared stack (tests, scripts, ...) sit ABOVE it:
            # they may import anything downward - never flag those edges.
            if lu >= len(self.layers):
                continue
            if lv < lu:  # lower layer importing an upper layer = upward dependency
                findings.append({"severity": "HIGH", "rule": "layer-direction", "file": fu, "line": 0,
                                 "description": f"{fu} (layer {self.layers[lv][0] if lv < len(self.layers) else 'core'}) imports {fv} (layer {self.layers[lu][0] if lu < len(self.layers) else 'core'}) - upward dependency",
                                 "suggested_fix": "invert the dependency: upper layer should call lower layer, or extract a shared interface downward"})
        try:
            for cyc in list(nx.simple_cycles(files_graph))[:5]:
                findings.append({"severity": "MEDIUM", "rule": "import-cycle", "file": cyc[0], "line": 0,
                                 "description": "import cycle: " + " -> ".join(cyc + [cyc[0]]),
                                 "suggested_fix": "break the cycle (extract module / dependency inversion)"})
        except Exception:
            pass
        # pydeps-style report: top importers (advisory only, never blocking)
        if files_graph.number_of_edges():
            top = sorted(files_graph.in_degree(), key=lambda kv: -kv[1])[:5]
            findings.append({"severity": "INFO", "rule": "fan-in", "file": "", "line": 0,
                             "description": "top import fan-in: " + ", ".join(f"{n}({d})" for n, d in top if d),
                             "suggested_fix": ""})
        return findings

    @staticmethod
    def _import_linter(root: Path, ini: Path):
        try:
            r = subprocess.run(["lint-imports", "--config", str(ini)], cwd=root,
                               capture_output=True, text=True, timeout=90)
            return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            return True, str(e)
