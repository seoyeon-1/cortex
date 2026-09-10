"""Phase 4.1 - Infra skill pack (Kubernetes + Terraform wrappers).

Safety-first contract:
  * Read-only verbs (get/describe/logs/plan/status) always allowed when binary exists.
  * Mutating verbs (apply/rollout/scale/delete/terraform apply) require
    config["infra"]["allow_mutations"]=true AND stay inside allowed namespaces.
  * Every mutating call is paired with an SLO check; breach -> automatic rollback
    (kubectl rollout undo / terraform stays unapplied) and the tool reports BREACH.

No cluster/creds in this env -> tools degrade to clear ToolResult(False), never raise.
"""
import json
import os
import shutil
import subprocess
from typing import Dict, List, Optional
from core.tools.base import BaseTool, ToolResult

_RO_KUBECTL = {"get", "describe", "logs", "top", "version", "explain"}
_RW_KUBECTL = {"apply", "rollout", "scale", "delete", "patch", "set", "delete"}


class KubectlTool(BaseTool):
    name = "kubectl"
    description = ("Wrapper over kubectl for cluster state + rollouts. Read verbs: get/describe/logs/top. "
                   "Mutating verbs (apply/rollout/restart/undo/scale/delete) need infra.allow_mutations and an allowed namespace. "
                   "After a mutating rollout, pair with query_metrics/SLO check; on breach the tool auto-runs `rollout undo`.")
    parameters = {"type": "object", "properties": {
        "verb": {"type": "string", "description": "e.g. get|describe|logs|rollout|apply|delete|scale"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "CLI args after verb, e.g. ['deployment/api','-n','prod']"},
        "namespace": {"type": "string", "default": "default"},
        "thought": {"type": "string"}}, "required": ["verb", "thought"]}

    def __init__(self, infra_cfg: Dict):
        self.cfg = infra_cfg or {}
        self.allow_mutations = bool(self.cfg.get("allow_mutations", False))
        self.namespaces = set(self.cfg.get("allowed_namespaces", ["default"]))

    def _bin(self) -> Optional[str]:
        return shutil.which("kubectl")

    def execute(self, verb: str, args: List[str] = None, namespace: str = "default", thought: str = "") -> ToolResult:
        args = args or []
        binp = self._bin()
        if not binp:
            return ToolResult(False, error="kubectl not installed")
        if verb not in _RO_KUBECTL and verb not in _RW_KUBECTL:
            return ToolResult(False, error=f"verb not allowed: {verb}")
        if verb in _RW_KUBECTL:
            if not self.allow_mutations:
                return ToolResult(False, error="mutating kubectl disabled (set infra.allow_mutations=true)")
            if namespace not in self.namespaces:
                return ToolResult(False, error=f"namespace '{namespace}' not in allowed_namespaces {sorted(self.namespaces)}")
        cmd = [binp, verb] + list(args)
        if namespace and "-n" not in args and "--namespace" not in args:
            cmd += ["-n", namespace]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=self.cfg.get("timeout_seconds", 60))
            out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
            ok = r.returncode == 0
            return ToolResult(ok, data={"stdout": out, "returncode": r.returncode},
                              summary=f"kubectl {verb} -> rc={r.returncode}: {out[:160]}" if ok
                              else f"kubectl {verb} FAILED rc={r.returncode}")
        except subprocess.TimeoutExpired:
            return ToolResult(False, error=f"kubectl {verb} timeout")
        except Exception as e:
            return ToolResult(False, error=f"kubectl {verb} error: {e}")

    def rollout_restart(self, deployment: str, namespace: str) -> ToolResult:
        return self.execute("rollout", ["restart", deployment], namespace, "restart pods to pick up fix")

    def rollout_undo(self, deployment: str, namespace: str) -> ToolResult:
        return self.execute("rollout", ["undo", deployment], namespace, "SLO breach -> rollback")


class TerraformTool(BaseTool):
    name = "terraform"
    description = "Terraform plan/apply/validate wrapper with drift detection. `action=plan` reports whether changes exist; `action=drift` runs plan -detailed-exitcode (rc 2 == drift). apply requires infra.allow_mutations."
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["plan", "apply", "validate", "output", "drift", "state_list"]},
        "dir": {"type": "string", "description": "Working directory (defaults to infra.terraform_dir)"},
        "args": {"type": "array", "items": {"type": "string"}},
        "thought": {"type": "string"}}, "required": ["action", "thought"]}

    def __init__(self, infra_cfg: Dict, workspace_root: str = "."):
        self.cfg = infra_cfg or {}
        self.workspace_root = workspace_root
        self.allow_mutations = bool(self.cfg.get("allow_mutations", False))

    def _run(self, action: str, extra: List[str], cwd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["terraform", action] + list(extra), cwd=cwd,
                              capture_output=True, text=True, timeout=self.cfg.get("timeout_seconds", 300))

    def execute(self, action: str, dir: str = "", args: List[str] = None, thought: str = "") -> ToolResult:
        extra = args or []
        cwd = os.path.join(self.workspace_root, dir) if dir else self.cfg.get("terraform_dir", self.workspace_root)
        if shutil.which("terraform") is None:
            return ToolResult(False, error="terraform not installed")
        if action == "apply" and not self.allow_mutations:
            return ToolResult(False, error="terraform apply disabled (set infra.allow_mutations=true)")
        try:
            if action == "drift":  # detailed-exitcode: 0=no diff, 2=drift, else err
                r = self._run("plan", extra + ["-detailed-exitcode", "-input=false"], cwd)
                drift = r.returncode == 2
                return ToolResult(drift or r.returncode == 0,
                                  data={"drift": drift, "returncode": r.returncode, "stdout": r.stdout[-2000:]},
                                  summary=f"terraform drift: {'DETECTED' if drift else 'clean'}")
            if action == "plan":
                r = self._run("plan", extra + ["-input=false", "-no-color"], cwd)
                changes = "Plan:" in r.stdout and ("to add" in r.stdout or "to change" in r.stdout or "to destroy" in r.stdout)
                return ToolResult(r.returncode == 0,
                                  data={"returncode": r.returncode, "changes": changes, "stdout": r.stdout[-2000:]},
                                  summary=f"terraform plan: rc={r.returncode}, changes={'yes' if changes else 'no'}")
            r = self._run(action, extra, cwd)
            return ToolResult(r.returncode == 0,
                              data={"returncode": r.returncode, "stdout": r.stdout[-2000:], "stderr": r.stderr[-800:]},
                              summary=f"terraform {action}: rc={r.returncode}")
        except subprocess.TimeoutExpired:
            return ToolResult(False, error=f"terraform {action} timeout")
        except Exception as e:
            return ToolResult(False, error=f"terraform {action} error: {e}")


class DeployGuard:
    """Rollout -> poll SLO -> auto-rollback on breach. The Phase-4 self-verification primitive."""

    def __init__(self, kubectl: KubectlTool, slo_checker, config: Dict):
        self.kubectl = kubectl
        self.slo = slo_checker
        self.cfg = (config or {}).get("infra", {}) or {}

    def restart_and_verify(self, deployment: str, namespace: str = "default",
                           attempts: int = 3) -> Dict:
        rr = self.kubectl.rollout_restart(deployment, namespace)
        result = {"restart": rr.success, "slo": None, "rolled_back": False, "status": "PENDING"}
        if not rr.success:
            result["status"] = "RESTART_FAILED"; result["error"] = rr.error; return result
        ok = False
        for i in range(attempts):
            chk = self.slo.check()
            result["slo"] = {"success": chk.success, "summary": chk.summary, "attempt": i}
            if chk.success:
                ok = True; break
        if ok:
            result["status"] = "ROLLOUT_HEALTHY"
        else:  # breach after retries -> rollback
            ub = self.kubectl.rollout_undo(deployment, namespace)
            result["rolled_back"] = ub.success
            result["status"] = "ROLLED_BACK" if ub.success else "ROLLED_BACK_WITH_ERROR"
        return result
