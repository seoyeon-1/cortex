"""Phase 10.3 - Guardrails: injection sanitize + tool whitelist + capability separation.

Two modes (config guardrails.mode):
  mask  (default, availability-first): injection spans are redacted in place and flagged -
        the run continues with defanged text, every hit journaled to security.jsonl.
  block (security-first): SecurityViolationError aborts the task immediately.

Tool validation is always hard: whitelist (capability-based), patch-size ceiling, sensitive
path deny-list, shell/network capability flags. A rejected tool call is fed back to the LLM
as a failed result (the loop learns "that tool/path is not in your capability set").
"""
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from pydantic import BaseModel, Field

INJECTION_PATTERNS = [
    r"ignore (all )?previous instructions",
    r"disregard (the )?system prompt",
    r"reveal (your )?system prompt",
    r"you are now",
    r"act as (root|admin|an unrestricted)",
    r"bypass (the )?(safety|guard|filter|policy)",
    r"sudo\s+rm",
    r"rm\s+-rf\s+/",
    r"chmod\s+777\s+/",
    r"curl\s+\S+\s*\|\s*(ba)?sh",
    r"wget\s+\S+\s*-O-\s*\|\s*(ba)?sh",
    r"base64\s+(-d|--decode)",
    r"</?\s*system\s*>",
]


class SecurityPolicy(BaseModel):
    allowed_tools: Set[str] = Field(default_factory=lambda: {
        "read_file", "list_files", "edit_file", "run_tests", "search_code", "apply_patch_set"})
    denied_commands: List[str] = Field(default_factory=lambda: [
        "rm", "dd", "mkfs", "shutdown", "reboot", "curl", "wget", "nc"])
    max_file_size_mb: int = 10
    max_patch_kb: int = 512
    allow_network: bool = False
    allow_shell: bool = False
    deny_paths: List[str] = Field(default_factory=lambda: [
        ".ssh", ".aws", ".git/config", ".git-credentials", "id_rsa", ".env",
        "password", "secret", "credential", "/etc/", ".kube/config"])
    mode: str = "mask"                       # mask | block


class SecurityViolationError(Exception):
    pass


class Guardrails:
    def __init__(self, policy: Optional[SecurityPolicy] = None, journal: Optional[Path] = None):
        self.policy = policy or SecurityPolicy()
        self.compiled = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
        self.journal = journal

    # ------------------------------------------------------------------ api
    def sanitize_input(self, text: str, where: str = "task") -> str:
        """mask mode defangs & records; block mode raises. Never silently passes through."""
        hits = [p.pattern for p in self.compiled if p.search(text)]
        if not hits:
            return text
        self._log({"ts": round(time.time(), 1), "where": where, "patterns": hits, "mode": self.policy.mode,
                   "snippet": text[:160]})
        if self.policy.mode == "block":
            raise SecurityViolationError(f"Detected injection pattern: {hits[0]}")
        out = text
        for p in self.compiled:
            out = p.sub(lambda m: "[REDACTED-INJECTION]", out)
        return out

    def validate_tool_call(self, tool_name: str, args: dict) -> bool:
        if tool_name not in self.policy.allowed_tools:
            raise SecurityViolationError(f"Tool '{tool_name}' not allowed by policy.")
        if tool_name in ("edit_file", "apply_patch_set"):
            patches = args.get("patches") or ([{"path": args.get("path", ""), "unified_diff": args.get("unified_diff", "")}]
                                              if "path" in args else [])
            total = 0
            for p in patches:
                path = str(p.get("path", ""))
                if self._sensitive(path):
                    raise SecurityViolationError(f"Access to sensitive path denied: {path}")
                d = str(p.get("unified_diff", ""))
                total += len(d)
                # the file the patch targets is the second half of its own truth
                for tgt in re.findall(r"^\+\+\+ b/(.+)$", d, re.M):
                    if self._sensitive(tgt.strip()):
                        raise SecurityViolationError(f"Patch targets sensitive path: {tgt.strip()}")
            if total > self.policy.max_patch_kb * 1024:
                raise SecurityViolationError(f"Patch size {total}B exceeds {self.policy.max_patch_kb}KB limit.")
        return True

    def validate_sandbox_command(self, cmd: List[str]) -> bool:
        if not cmd:
            return False
        base = Path(str(cmd[0])).name
        if base in self.policy.denied_commands and not (self.policy.allow_shell and base in ("curl", "wget")):
            raise SecurityViolationError(f"Command denied by capability policy: {base}")
        return True

    def allow_extra_tools(self, names: List[str]) -> None:
        self.policy.allowed_tools |= set(names)

    # ---------------------------------------------------------------- util
    def _sensitive(self, path: str) -> bool:
        low = path.lower()
        return any(d.lower().lstrip("/") in low for d in self.policy.deny_paths if d.strip("/"))

    def _log(self, rec: dict) -> None:
        if not self.journal:
            return
        try:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
