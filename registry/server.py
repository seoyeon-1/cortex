"""Phase 6.5b - Agent Protocol server: JSON-RPC 2.0 over HTTP, streaming via NDJSON.

  POST / {"jsonrpc":"2.0","method":"agent.invoke","params":{"agent":"security","task":{...}}}
  POST / {"jsonrpc":"2.0","method":"agent.stream","params":{...}}  -> application/x-ndjson
Methods: ping, registry.list, registry.resolve, agent.card, agent.invoke, agent.stream.
Sub-agents packaged in the registry are thus reachable *out of process* - the marketplace
claim in the spec. stdlib-only; bind 0.0.0.0 for preview; read-only by construction (agents
never edit files, they only review).
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from registry.manifest import Registry


class _RPC(BaseHTTPRequestHandler):
    registry: Registry = None  # injected by serve()
    cfg: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json", raw=False):
        body = obj if raw else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _agents(self):
        out = {}
        for m in self.registry.all():
            sc = ((self.cfg.get("agents", {}) or {}).get("sub_agents", {}) or {}).get(m.key, {}) or {}
            out[m.key] = self.registry.instantiate(m, {**m.config_defaults, **sc})
        return out

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "personas": [m.key for m in self.registry.all()]})
        self._send(404, {"error": "use POST /"})

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        except Exception:
            return self._send(400, {"error": "bad json"})
        rid = req.get("id")
        try:
            res = self._dispatch(req.get("method", ""), req.get("params") or {})
            if res is _STREAMED:
                return
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": res})
        except Exception as e:
            self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(e)}})

    def _dispatch(self, method, params):
        from protocol.a2a import Task
        if method == "ping":
            return "pong"
        if method == "registry.list":
            return self.registry.catalog()
        if method == "registry.resolve":
            m = self.registry.resolve(params.get("query", ""))
            return m.model_dump() if m else None
        if method in ("agent.card", "agent.invoke", "agent.stream"):
            key = params.get("agent", "")
            m = self.registry.latest(key) or self.registry.resolve(key)
            if not m:
                raise ValueError(f"unknown agent: {key}")
            agent = self._agents().get(m.key) or self.registry.instantiate(m)
            if method == "agent.card":
                return agent.card.model_dump()
            tp = params.get("task") or params   # invoke(task, context) OR flat {summary,skill,payload}
            task = Task(summary=tp.get("summary", "rpc task"), skill=tp.get("skill", "review"),
                        payload=tp.get("payload") or {}, parent_run=str(params.get("parent_run", "")))
            if method == "agent.invoke":
                ev = agent.run_task_sync(task)
                return {"kind": ev.kind.value, "artifact": ev.artifact.model_dump()}
            # agent.stream: NDJSON of A2A events (progress... then terminal verdict)
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson")
            self.end_headers()
            async def _pump():
                async for e in agent.handle_task(task):
                    self.wfile.write(e.model_dump_json().encode() + b"\n")
                    self.wfile.flush()
            import asyncio
            asyncio.run(_pump())
            return _STREAMED
        raise ValueError(f"method not found: {method}")


_STREAMED = object()


def serve(port: int, config_path: str, registry_dir: str):
    cfg = {}
    try:
        cfg = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
    except Exception:
        pass
    _RPC.registry = Registry(registry_dir).load()
    _RPC.cfg = cfg
    print(f"[registry-rpc] {len(_RPC.registry.all())} persona(s) at http://0.0.0.0:{port}/ (POST JSON-RPC)")
    ThreadingHTTPServer(("0.0.0.0", port), _RPC).serve_forever()


if __name__ == "__main__":
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8331)
    ap.add_argument("--config", default=os.path.join(root, "config.yaml"))
    ap.add_argument("--registry-dir", default=os.path.join(root, "registry"))
    a = ap.parse_args()
    serve(a.port, a.config, a.registry_dir)
