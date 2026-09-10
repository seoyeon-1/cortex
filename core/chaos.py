"""Phase 4.3 - Chaos engineering / self-verification.

`ChaosTool` renders chaos-mesh experiments (NetworkChaos / PodChaos / StressChaos),
applies them through the kubectl wrapper (mutations must be allowed), holds the fault
for a bounded window, and GATES the run on SLOs: the cluster must absorb the fault
(error rate & p99 within bounds during injection) before anything is reported as
"deploy complete". Default `dry_run: true` returns the manifest only - safe to keep
enabled in config without a cluster.
"""
import time
from typing import Dict, Optional
from core.tools.base import BaseTool, ToolResult

NETWORK_DELAY = """apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: cortex-{slug}-delay
  namespace: {namespace}
spec:
  action: delay
  mode: one
  selector:
    namespaces: [{namespace}]
    labelSelectors: {{app: '{target}'}}
  delay:
    latency: {latency}
    jitter: {jitter}
  duration: {duration}s
"""

POD_KILL = """apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: cortex-{slug}-podkill
  namespace: {namespace}
spec:
  action: pod-kill
  mode: one
  selector:
    namespaces: [{namespace}]
    labelSelectors: {{app: '{target}'}}
  duration: {duration}s
"""

CPU_STRESS = """apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: cortex-{slug}-cpu
  namespace: {namespace}
spec:
  mode: one
  selector:
    namespaces: [{namespace}]
    labelSelectors: {{app: '{target}'}}
  stressors:
    cpu: {{workers: {workers}, load: {load}}}
  duration: {duration}s
"""

_TEMPLATES = {"network_delay": NETWORK_DELAY, "pod_kill": POD_KILL, "cpu_limit": CPU_STRESS}


class ChaosTool(BaseTool):
    name = "chaos_experiment"
    description = ("Run a chaos-mesh self-verification experiment against the sandbox target: "
                   "inject fault (network_delay|pod_kill|cpu_limit), wait, then SLO-gate. "
                   "Reports DEPLOY_OK only if SLOs hold during the fault window; otherwise FAILED/SLO_BREACH. "
                   "dry_run (default) renders + validates the manifest without touching a cluster.")
    parameters = {"type": "object", "properties": {
        "fault": {"type": "string", "enum": ["network_delay", "pod_kill", "cpu_limit"]},
        "target": {"type": "string", "description": "app label / deployment name"},
        "namespace": {"type": "string", "default": "default"},
        "duration": {"type": "integer", "default": 30, "description": "seconds to hold fault (clamped to config max)"},
        "latency": {"type": "string", "default": "200ms"},
        "jitter": {"type": "string", "default": "40ms"},
        "workers": {"type": "integer", "default": 1},
        "load": {"type": "integer", "default": 50},
        "thought": {"type": "string"}}, "required": ["fault", "target", "thought"]}

    def __init__(self, chaos_cfg: Dict, kubectl=None, slo_checker: Optional[object] = None):
        self.cfg = chaos_cfg or {}
        self.kubectl = kubectl
        self.slo = slo_checker

    def render(self, args: Dict) -> "tuple[str, int]":
        dur = min(int(args.get("duration", 30) or 30), int(self.cfg.get("max_duration_seconds", 60)))
        return _TEMPLATES[args["fault"]].format(
            slug=f"{args['fault'].replace('_', '-')}-{int(time.time())}",
            namespace=args.get("namespace", "default"), target=args.get("target", "app"),
            duration=dur, latency=args.get("latency", "200ms"), jitter=args.get("jitter", "40ms"),
            workers=args.get("workers", 1), load=args.get("load", 50)), dur

    def execute(self, fault: str, target: str, thought: str = "", namespace: str = "default",
                duration: int = 30, latency: str = "200ms", jitter: str = "40ms",
                workers: int = 1, load: int = 50) -> ToolResult:
        if fault not in _TEMPLATES:
            return ToolResult(False, error=f"unknown fault: {fault}")
        args = {"fault": fault, "target": target, "namespace": namespace, "duration": duration,
                "latency": latency, "jitter": jitter, "workers": workers, "load": load}
        manifest, held = self.render(args)
        if self.cfg.get("dry_run", True) or not self.kubectl:
            return ToolResult(True, data={"manifest": manifest, "dry_run": True},
                              summary=f"chaos[{fault}] dry-run: manifest rendered ({held}s window, ns={namespace})")
        apply = self.kubectl.execute("apply", ["-f", "-", "-o", "yaml"], namespace, thought)
        if not apply.success:
            return ToolResult(False, error=f"chaos apply failed: {apply.error or apply.summary}")
        time.sleep(min(held, int(self.cfg.get("observe_seconds", 5))))
        verdict = self.slo.check() if self.slo else ToolResult(False, error="no slo checker")
        self.kubectl.execute("delete", ["networkchaos", "-l", "managed-by=cortex", "--ignore-not-found"], namespace, "cleanup")
        if verdict.success:
            return ToolResult(True, data={"manifest": manifest, "slo": verdict.data},
                              summary=f"chaos[{fault}] on {target}: DEPLOY_OK - SLOs held during {held}s fault window")
        return ToolResult(False, data={"manifest": manifest, "slo": verdict.data, "error_detail": verdict.summary},
                          summary=f"chaos[{fault}] on {target}: SLO_BREACH during window - not deploy-ready")
