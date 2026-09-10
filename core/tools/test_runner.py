import subprocess
import os
from dataclasses import dataclass, asdict
from typing import List, Dict, Any
from core.tools.base import BaseTool, ToolResult

@dataclass
class TestResult:
    passed: int
    failed: int
    errors: int
    total_time: float
    failures: List[Dict[str, Any]]
    raw_output: str

class TestRunnerTool(BaseTool):
    name = "run_tests"
    description = "Run pytest in the sandbox workspace. Returns structured results. Use 'args' for specific targeting (e.g., ['-x', 'test_foo.py'])."
    parameters = {
        "type": "object",
        "properties": {
            "args": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Additional pytest args"},
            "thought": {"type": "string", "description": "Why you are running tests now (e.g., verify fix, get baseline)"}
        },
        "required": ["thought"]
    }

    def __init__(self, workspace_root: str, timeout: int = 120):
        self.workspace_root = workspace_root
        self.timeout = timeout

    def execute(self, args: List[str] = None, thought: str = "") -> ToolResult:
        if args is None: args = []

        cmd = ["python", "-m", "pytest", "--tb=short", "-v", "--no-header"] + args

        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={**os.environ, "PYTHONPATH": self.workspace_root}
            )

            output = result.stdout + "\n" + result.stderr

            passed = 0
            failed = 0
            errors = 0
            failures_detail = []
            current_failure = {}

            lines = output.split('\n')
            for line in lines:
                if " PASSED" in line:
                    passed += 1
                elif " FAILED" in line:
                    failed += 1
                    current_failure = {"test": line.strip(), "trace": ""}
                elif " ERROR" in line:
                    errors += 1
                    current_failure = {"test": line.strip(), "trace": ""}

                if current_failure and (line.strip().startswith("AssertionError") or line.strip().startswith("File ") or "raise " in line or "======" in line or "------" in line):
                    current_failure["trace"] += line + "\n"

                if ("======" in line or "------" in line) and current_failure and current_failure.get("trace"):
                    failures_detail.append(current_failure)
                    current_failure = {}

            tr = TestResult(passed, failed, errors, 0.0, failures_detail, output)
            success = (failed == 0 and errors == 0 and result.returncode == 0)

            summary = f"Tests: {passed} passed, {failed} failed, {errors} errors. (Exit code: {result.returncode})"
            return ToolResult(success, data=asdict(tr), summary=summary, error=output if not success else "")

        except subprocess.TimeoutExpired:
            return ToolResult(False, error=f"Test timeout ({self.timeout}s)", summary="Timeout")
        except Exception as e:
            return ToolResult(False, error=str(e), summary="Runner Exception")
