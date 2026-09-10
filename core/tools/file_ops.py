import os
import glob
from core.tools.base import BaseTool, ToolResult

class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read the full content of a file. Use to understand current code before editing."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to file from workspace root"},
            "thought": {"type": "string", "description": "Why you need to read this file"}
        },
        "required": ["path", "thought"]
    }

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root

    def execute(self, path: str, thought: str) -> ToolResult:
        full_path = os.path.join(self.workspace_root, path)
        if not os.path.exists(full_path):
            return ToolResult(False, error=f"File not found: {path}")
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            return ToolResult(True, data=content, summary=f"Read {len(content)} chars from {path}")
        except Exception as e:
            return ToolResult(False, error=str(e))

class ListFilesTool(BaseTool):
    name = "list_files"
    description = "List files matching a glob pattern. Use to explore project structure."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "default": "**/*.py", "description": "Glob pattern (e.g., **/*.py, tests/**) "},
            "thought": {"type": "string", "description": "What you are looking for"}
        },
        "required": ["thought"]
    }

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root

    def execute(self, pattern: str = "**/*.py", thought: str = "") -> ToolResult:
        try:
            search_path = os.path.join(self.workspace_root, pattern)
            files = glob.glob(search_path, recursive=True)
            rel_files = [os.path.relpath(f, self.workspace_root) for f in files]
            return ToolResult(True, data=rel_files, summary=f"Found {len(rel_files)} files matching '{pattern}'")
        except Exception as e:
            return ToolResult(False, error=str(e))
