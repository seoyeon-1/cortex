import os
import tempfile
import subprocess
from unidiff import PatchSet
from core.tools.base import BaseTool, ToolResult
import libcst as cst

class CodeEditTool(BaseTool):
    name = "edit_file"
    description = "Apply a Unified Diff patch to a file. Provide 'path', 'unified_diff', and 'thought'. The diff MUST be valid Unified Diff format with correct context lines."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to file"},
            "unified_diff": {"type": "string", "description": "Valid Unified Diff format patch string"},
            "thought": {"type": "string", "description": "Reasoning for this specific change"}
        },
        "required": ["path", "unified_diff", "thought"]
    }

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root

    def execute(self, path: str, unified_diff: str, thought: str) -> ToolResult:
        full_path = os.path.join(self.workspace_root, path)

        if not os.path.exists(full_path):
            return ToolResult(False, error=f"File not found: {path}")

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                original_source = f.read()

            try:
                patch_set = PatchSet(unified_diff.splitlines(keepends=True))
                if not patch_set:
                    return ToolResult(False, error="Empty or invalid patch set.")

                target_patch = None
                for p in patch_set:
                    if p.target_file.endswith(path) or p.target_file == path or p.target_file == f"b/{path}":
                        target_patch = p
                        break

                if not target_patch:
                    return ToolResult(False, error=f"Patch does not target file: {path}. Patch targets: {[p.target_file for p in patch_set]}")

            except Exception as e:
                return ToolResult(False, error=f"Invalid Unified Diff format: {e}")

            with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False, encoding='utf-8') as tf:
                tf.write(unified_diff)
                patch_file = tf.name

            try:
                result = subprocess.run(
                    ["git", "apply", "--check", patch_file],
                    cwd=self.workspace_root,
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0:
                    return ToolResult(False, error=f"Git apply check failed: {result.stderr}")
            finally:
                if os.path.exists(patch_file):
                    os.unlink(patch_file)

            patched_source = self._simulate_apply(original_source, target_patch)

            try:
                cst.parse_module(patched_source)
            except Exception as e:
                return ToolResult(False, error=f"Syntax error after patch simulation: {e}")

            return ToolResult(True, data=patched_source, summary=f"Patch validated for {path}. Thought: {thought}")

        except Exception as e:
            return ToolResult(False, error=f"Unexpected error in edit_file: {e}")

    def _simulate_apply(self, source: str, patch) -> str:
        return source
