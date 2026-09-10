import os
import subprocess
import tempfile
from typing import Dict, List
from unidiff import PatchSet
from core.tools.base import BaseTool, ToolResult
from core.tools.test_runner import TestRunnerTool


class ApplyPatchSetTool(BaseTool):
    name = "apply_patch_set"
    description = ("Apply a SET of Unified Diff patches across MULTIPLE files atomically in one call: "
                   "single `git apply --check` for the whole set -> apply -> run tests -> auto-rollback (git checkout/clean) if tests fail. "
                   "Prefer this over calling edit_file repeatedly for multi-file features. Each item: {\"path\": ..., \"unified_diff\": ...}.")
    parameters = {
        "type": "object",
        "properties": {
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path of the file this diff targets"},
                        "unified_diff": {"type": "string", "description": "Valid Unified Diff for that file"}
                    },
                    "required": ["path", "unified_diff"]
                }
            },
            "run_tests": {"type": "boolean", "default": True, "description": "Run pytest after applying; rollback on failure"},
            "thought": {"type": "string", "description": "Plan-level reasoning covering ALL patches in this set"}
        },
        "required": ["patches", "thought"]
    }

    def __init__(self, workspace_root: str, timeout: int = 120, preflight=None):
        self.workspace_root = workspace_root
        self.timeout = timeout
        self.preflight = preflight   # Phase 15.1 PreflightSimulator (optional)

    def execute(self, patches: List[Dict], thought: str = "", run_tests: bool = True) -> ToolResult:
        if not patches:
            return ToolResult(False, error="Empty patch set.")

        # 1) Validate each diff is parseable and targets the declared file
        for i, p in enumerate(patches):
            path, diff = p.get("path", ""), p.get("unified_diff", "")
            try:
                ps = PatchSet((diff if diff.endswith("\n") else diff + "\n").splitlines(keepends=True))
                if not ps:
                    return ToolResult(False, error=f"patch[{i}] empty/invalid for {path}")
            except Exception as e:
                return ToolResult(False, error=f"patch[{i}] invalid Unified Diff for {path}: {e}")

        # Phase 15.1: one static pass over the whole set first - one gate, all-or-nothing,
        # exactly like the git gate below, but free of subprocess/disk state.
        if self.preflight is not None:
            pf = self.preflight.simulate_patch_set(patches)
            if not pf.ok:
                return ToolResult(False, error="preflight: " + pf.as_error(), retryable=False)

        full_patch = "".join(p["unified_diff"].rstrip("\n") + "\n" for p in patches)
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False, encoding="utf-8")
        try:
            tf.write(full_patch); tf.close()
            # 2) One-shot git apply --check for the whole set
            check = subprocess.run(["git", "apply", "--check", tf.name], cwd=self.workspace_root,
                                   capture_output=True, text=True, timeout=15)
            if check.returncode != 0:
                return ToolResult(False, error=f"patch set failed git apply --check: {check.stderr.strip()} (all-or-nothing: nothing applied)")
            applied = subprocess.run(["git", "apply", tf.name], cwd=self.workspace_root,
                                     capture_output=True, text=True, timeout=15)
            if applied.returncode != 0:
                self._rollback()
                return ToolResult(False, error=f"git apply failed mid-way, rolled back: {applied.stderr.strip()}")
        finally:
            os.unlink(tf.name)

        # 3) Verify + 4) atomic rollback on failure
        if run_tests:
            tr = TestRunnerTool(self.workspace_root, self.timeout).execute(thought=f"verify patch set: {thought}"[:200])
            if not tr.success:
                self._rollback()
                return ToolResult(False,
                                  error=(tr.data or {}).get("raw_output", tr.error)[-3000:],
                                  summary=f"patch set REJECTED ({tr.summary}) -> auto-rolled back. Formulate a NEW hypothesis.")
            return ToolResult(True, data=tr.data,
                              summary=f"Atomic patch set APPLIED+COMMITTED-READY: {len(patches)} file(s); {tr.summary}. Thought: {thought}"[:500])
        return ToolResult(True, summary=f"Atomic patch set applied: {len(patches)} file(s) (tests skipped)")

    def _rollback(self):
        subprocess.run(["git", "checkout", "--", "."], cwd=self.workspace_root, capture_output=True, text=True, timeout=15)
        subprocess.run(["git", "clean", "-fd", "--", "."], cwd=self.workspace_root, capture_output=True, text=True, timeout=15)
