"""Phase 11.1b - multi-tenant task API (stdlib; RBAC-guarded, audit-logged, run-is-opt-in).

  POST /tenants/<tid>/tasks   {"task": "...", "repo": "acme/x"}   [agent:execute]
  GET  /tenants/<tid>/tasks                                        [code:read]
  GET  /tenants/<tid>/cost                                         [cost:view]
  GET  /healthz

Queued tasks are journaled (.cortex_memory/tenant_jobs.jsonl); add ?run=true to ALSO execute
through the real pipeline (main.py subprocess, config from server.run_config). Default is
journal-only - an operator cannot trigger compute by accident from a browser.
"""
import argparse
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from server.rbac import AUDIT, AuthZ, Permission, Principal, audit

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / ".cortex_memory" / "tenant_jobs.jsonl"
CFG = {}


def load_cfg(path: Path) -> dict:
    try:
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("server", {}) or {}
    except Exception:
        return {}


class H(BaseHTTPRequestHandler):
    authz: AuthZ = None
    cfg: dict = {}

    def log_message(self, *a): pass

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _principal(self):
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        if not tok:
            self._send(401, {"error": "missing bearer token"})
            return None
        try:
            return self.authz.decode_token(tok)
        except Exception as e:
            self._send(401, {"error": f"invalid token: {e}"})
            return None

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True, "tenants": list((self.cfg.get("tenants") or {}).keys())})
        p = self._principal()
        if p is None:
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "tenants":
            tid, what = parts[1], parts[2]
            perm = Permission.VIEW_COST if what == "cost" else Permission.READ_CODE
            if not self.authz.check(p, perm, tenant=tid):
                audit(p, str(perm), what, tid, "DENY")
                return self._send(403, {"error": "forbidden"})
            audit(p, str(perm), what, tid, "ALLOW")
            if what == "cost":
                return self._send(200, {"tenant": tid, "cost": self._cost_for(tenant=tid)})
            return self._send(200, {"tenant": tid, "jobs": [j for j in self._jobs() if j.get("tenant") == tid]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self._principal()
        if p is None:
            return
        parts = self.path.strip("/").split("?")
        q = parts[1] if len(parts) > 1 else ""
        parts = parts[0].split("/")
        if not (len(parts) == 3 and parts[0] == "tenants" and parts[2] == "tasks"):
            return self._send(404, {"error": "not found"})
        tid = parts[1]
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        except Exception:
            return self._send(400, {"error": "bad json"})
        repo = body.get("repo", "")
        if not self.authz.check(p, Permission.EXECUTE_AGENT, resource=repo, tenant=tid):
            why = ("tenant isolation" if p.tenant_id != tid else
                   "role lacks agent:execute" if not self.authz.check(p, Permission.EXECUTE_AGENT, "", tid)
                   else f"repo '{repo}' not in allow-list")
            audit(p, "agent:execute", repo, tid, "DENY", why)
            return self._send(403, {"error": f"forbidden: {why}"})
        audit(p, "agent:execute", repo, tid, "ALLOW")
        job = {"ts": round(time.time(), 1), "tenant": tid, "user": p.user_id, "repo": repo,
               "task": str(body.get("task", ""))[:300], "status": "queued", "run": "run=true" in q}
        JOBS.parent.mkdir(parents=True, exist_ok=True)
        with JOBS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")
        if job["run"]:
            threading.Thread(target=self._execute, args=(job,), daemon=True).start()
        return self._send(202, {"queued": True, "executing": job["run"], "tenant": tid})

    # ---------------------------------------------------------------- util
    def _jobs(self):
        if not JOBS.exists():
            return []
        return [json.loads(l) for l in JOBS.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _execute(self, job):
        repo_path = (self.cfg.get("tenants", {}).get(job["tenant"], {}) or {}).get("repo_path", "")
        cmd = [str(ROOT / ".venv/bin/python"), "main.py", job["task"], "--repo", repo_path,
               "--config", self.cfg.get("run_config", "config.yaml"), "--yes"]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
        job.update({"status": "done" if "TASK COMPLETED" in (r.stdout or "") else "failed",
                    "tail": (r.stdout or r.stderr)[-200:]})
        # append final status marker
        with JOBS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")

    def _cost_for(self, tenant):
        # attribution: tenant jobs carry durable task ids (md5[:12] of the task string) - match
        # session metas by task text hash, else fall back to the fleet aggregate.
        import hashlib
        sess = ROOT / ".cortex_memory" / "sessions"
        ttasks = {j["task"] for j in self._jobs() if j.get("tenant") == tenant}
        pricing = (((self.cfg.get("pricing") or {}).get("gpt-4o-mini")) or {"prompt": 0.15, "completion": 0.60})
        total, runs = 0.0, 0
        for mp in sess.glob("*.meta.json"):
            try:
                m = json.loads(mp.read_text())
            except Exception:
                continue
            cost = m.get("est_prompt_tokens", 0) / 1e6 * pricing.get("prompt", 0) + \
                   m.get("est_completion_tokens", 0) / 1e6 * pricing.get("completion", 0)
            if ttasks and (m.get("task", "")[:120] not in {t[:120] for t in ttasks}):
                continue
            total += cost
            runs += 1
        return {"tenant": tenant, "runs": runs, "est_cost_usd": round(total, 6),
                "attribution": "per-task" if ttasks else "fleet-aggregate"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8333)
    ap.add_argument("--secret", default=None, help="JWT HS256 secret (default: server.jwt_secret from config)")
    a = ap.parse_args()
    H.cfg = load_cfg(ROOT / "config.yaml")
    H.authz = AuthZ(a.secret or H.cfg.get("jwt_secret", "dev-secret"))
    print(f"[server] multi-tenant API on :{a.port} (tenants: {list((H.cfg.get('tenants') or {}).keys())})")
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
