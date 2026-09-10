import os
import subprocess
import uuid
from pathlib import Path
from typing import Generator, Optional
from contextlib import contextmanager
import git

class Sandbox:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        try:
            self.repo = git.Repo(self.repo_path)
        except git.InvalidGitRepositoryError:
            raise ValueError(f"Not a git repository: {repo_path}. Run 'git init' first.")
        self.worktree_path: Optional[Path] = None
        self.branch_name: str = ""

    @contextmanager
    def session(self, base_branch: str = "HEAD") -> Generator[Path, None, None]:
        self._create_worktree(base_branch)
        try:
            yield self.worktree_path
        finally:
            self._cleanup()

    def _create_worktree(self, base_branch: str):
        self.branch_name = f"cortex-sandbox-{uuid.uuid4().hex[:8]}"
        self.worktree_path = self.repo_path.parent / f".cortex_sandbox_{self.branch_name}"

        self.repo.git.worktree("add", "-b", self.branch_name, str(self.worktree_path), base_branch)
        print(f"[SANDBOX] Created at: {self.worktree_path} (branch: {self.branch_name})")

    def _cleanup(self):
        if self.worktree_path and self.worktree_path.exists():
            try:
                self.repo.git.worktree("remove", "--force", str(self.worktree_path))
            except Exception as e:
                print(f"[SANDBOX] Warning: Failed to remove worktree: {e}")
            try:
                self.repo.git.branch("-D", self.branch_name)
            except Exception: pass
            print(f"[SANDBOX] Cleaned up.")

    def apply_patch(self, patch_content: str) -> bool:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False, encoding='utf-8') as tf:
            tf.write(patch_content if patch_content.endswith("\n") else patch_content + "\n")
            patch_file = tf.name

        try:
            result = subprocess.run(
                ["git", "apply", patch_file],
                cwd=self.worktree_path,
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                print(f"[SANDBOX] Git apply failed: {result.stderr}")
                return False
            return True
        finally:
            if os.path.exists(patch_file):
                os.unlink(patch_file)

    def run_command(self, cmd: list, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=self.worktree_path, **kwargs)

    def get_diff(self) -> str:
        sandbox_repo = git.Repo(self.worktree_path)
        return sandbox_repo.git.diff("HEAD")
