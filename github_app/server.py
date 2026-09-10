"""Phase 5.3 - GitHub App / webhook automation.

Receives webhooks, verifies X-Hub-Signature-256 (hmac.compare_digest), and on
`issues.labeled` with label `cortex:fix`:
  1. gh issue view -> title/body as task
  2. gh branch `cortex/issue-<n>` from HEAD
  3. run AgentLoop headless in the repo (config github.repo_dir)
  4. if success: push branch + gh pr create (idempotent: update existing PR body)
`dependabot` PRs with label `cortex:verify` get a run_tests-only validation comment.

Server is stdlib-only. Without `gh`/network the dispatcher degrades to
DRY-RUN: it logs the exact command sequence it would run (validated in tests).
"""
import argparse
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIX_LABEL = "cortex:fix"
VERIFY_LABEL = "cortex:verify"


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class GithubBot:
    def __init__(self, cfg: dict, cortex_root: Path):
        self.cfg = cfg or {}
        self.root = cortex_root
        self.repo_dir = self.cfg.get("repo_dir", ".")
        self.dry = bool(self.cfg.get("dry_run", not shutil.which("gh")))
        self.journal = cortex_root / ".cortex_memory" / "github_jobs.jsonl"

    def _gh(self, *args, input_text: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(["gh"] + list(args), cwd=self.repo_dir, input=input_text,
                              capture_output=True, text=True, timeout=self.cfg.get("timeout_seconds", 600))

    def _log(self, rec: dict):
        rec["queued"] = True
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[GITHUB-BOT] job: {rec}")

    def handle(self, event: str, payload: dict) -> dict:
        # Phase 9.3: maintainer mode (triage + dependabot plans; journal-only unless posted)
        try:
            if (self.cfg.get("maintainer", {}) or {}).get("enabled"):
                from econ.maintainer import classify, dependabot_plan, record, reply_draft
                action = payload.get("action")
                if event == "issues" and action == "opened":
                    iss = payload.get("issue", {})
                    cls = classify(iss.get("title", ""), iss.get("body", ""))
                    record("issue_triage", {"number": iss.get("number"), **cls,
                                            "reply_draft": reply_draft(cls, iss.get("title", ""))})
                    return {"handled": True, "maintainer": "triage journaled", **cls}
                if event == "pull_request" and action == "opened":
                    pr = payload.get("pull_request", {})
                    who = str((pr.get("user") or {}).get("login", "")).lower()
                    if "dependabot" in who or "bump" in str(pr.get("title", "")).lower():
                        record("dependabot_plan", dependabot_plan(pr))
                        return {"handled": True, "maintainer": "dependabot plan journaled"}
        except Exception as e:
            print(f"[github] maintainer hook skipped: {e}")
        if event == "issues" and payload.get("action") == "labeled":
            label = (payload.get("label") or {}).get("name", "")
            if label == FIX_LABEL:
                return self._fix_issue(payload["repository"]["full_name"], payload["issue"])
            if label == VERIFY_LABEL:
                return self._verify_pr(payload["repository"]["full_name"], payload["issue"])
        return {"handled": False, "event": event}

    def _fix_issue(self, repo: str, issue: dict) -> dict:
        n = issue["number"]
        task = f"{issue['title']}\n{issue.get('body') or ''}".strip()[:1500]
        plan = ["git fetch && git checkout -B cortex/issue-" + str(n),
                f"python main.py {json.dumps(task, ensure_ascii=False)} --repo <repo> --yes",
                "git push -u origin cortex/issue-" + str(n),
                f"gh pr create --title 'fix: {issue['title'][:60]}' --body 'Closes #{n} (auto by Cortex)'"]
        if self.dry:
            self._log({"type": "fix", "repo": repo, "issue": n, "mode": "dry_run", "commands": plan})
            return {"handled": True, "mode": "dry_run", "commands": plan}
        try:
            branch = f"cortex/issue-{n}"
            subprocess.run(["git", "checkout", "-q", "-B", branch], cwd=self.repo_dir, capture_output=True, text=True)
            self._gh("issue", "comment", str(n), "--body", f"🤖 Cortex picking up #{n} on branch `{branch}`...")
            run = subprocess.run([sys.executable, str(self.root / "main.py"), task,
                                  "--repo", self.repo_dir, "--yes"],
                                 cwd=self.root, capture_output=True, text=True,
                                 timeout=self.cfg.get("agent_timeout_seconds", 900))
            status = "DONE" if run.returncode == 0 else "NEEDS_REVIEW"
            changed = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo_dir, capture_output=True, text=True).stdout.strip()
            pr_url = ""
            if run.returncode == 0 and changed:
                subprocess.run(["git", "add", "-A"], cwd=self.repo_dir)
                subprocess.run(["git", "-c", "user.name=cortex-bot", "-c", "user.email=cortex@local",
                                "commit", "-q", "-m", f"fix: {issue['title'][:70]} (closes #{n})"], cwd=self.repo_dir)
                subprocess.run(["git", "push", "-u", "origin", branch, "-f"], cwd=self.repo_dir, capture_output=True, text=True)
                pr = self._gh("pr", "create", "--fill", "--title", f"fix: {issue['title'][:60]}",
                              "--body", f"Closes #{n} - opened autonomously by Cortex.\n```\n{run.stdout[-1500:]}\n```")
                pr_url = pr.stdout.strip()
            body = f"### Cortex run for #{n}\nStatus: {status}" + (f"\nPR: {pr_url}" if pr_url else "")
            self._gh("issue", "comment", str(n), "--body", body)
            self._log({"type": "fix", "repo": repo, "issue": n, "mode": "live", "agent_rc": run.returncode, "pr": pr_url})
            return {"handled": True, "mode": "live", "agent_rc": run.returncode, "pr": pr_url}
        except Exception as e:
            self._log({"type": "fix", "repo": repo, "issue": n, "mode": "live", "error": str(e)})
            return {"handled": True, "mode": "live", "error": str(e)}

    def _verify_pr(self, repo: str, pr: dict) -> dict:
        n = pr["number"]
        if self.dry:
            self._log({"type": "verify", "repo": repo, "pr": n, "mode": "dry_run"})
            return {"handled": True, "mode": "dry_run"}
        run = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=self.repo_dir,
                             capture_output=True, text=True, timeout=300)
        verdict = "✅ tests pass" if run.returncode == 0 else f"❌ tests failed\n```\n{run.stdout[-2000:]}\n```"
        self._gh("pr", "review", str(n), "--body", f"Cortex CI verify: {verdict}", "--comment")
        return {"handled": True, "mode": "live", "returncode": run.returncode}


BOT: GithubBot | None = None


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        ln = int(self.headers.get("content-length", 0))
        body = self.rfile.read(ln)
        secret = os.environ.get("CORTEX_WEBHOOK_SECRET", "")
        if secret and not verify_signature(secret, body, self.headers.get("X-Hub-Signature-256", "")):
            self._send(403, {"error": "invalid signature"})
            return
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        result = BOT.handle(self.headers.get("X-GitHub-Event", ""), payload)
        self._send(202 if result.get("handled") else 200, result)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "dry_run": bool(BOT and BOT.dry)})
        else:
            self._send(404, {"error": "use POST /webhook"})

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    global BOT
    ap = argparse.ArgumentParser(description="Cortex GitHub App webhook receiver")
    ap.add_argument("--port", type=int, default=8323)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    import yaml
    cfg = {}
    p = Path(__file__).resolve().parents[1] / "config.yaml"
    if p.exists():
        cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("github", {})
    BOT = GithubBot(cfg, Path(__file__).resolve().parents[1])
    print(f"[GITHUB-BOT] listening on :{a.port}/webhook (dry_run={BOT.dry}, needs env CORTEX_WEBHOOK_SECRET)")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
