"""Phase 6.5a - AgentManifest + Registry: package/deploy/version full sub-agents.

Manifest YAML (registry/agents/<key>.yaml) is the "listing" in the marketplace:
  name, key, version (semver), persona, entrypoint "module:Class", capabilities[], tools[],
  validation (human-readable bar), routing {always, keywords[]}, config_defaults{}, status.

Registry = load/resolve/instantiate/publish. `resolve` does semver-latest. Publishing a new
version never breaks consumers: old manifests stay in the dir, `latest` wins.
"""
import importlib
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def _vt(v: str):
    parts = (v.split(".") + ["0"])[:3]
    return tuple(int(p) for p in parts)


class Routing(BaseModel):
    always: bool = False                 # run on every patch set (e.g. security/qa)
    keywords: List[str] = []             # else: run only when task/files hit these
    match_files: bool = True             # file paths also participate in keyword match


class AgentManifest(BaseModel):
    name: str                            # display / A2A agent name (e.g. "security-agent")
    key: str                             # short registry key ("security")
    version: str = "1.0.0"
    persona: str = ""                    # "🔒 Security Engineer"
    description: str = ""
    entrypoint: str                      # "agents.security:SecurityAgent"
    capabilities: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    validation: str = ""                 # the bar this persona enforces
    routing: Routing = Routing()
    config_defaults: Dict = Field(default_factory=dict)
    status: str = "stable"               # stable | experimental | deprecated

    @field_validator("version")
    @classmethod
    def _semver(cls, v):
        if not _VERSION_RE.match(v):
            raise ValueError(f"version must look like 1.2 / 1.2.3 (got {v!r})")
        return v

    @field_validator("entrypoint")
    @classmethod
    def _entry(cls, v):
        if ":" not in v:
            raise ValueError("entrypoint must be 'module:Class'")
        return v

    def load_class(self):
        mod, cls = self.entrypoint.split(":", 1)
        return getattr(importlib.import_module(mod), cls)

    def has_capability(self, cap: str) -> bool:
        cap = cap.lower()
        return any(cap == c.lower() or cap in c.lower() or c.lower() in cap for c in self.capabilities)

    def route_hit(self, task: str, files: Optional[List[str]] = None) -> bool:
        if self.routing.always:
            return True
        hay = (task or "").lower() + " " + " ".join((files or [])).lower()
        return any(k.lower() in hay for k in self.routing.keywords)


class Registry:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.dir = self.root / "agents"
        self._items: List[AgentManifest] = []

    # ------------------------------------------------------------------ ops
    def load(self) -> "Registry":
        self._items = []
        for p in sorted(self.dir.glob("*.yaml")):
            try:
                self._items.append(AgentManifest(**yaml.safe_load(p.read_text(encoding="utf-8"))))
            except Exception as e:  # a broken listing never bricks the marketplace
                print(f"[registry] skipping {p.name}: {e}")
        return self

    def all(self, include_deprecated: bool = False) -> List[AgentManifest]:
        out = [m for m in self._items if include_deprecated or m.status != "deprecated"]
        return sorted(out, key=lambda m: m.key)

    def latest(self, key: str) -> Optional[AgentManifest]:
        vers = [m for m in self._items if m.key == key and m.status != "deprecated"]
        return max(vers, key=lambda m: _vt(m.version)) if vers else None

    def resolve(self, query: str) -> Optional[AgentManifest]:
        """by key, full name, or a capability string ('kubernetes' -> k8s persona)."""
        m = self.latest(query) or next((x for x in self.all() if x.name == query), None)
        if m:
            return m
        by_ver = sorted(self.all(), key=lambda v: _vt(v.version), reverse=True)
        return next((x for x in by_ver if x.has_capability(query)), None)

    def instantiate(self, m: AgentManifest, cfg_override: Optional[Dict] = None):
        """Build the live agent: config_defaults merged under runtime override."""
        cls = m.load_class()
        cfg = {**(m.config_defaults or {}), **(cfg_override or {})}
        agent = cls(cfg)
        agent.manifest = m  # consumed by orchestrator for version-pinned synthesis
        return agent

    def publish(self, manifest: Dict) -> Path:
        m = AgentManifest(**manifest)  # validates before landing in the marketplace
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{m.key}.yaml"
        m.model_dump()
        path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
        self.load()
        return path

    def catalog(self) -> List[Dict]:
        return [{"key": m.key, "name": m.name, "version": m.version, "persona": m.persona,
                 "capabilities": m.capabilities, "tools": m.tools, "validation": m.validation,
                 "status": m.status} for m in self.all()]
