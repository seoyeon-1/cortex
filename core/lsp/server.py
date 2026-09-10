"""Phase 5.1 - Minimal LSP server (stdio, JSON-RPC) backed by Cortex's code graph.

Serves what a VS Code / any-LSP client needs for Cortex-aware navigation:
  * initialize / initialized / shutdown / exit lifecycle
  * textDocument/documentSymbol   (symbols of one file, from tree-sitter graph)
  * workspace/symbol              (project-wide symbol search - the retriever's graph)

Symbol kinds: class=5, function=12, method=6, file=1 (LSP standard).
Run:  python -m core.lsp.server [--root <repo>]
"""
import argparse
import json
import sys
from pathlib import Path

KIND = {"Class": 5, "Function": 12, "File": 1, "Import": 9}


class LspServer:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self._graph = None
        self.running = True

    def _load_graph(self):
        if self._graph is None:
            from core.memory.graph_builder import CodeGraphBuilder
            self._graph = CodeGraphBuilder().build(str(self.root))
        return self._graph

    def document_symbol(self, uri: str) -> list:
        rel = uri.replace("file://", "")
        try:
            rel = str(Path(rel).resolve().relative_to(self.root))
        except Exception:
            pass
        out = []
        g = self._load_graph()
        for nid, d in g.nodes(data=True):
            if d.get("path") == rel and d.get("type") in ("Class", "Function"):
                line = int(d.get("line", 1) or 1) - 1
                out.append({
                    "name": d.get("name", "?"), "kind": KIND.get(d.get("type"), 12),
                    "location": {"uri": uri, "range": {
                        "start": {"line": line, "character": 0}, "end": {"line": line, "character": 120}}},
                })
        return out

    def workspace_symbol(self, query: str) -> list:
        out = []
        for nid, d in self._load_graph().nodes(data=True):
            name = d.get("name", "")
            if name and (query or "").lower() in name.lower():
                line = int(d.get("line", 1) or 1) - 1
                uri = f"file://{self.root / d.get('path','')}"
                out.append({"name": name, "kind": KIND.get(d.get("type"), 12), "containerName": d.get("path", ""),
                            "location": {"uri": uri, "range": {
                                "start": {"line": line, "character": 0}, "end": {"line": line, "character": 0}}}})
                if len(out) >= 50:
                    break
        return out

    def handle(self, msg: dict):
        m = msg.get("method")
        if m == "initialize":
            return self._result(msg, {
                "capabilities": {"documentSymbolProvider": True, "workspace": {"symbolProvider": True}},
                "serverInfo": {"name": "cortex-lsp", "version": "1.0"}})
        if m == "shutdown":
            return self._result(msg, None)
        if m == "exit":
            self.running = False
            return None
        if m == "textDocument/documentSymbol":
            return self._result(msg, self.document_symbol(msg["params"]["textDocument"]["uri"]))
        if m == "workspace/symbol":
            return self._result(msg, self.workspace_symbol(msg["params"].get("query", "")))
        if msg.get("id") is not None:  # unknown request -> empty result, never error a client on init
            return self._result(msg, None)
        return None  # notifications

    @staticmethod
    def _result(msg, result):
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}


def _read(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
    n = int(headers.get(b"content-length", b"0"))
    return json.loads(stream.read(n))


def _write(stream, obj):
    data = json.dumps(obj).encode()
    stream.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
    stream.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    srv = LspServer(a.root)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while srv.running:
        msg = _read(stdin)
        if msg is None:
            break
        resp = srv.handle(msg)
        if resp is not None:
            _write(stdout, resp)


if __name__ == "__main__":
    main()
