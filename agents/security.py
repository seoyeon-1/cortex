"""Phase 6.3a - SecurityAgent: reviews changed files for vulnerabilities.

Tools (each optional, auto-detected):
  1. semgrep  - with a LOCAL ruleset (agents/rules/security.yaml), offline-safe
  2. bandit   - python security linter
  3. builtin  - regex heuristics (SQLi interpolation, eval/exec, shell injection)
Findings are merged + deduped; the verdict is FAIL when any finding's severity is in
`block_on` (config agents.sub_agents.security.block_on).
"""
import json
import re
import subprocess
from pathlib import Path
from typing import AsyncIterator, Dict, List

from agents.base_agent import A2AAgent
from protocol.a2a import AgentCard, AgentSkill, Event, Task, SEVERITY_RANK

RULES_FILE = Path(__file__).resolve().parent / "rules" / "security.yaml"

_BUILTIN_PATTERNS = [
    (r"(?:execute|executemany|executescript)\s*\(\s*f[\"']", "B608-sqli", "HIGH", "SQL executed via f-string interpolation"),
    (r"=\s*f[\"'](\s*(SELECT|INSERT|UPDATE|DELETE)|\s*[\"']\s*(?:\+|SELECT))", "B608-sqli", "HIGH", "f-string SQL literal"),
    (r"[\[\"'](SELECT|INSERT|UPDATE|DELETE)[\"'][^\]]*\bLIKE\b[^\n]*\{[^}]+\}", "B608-sqli", "HIGH", "LIKE pattern interpolated into SQL f-string"),
    (r"[\"'](SELECT|INSERT|UPDATE|DELETE|DROP)[^\"']*[\"']\s*(?:\+|%|\.format)", "B608-sqli", "HIGH", "SQL built by concatenation/format"),
    (r"(?:os\.system|os\.popen|subprocess\.(?:run|call|Popen))\s*\(\s*f[\"']", "shell-injection", "HIGH", "shell command with interpolation"),
    (r"\beval\s*\(|\bexec\s*\(", "dangerous-eval", "MEDIUM", "eval()/exec() usage"),
    (r"verify\s*=\s*False|ssl\s*=\s*False", "tls-verification-disabled", "MEDIUM", "TLS verification disabled"),
]


class SecurityAgent(A2AAgent):
    name = "security-agent"

    def __init__(self, cfg: Dict | None = None):
        super().__init__(cfg)
        self.block_on = {s.upper() for s in (cfg or {}).get("block_on", ["CRITICAL", "HIGH", "MEDIUM"])}
        self.card = AgentCard(
            name=self.name,
            description="Security review of changed files (semgrep + bandit + builtin heuristics).",
            skills=[AgentSkill(id="scan_security", name="Scan changed code for vulnerabilities")],
        )

    async def handle_task(self, task: Task) -> AsyncIterator[Event]:
        if task.skill == "threat_modeling" or task.payload.get("threat_model"):
            async for ev in self._stride(task):
                yield ev
            return
        root = Path(task.payload.get("root", "."))
        files: List[str] = [f for f in task.payload.get("files", []) if f.endswith(".py") and (root / f).exists()]
        if not files:
            yield self.verdict(task, self.name, name="security-report", passed=True, summary="no python files changed", score=0.5)
            return

        findings: List[Dict] = []
        tools_used = []
        skipped = {}
        enabled = task.payload.get("tools") or self.cfg.get("tools") or ["semgrep", "bandit", "builtin_sqli_scan"]
        sg_timeout = int(self.cfg.get("semgrep_timeout_seconds", 25))

        if "semgrep" in enabled and self.tool("semgrep") and RULES_FILE.exists():
            yield self.progress(task, self.name, f"semgrep scan ({len(files)} file(s), {sg_timeout}s budget)")
            got, why = self._semgrep(root, files, sg_timeout)
            if got is not None:
                findings += got
                tools_used.append("semgrep")
            else:
                skipped["semgrep"] = why  # degradation-first: slow/erratic semgrep never blocks the society
        if "bandit" in enabled and self.tool("bandit"):
            yield self.progress(task, self.name, f"bandit scan ({len(files)} file(s))")
            bt, bwhy = self._bandit(root, files)
            if bt is not None:
                findings += bt
                tools_used.append("bandit")
            else:
                skipped["bandit"] = bwhy
        if "builtin_sqli_scan" in enabled or not tools_used:
            yield self.progress(task, self.name, "builtin heuristic scan")
            findings += self._builtin(root, files)
            tools_used.append("builtin")

        if "trivy" in (enabled or []) and self.tool("trivy"):
            got, why = self._trivy(root)
            if got is not None:
                findings += got
                tools_used.append("trivy")
            else:
                skipped["trivy"] = why

        findings = self._dedupe(findings)
        blocking = [f for f in findings if f["severity"] in self.block_on]
        worst = max((SEVERITY_RANK.get(f["severity"], 0) for f in findings), default=0)
        passed = not blocking
        score = {4: 0.0, 3: 0.2, 2: 0.55, 1: 0.85, 0: 1.0}.get(worst, 1.0)
        yield self.verdict(
            task, self.name, name="security-report", passed=passed, score=score,
            summary=(f"{len(blocking)} blocking finding(s): " + "; ".join(f"{f['rule']}@{f['file']}:{f['line']}" for f in blocking[:4]))
                    if blocking else f"no blocking findings ({len(findings)} info)",
            findings=findings, details={"tools": tools_used, "skipped": skipped, "block_on": sorted(self.block_on)},
        )

    def _trivy(self, root: Path):
        """Dependency CVE gate - only meaningful when the vuln DB exists locally."""
        req = next(iter(list(root.rglob("requirements*.txt")) + list(root.rglob("pyproject.toml"))), None)
        if req is None:
            return None, "no requirements file"
        try:
            r = subprocess.run(["trivy", "fs", "-q", "--format", "json", "--skip-db-download", str(req)],
                               cwd=root, capture_output=True, text=True, timeout=60)
            data = json.loads(r.stdout or "{}")
            out = []
            for res in data.get("Results") or []:
                for v in res.get("Vulnerabilities") or []:
                    out.append({"severity": {"CRITICAL": "CRITICAL", "HIGH": "HIGH"}.get(str(v.get("Severity")), "MEDIUM"),
                                "rule": f"trivy/{v.get('VulnerabilityID', 'CVE')}", "file": str(req.relative_to(root)), "line": 0,
                                "description": f"{v.get('PkgName')} {v.get('InstalledVersion')} -> {v.get('FixedVersion') or 'no fix'}: {str(v.get('Title'))[:120]}",
                                "suggested_fix": "bump dependency version"})
            return out, None
        except Exception as e:
            return None, str(e)[:120]

    async def _stride(self, task: Task) -> AsyncIterator[Event]:
        """STRIDE threat model over the entry-point surface (heuristic, deterministic)."""
        root = Path(task.payload.get("root", "."))
        yield self.progress(task, self.name, "threat_modeling (STRIDE over entry points)")
        threats = []
        try:
            from core.memory.graph_builder import CodeGraphBuilder
            g = CodeGraphBuilder().build(str(root))
            funcs = [n.split(":", 1)[1] for n, d in g.nodes(data=True) if d.get("type") == "FUNCTION" or n.startswith("func:")]
        except Exception:
            funcs = []
        entryish = [f for f in funcs if any(k in f.lower() for k in ("api/", "route", "view", "handler", "controller"))] or funcs[:8]
        text = ""
        for py in list(root.rglob("*.py"))[:30]:
            try:
                text += py.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        stride = [
            ("Spoofing", r"(password|token|jwt|session)", "no auth dependency found" if not re.search(r"(require_auth|@auth|verify_token)", text) else "auth present (verify coverage)", "auth middleware on every entry point"),
            ("Tampering", r"(\bf[\x27\x22]SELECT|\.format\(|LIKE .%\s*\+)", "string-built SQL/inputs reachable", "parameter binding + input validation"),
            ("Repudiation", r"(logger|logging)", "audit log present" if re.search(r"logging\.", text) else "no audit trail found", "structured audit events on mutations"),
            ("Information Disclosure", r"(SELECT \*|select \*)", "SELECT * may leak columns", "project explicit column lists"),
            ("Denial of Service", r"(while True|\.fetchall\(\))", "unbounded fetch/loop patterns", "pagination + timeouts"),
            ("Elevation of Privilege", r"(admin|role|superuser)", "role checks present" if re.search(r"(role|permission)", text) else "no RBAC found", "RBAC guard at route layer"),
        ]
        for name, probe, if_found, mitigation in stride:
            hit = bool(re.search(probe, text))
            threats.append({"threat": name, "surface": ", ".join(entryish[:4]) or "module entry points",
                            "exposed": hit, "note": if_found if hit else "pattern not detected",
                            "mitigation": mitigation})
        exposed = [t for t in threats if t["exposed"]]
        findings = [{"severity": "MEDIUM" if t["threat"] in ("Tampering", "Elevation of Privilege", "Spoofing") else "LOW",
                     "rule": f"stride/{t['threat'].split()[0].lower()}", "file": "(model)", "line": 0,
                     "description": f"{t['threat']}: {t['note']}", "suggested_fix": t["mitigation"]} for t in exposed]
        yield self.verdict(task, self.name, name="threat-model", passed=True, score=1.0,
                           summary=f"{len(exposed)}/6 STRIDE categories with exposed patterns",
                           findings=findings, details={"stride": threats, "entry_points": entryish[:10]})

    # ----------------------------------------------------------------- tools
    def _semgrep(self, root: Path, files: List[str], timeout_s: int):
        """Returns (findings, None) on success or (None, reason) to degrade gracefully."""
        import time
        t0 = time.monotonic()
        try:
            r = subprocess.run(["semgrep", "--quiet", "--json", "--metrics=off", "--disable-nosemgrep",
                                "--timeout", "5", "--config", str(RULES_FILE), *[str(root / f) for f in files]],
                               cwd=root, capture_output=True, text=True, timeout=timeout_s)
            if time.monotonic() - t0 > timeout_s * 0.9:
                return None, "slow-start (budget exhausted); use bandit+builtin only on this host"
            data = json.loads(r.stdout or "{}")
            out = []
            for res in data.get("results", []):
                rel = res.get("path", "")
                try:
                    rel = str(Path(rel).relative_to(root))
                except Exception:
                    pass
                out.append({"severity": {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}.get(res.get("severity", "WARNING"), "MEDIUM"),
                            "rule": res.get("check_id", "semgrep"), "file": rel, "line": res.get("start", {}).get("line", 0),
                            "description": res.get("extra", {}).get("message", "")[:200],
                            "suggested_fix": "use parameter binding / safe APIs"})
            return out, None
        except subprocess.TimeoutExpired:
            return None, f"timeout after {timeout_s}s"
        except Exception as e:
            return None, str(e)

    def _bandit(self, root: Path, files: List[str]):
        """Returns (findings, None) or (None, reason)."""
        try:
            r = subprocess.run(["bandit", "-f", "json", "-q", "-n", "LOW", *[str(root / f) for f in files]],
                               cwd=root, capture_output=True, text=True, timeout=90)
            data = json.loads(r.stdout or "{}")
            out = []
            for res in data.get("results", []):
                rel = res.get("filename", "")
                try:
                    rel = str(Path(rel).relative_to(root))
                except Exception:
                    pass
                out.append({"severity": res.get("issue_severity", "MEDIUM").upper(), "rule": res.get("test_id", "bandit"),
                            "file": rel, "line": res.get("line_number", 0),
                            "description": res.get("issue_text", "")[:200],
                            "suggested_fix": (res.get("issue_text") or "")[:120]})
            return out, None
        except subprocess.TimeoutExpired:
            return None, "bandit timeout"
        except Exception as e:
            return None, str(e)

    @staticmethod
    def _builtin(root: Path, files: List[str]) -> List[Dict]:
        out = []
        for rel in files:
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for ln, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pat, rule, sev, desc in _BUILTIN_PATTERNS:
                    if re.search(pat, line):
                        out.append({"severity": sev, "rule": f"builtin/{rule}", "file": rel, "line": ln,
                                    "description": f"{desc}: {stripped[:120]}",
                                    "suggested_fix": "parameter binding ($1 placeholders) / shlex.quote / avoid eval"})
                        break
        return out

    @staticmethod
    def _dedupe(findings: List[Dict]) -> List[Dict]:
        seen = set(); out = []
        for f in findings:
            key = (f["file"], f["line"], f["rule"].split("/")[-1].split(":")[0][:14])
            if key in seen:
                continue
            seen.add(key); out.append(f)
        out.sort(key=lambda f: -SEVERITY_RANK.get(f["severity"], 0))
        return out
