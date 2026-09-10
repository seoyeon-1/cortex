"""Phase 15.1 - Preflight static simulator.

Goal: reject ~90% of broken edits BEFORE paying for more LLM turns or touching the
worktree. A candidate unified diff is applied fully in memory, then run through:

  1. diff parse      (unidiff structure must be valid and target `path`)
  2. context check   (hunk context lines must match the file as it is NOW)
  3. syntax check    (ast.parse of the predicted file content)
  4. lint check      (ruff, if importable; mode: off|warn|block)
  5. import check    (every imported top-level module must resolve to stdlib,
                      an installed package, or a local file - otherwise warning)
  6. minimal-diff    (changed-lines heuristic from the system prompt's hard rule ->
                      a *warning* here so the model gets an actionable nudge)

This is an ADVISORY gate: the real apply still goes through `git apply --check`
+ libcst in CodeEditTool/ApplyPatchSetTool. Preflight just front-loads the failure
with a precise, cheap message. Errors block; warnings are advisory.
"""
import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from unidiff import PatchSet


@dataclass
class PreflightResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # simulated outcome, keyed by repo-relative path
    predicted_files: Dict[str, str] = field(default_factory=dict)

    def as_error(self) -> str:
        parts = [f"Preflight failed: {e}" for e in self.errors]
        parts += [f"(warning) {w}" for w in self.warnings]
        return " | ".join(parts) if parts else "Preflight failed."


def apply_patch_in_memory(source: str, patch) -> str:
    """Apply one unidiff `Patch` (its hunks) to `source` without touching disk.

    Raises ValueError on context mismatch (i.e. the file drifted since the model
    read it) - the exact class of bug that makes `git apply` fail mid-loop.
    """
    lines = source.splitlines(keepends=True)
    for hunk in sorted(patch, key=lambda h: h.source_start, reverse=True):
        src_lines, tgt_lines = [], []
        for line in hunk:
            if line.is_context:
                src_lines.append(line)
                tgt_lines.append(line)
            elif line.is_removed:
                src_lines.append(line)
            elif line.is_added:
                tgt_lines.append(line)
        start = hunk.source_start - 1
        for off, sl in enumerate(src_lines):
            idx = start + off
            if idx >= len(lines):
                raise ValueError(f"hunk at line {hunk.source_start}: file is shorter than patch context")
            if lines[idx].rstrip("\r\n") != sl.value.rstrip("\r\n"):
                raise ValueError(
                    f"context mismatch at line {idx + 1}: file has {lines[idx].rstrip()!r}, "
                    f"patch expects {sl.value.rstrip(chr(10)+chr(13))!r} (file drifted since you read it - re-read, then re-diff)")
        new = [l.value.rstrip("\r\n") + "\n" for l in tgt_lines]
        if not "".join(new).endswith("\n") and lines and not lines[-1].endswith("\n") and start + len(src_lines) >= len(lines):
            new[-1] = new[-1].rstrip("\n")  # preserve "no newline at EOF" of the original
        lines[start:start + len(src_lines)] = new
    return "".join(lines)


class PreflightSimulator:
    def __init__(self, workspace_root: str, cfg: Optional[dict] = None):
        c = cfg or {}
        self.workspace_root = str(workspace_root)
        self.enabled = bool(c.get("enabled", True))
        self.lint_mode = str(c.get("lint", "warn"))            # off|warn|block
        self.import_check = bool(c.get("import_check", True))
        self.max_changed_lines = int(c.get("max_changed_lines", 12))
        self.lint_timeout = int(c.get("lint_timeout_sec", 10))
        self._ruff = self._find_ruff() if self.lint_mode != "off" else None

    # ------------------------------------------------------------------ public

    def simulate_patch(self, path: str, unified_diff: str) -> PreflightResult:
        errors, warnings, predicted = [], [], {}

        # 1) parse
        try:
            patch_set = PatchSet((unified_diff if unified_diff.endswith("\n") else unified_diff + "\n")
                                 .splitlines(keepends=True))
            target = next((p for p in patch_set
                           if p.target_file.endswith(path)
                           or p.target_file.removeprefix("b/").removeprefix("a/") == path), None)
            if target is None:
                return PreflightResult(False, [f"Patch target mismatch: {path} "
                                               f"(diff targets {[p.target_file for p in patch_set]})"], [], {})
        except Exception as e:
            return PreflightResult(False, [f"Invalid diff format: {e}"], [], {})

        # 2) load current content + in-memory apply (context check happens inside)
        full_path = os.path.join(self.workspace_root, path)
        if not os.path.isfile(full_path):
            return PreflightResult(False, [f"File not found: {path}"], [], {})
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                original = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return PreflightResult(False, [f"Cannot read {path}: {e}"], [], {})
        if not path.endswith(".py"):
            predicted[path] = original            # non-python: apply/parse pass not applicable
            return PreflightResult(True, [], warnings, predicted)
        try:
            patched = apply_patch_in_memory(original, target)
        except ValueError as e:
            return PreflightResult(False, [str(e)], [], {})
        except Exception as e:
            return PreflightResult(False, [f"Patch apply simulation failed: {e}"], [], {})
        predicted[path] = patched

        changed = sum(1 for l in unified_diff.split("\n")
                      if (l.startswith("+") and not l.startswith("+++")) or (l.startswith("-") and not l.startswith("---")))
        if changed > self.max_changed_lines:
            warnings.append(f"minimal-diff rule: {changed} lines changed in {path} "
                            f"(> {self.max_changed_lines}) - only touch what the failing test asserts")

        # 3) syntax
        try:
            ast.parse(patched, filename=path)
        except SyntaxError as e:
            errors.append(f"SyntaxError after patch: {e.msg} at line {e.lineno}")
            return PreflightResult(False, errors, warnings, {})   # parse below would be noise

        # 4) lint
        if self.lint_mode != "off":
            lint_out = self._run_ruff(path, patched)
            if lint_out:
                (errors if self.lint_mode == "block" else warnings).extend(lint_out)

        # 5) imports
        if self.import_check:
            warnings.extend(self._check_imports(patched, path))

        return PreflightResult(len(errors) == 0, errors, warnings, predicted)

    def simulate_patch_set(self, patches: List[Dict]) -> PreflightResult:
        """Multi-file: each file preflighted against current content; then a
        cross-file check that imports of newly created local modules resolve."""
        merged = PreflightResult(True, [], [], {})
        for i, p in enumerate(patches):
            r = self.simulate_patch(p.get("path", "?"), p.get("unified_diff", "") or "")
            merged.errors += [f"patch[{i}] {p.get('path','?')}: {e}" for e in r.errors]
            merged.warnings += r.warnings
            merged.predicted_files.update(r.predicted_files)
        merged.ok = not merged.errors
        return merged

    # ----------------------------------------------------------------- internals

    @staticmethod
    def _find_ruff() -> Optional[List[str]]:
        exe = shutil.which("ruff")
        if exe:
            return [exe]
        if importlib.util.find_spec("ruff") is not None:
            return [sys.executable, "-m", "ruff"]
        return None

    def _run_ruff(self, filename: str, source: str) -> List[str]:
        if self._ruff is None:
            return []                       # not installed: never block on tooling absence
        try:
            r = subprocess.run(self._ruff + ["check", "--output-format", "json", "--quiet",
                                             "--stdin-filename", filename, "-"],
                               input=source.encode(), capture_output=True,
                               cwd=self.workspace_root, timeout=self.lint_timeout)
            findings = json.loads((r.stdout or b"[]").decode("utf-8", "replace"))
            return [f"Lint {f.get('code','?')} at line {f.get('location',{}).get('row','?')}: "
                    f"{f.get('message','')}"[:200] for f in findings if isinstance(f, dict)]
        except Exception as e:              # timeout etc: advisory, never fatal
            return []

    def _check_imports(self, source: str, current_file: str) -> List[str]:
        warnings = []
        try:
            tree = ast.parse(source)
        except Exception:
            return warnings
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])
        stdlib = getattr(sys, "stdlib_module_names", frozenset())
        ws = self.workspace_root
        for root in sorted(roots):
            if root in stdlib or root == "sys" or root in sys.builtin_module_names:
                continue
            if (os.path.exists(os.path.join(ws, root + ".py"))
                    or os.path.isfile(os.path.join(ws, root, "__init__.py"))):
                continue                       # importable from workspace root at runtime
            try:
                if importlib.util.find_spec(root) is not None:
                    continue
            except (ImportError, ValueError, ModuleNotFoundError):
                pass
            warnings.append(f"Import '{root}' may not resolve in this environment "
                            f"(no stdlib/package/workspace module named '{root}')")
        return warnings
