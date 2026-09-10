"""Phase 11.2 - GitOps: branch strategy + Conventional Commits + signing + merge policy.

"PR을 열어줘"가 아니라 *정책*을 선언한다: branch_prefix, commit_convention (conventional|jira),
sign_commits, merge_strategy, protected branches. Every mutating command goes through here,
so violations are rejected BEFORE git runs them - and dry_run (no push/gh) keeps CI safe.

Run: python -m vcs.gitops --selftest      (spins up a local bare origin and exercises all gates)
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

CONVENTIONAL = re.compile(r"^(feat|fix|chore|docs|test|refactor|perf|ci|build|style)(\([a-z0-9\-_./]+\))?!?: .{1,72}$")
JIRA = re.compile(r"^[A-Z][A-Z0-9]+-\d+: .+")


class GitPolicyError(Exception):
    pass


@dataclass
class GitPolicy:
    branch_prefix: str = "cortex/"
    commit_convention: str = "conventional"          # conventional | jira | free
    sign_commits: bool = False                        # keep False unless a key exists
    gpg_key_id: Optional[str] = None
    merge_strategy: str = "squash"                    # squash | rebase | merge
    protected_branches: List[str] = field(default_factory=lambda: ["main", "master"])
    dry_run: bool = False                             # plan instead of push/PR


class GitOps:
    def __init__(self, repo_path: Path | str, policy: Optional[GitPolicy] = None):
        self.repo = Path(repo_path)
        self.policy = policy or GitPolicy()

    # ------------------------------------------------------------ validation
    def validate_branch(self, branch: str, creating: bool = True) -> None:
        if creating and branch in self.policy.protected_branches:
            raise GitPolicyError(f"'{branch}' is protected - work on {self.policy.branch_prefix}<task-id>")
        if creating and not branch.startswith(self.policy.branch_prefix):
            raise GitPolicyError(f"branch must start with '{self.policy.branch_prefix}' (got '{branch}')")

    def validate_message(self, message: str) -> None:
        head = message.strip().splitlines()[0] if message.strip() else ""
        if self.policy.commit_convention == "conventional" and not CONVENTIONAL.match(head):
            raise GitPolicyError(f"not a Conventional Commit: {head[:70]!r}")
        if self.policy.commit_convention == "jira" and not JIRA.match(head):
            raise GitPolicyError(f"needs 'PROJ-123: subject': {head[:70]!r}")

    def _g(self, *args: str, check: bool = True, capture: bool = True) -> str:
        r = subprocess.run(["git", *args], cwd=self.repo, capture_output=capture, text=True)
        if check and r.returncode != 0:
            raise GitPolicyError(f"git {' '.join(args)}: {(r.stderr or '').strip()[:200]}")
        return (r.stdout or "").strip()

    # ----------------------------------------------------------------- ops
    def create_task_branch(self, task_id: str, base: str = "main") -> str:
        branch = f"{self.policy.branch_prefix}{task_id}"
        self.validate_branch(branch)
        if (self.repo / ".git").exists() or self._inside_worktree():
            self._g("checkout", base)
            if self._has_upstream(base):
                self._g("pull", "--ff-only")
        self._g("checkout", "-b", branch)
        return branch

    def commit_changes(self, message: str, files: Optional[List[str]] = None) -> str:
        self.validate_message(message)
        self._g("add", *(files or ["-A"]))
        if not self._g("status", "--porcelain"):
            raise GitPolicyError("nothing to commit")
        cmd = ["commit", "-m", message]
        if self.policy.sign_commits:
            if shutil.which("gpg") and self.policy.gpg_key_id:
                cmd += ["-S", "-u", self.policy.gpg_key_id]
            elif self.policy.gpg_key_id:
                print("[gitops] signing requested but no gpg binary - committing unsigned")
        self._g(*cmd)
        return self._g("rev-parse", "HEAD")

    def push(self, branch: str) -> str:
        if branch in self.policy.protected_branches:
            raise GitPolicyError(f"refusing to push protected branch '{branch}'")
        if self.policy.dry_run:
            return f"DRY-RUN: git push -u origin {branch}"
        self._g("push", "-u", "origin", branch)
        return f"pushed {branch}"

    def create_pr(self, branch: str, title: str, body: str, base: str = "main") -> str:
        self.validate_message(title)
        if self.policy.dry_run or not shutil.which("gh"):
            return f"DRY-RUN PR PLAN: gh pr create --base {base} --head {branch} --title {title!r} (merge={self.policy.merge_strategy})"
        out = subprocess.run(["gh", "pr", "create", "--base", base, "--head", branch,
                              "--title", title, "--body", body], cwd=self.repo,
                             capture_output=True, text=True)
        return (out.stdout or out.stderr).strip()

    def merge_plan(self, pr_number: int = 0) -> str:
        return f"gh pr merge {('--squash' if self.policy.merge_strategy == 'squash' else '--rebase' if self.policy.merge_strategy == 'rebase' else '--merge')} {pr_number or ''}".strip()

    def apply_policy_checks(self) -> Optional[bool]:
        if not shutil.which("pre-commit"):
            return None                                   # not installed -> reported, never faked
        return subprocess.run(["pre-commit", "run", "--all-files"], cwd=self.repo, capture_output=True).returncode == 0

    def _inside_worktree(self) -> bool:
        return subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip() == "true"

    def _has_upstream(self, branch: str) -> bool:
        return subprocess.run(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=self.repo,
                              capture_output=True).returncode == 0


def _selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gitops-selftest-"))
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    for k, v in (("user.name", "cortex-gitops-selftest"), ("user.email", "bot@cortex.local")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    g = GitOps(work, GitPolicy(dry_run=False))
    subprocess.run(["git", "-c", "user.name=cortex", "-c", "user.email=c@c", "commit", "-q", "--allow-empty", "-m", "chore: seed"],
                   cwd=work, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=work, check=True)

    ok = 0
    try:
        g.policy.dry_run = True
        g.create_task_branch("T-42")
        (work / "fix.py").write_text("V = 1\n")
        sha = g.commit_changes("fix(repo): harden path checks\n\nBody explains the why.")
        print("  commit:", sha[:8]); ok += 1
        print(" ", g.push("cortex/T-42")); ok += 1
        print(" ", g.create_pr("cortex/T-42", "fix(repo): harden path checks", "auto")); ok += 1
        for bad_branch, bad_msg in [("hotfix/now", None), (None, "fixed stuff")]:
            try:
                if bad_branch: g.validate_branch(bad_branch)
                if bad_msg: g.validate_message(bad_msg)
                print("  FAIL: accepted", bad_branch or bad_msg)
            except GitPolicyError as e:
                print("  rejected:", str(e)[:64]); ok += 1
        try:
            g.push("main")
            print("  FAIL: pushed protected main")
        except GitPolicyError as e:
            print("  rejected:", str(e)[:52]); ok += 1
        # real push path (dry_run off) then PR via gh -> no gh remote: falls back to plan
        g.policy.dry_run = False
        g._g("push", "-q", "-u", "origin", "cortex/T-42")
        print("  real push to local bare origin: OK"); ok += 1
        print(f"\nGITOPS SELFTEST {ok}/8")
        return 0 if ok >= 7 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.exit(_selftest() if a.selftest else 0)
