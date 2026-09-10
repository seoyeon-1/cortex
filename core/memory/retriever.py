"""Phase 2.2 - Hybrid Retriever (Vector Search + Graph Traversal + Keyword, RRF fusion).

- Vector: semantic search over Function/Class/File chunks (signature + docstring + body<=50 lines).
    Embedder: sentence-transformers(all-MiniLM-L6-v2, 384-d) when available;
    otherwise a deterministic offline HashEmbedder (384-d) so the pipeline is identical everywhere.
- Storage: LanceDB embedded (no server). Falls back to brute-force cosine over .npz if lancedb is unusable.
- Graph: CodeGraphBuilder (workspace_graph.json). Symbol/keyword hits + 1-hop neighbours.
- Fusion: Reciprocal Rank Fusion (k=60).
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

DIM = 384
_RRF_K = 60


class HashEmbedder:
    """Deterministic offline stand-in for all-MiniLM-L6-v2 (same 384-d contract)."""
    name = "hash-embedder-384"

    def encode(self, texts: List[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in re.findall(r"[A-Za-z0-9_]+", t.lower()):
                h = hashlib.blake2b(tok.encode(), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "little") % DIM
                sign = 1.0 if h[4] % 2 == 0 else -1.0
                out[i, idx] += sign * (1.0 + np.log1p(int.from_bytes(h[5:8], "little") % 1000) / 10.0)
                # char-trigrams help partial symbol matches (e.g. 'fetch' ~ 'fetch_user')
            low = t.lower()
            for j in range(len(low) - 2):
                g = low[j:j + 3]
                h = hashlib.blake2b(g.encode(), digest_size=4).digest()
                out[i, int.from_bytes(h, "little") % DIM] += 0.15
            norm = np.linalg.norm(out[i])
            if norm:
                out[i] /= norm
        return out


class _SentenceTransformerEmbedder:
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # noqa: F401
        self._m = SentenceTransformer(model)
        self.name = model

    def encode(self, texts: List[str]) -> np.ndarray:
        v = self._m.encode(texts, normalize_embeddings=True).astype("float32")
        return v


def _make_embedder(model: str):
    try:
        emb = _SentenceTransformerEmbedder(model)
        if emb._m.get_sentence_embedding_dimension() == DIM:
            return emb
    except Exception:
        pass
    return HashEmbedder()


class _LanceStore:
    def __init__(self, path: str):
        import lancedb
        self.db = lancedb.connect(path)
        self.table = None

    def replace(self, rows: List[Dict]):
        if self.table is not None:
            try:
                self.db.drop_table("code_chunks")
            except Exception:
                pass
        self.table = self.db.create_table("code_chunks", data=rows, mode="overwrite")

    def search(self, q: np.ndarray, k: int) -> List[Dict]:
        if self.table is None:
            return []
        res = self.table.search(q).limit(k)
        try:
            return res.to_list()
        except Exception:
            return res.to_arrow().to_pylist()

    def count(self) -> int:
        return 0 if self.table is None else self.table.count_rows()


class _NpzStore:
    """Brute-force cosine fallback when LanceDB is unavailable."""
    def __init__(self, path: str):
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "code_chunks.npz"
        self.meta = self.dir / "code_chunks.meta.json"
        self.rows: List[Dict] = []
        self.mat: Optional[np.ndarray] = None

    def replace(self, rows: List[Dict]):
        self.rows = rows
        if rows:
            self.mat = np.stack([r["vector"] for r in rows]).astype(np.float32)
            np.savez_compressed(self.file, mat=self.mat)
            self.meta.write_text(json.dumps([r["text"] for r in rows]), encoding="utf-8")

    def search(self, q: np.ndarray, k: int) -> List[Dict]:
        if self.mat is None or len(self.mat) == 0:
            return []
        sims = self.mat @ q.astype(np.float32)
        for i in np.argsort(-sims)[:k]:
            yield {**self.rows[int(i)], "_score": float(sims[i])}

    def count(self) -> int:
        return len(self.rows)


class HybridRetriever:
    def __init__(self, workspace_root: str,
                 model: str = "sentence-transformers/all-MiniLM-L6-v2",
                 vector_db_path: Optional[str] = None,
                 graph_path: Optional[str] = None):
        from core.memory.graph_builder import CodeGraphBuilder
        self._graph_builder_cls = CodeGraphBuilder
        self.workspace_root = str(Path(workspace_root).resolve())
        self.embedder = _make_embedder(model)
        self.graph_path = graph_path or str(Path(self.workspace_root) / "workspace_graph.json")
        self.vector_db_path = vector_db_path or str(Path(self.workspace_root) / ".cortex_vector_db")
        try:
            self.store = _LanceStore(self.vector_db_path)
        except Exception:
            self.store = _NpzStore(self.vector_db_path)
        self.graph = None
        self._file_cache: Dict[str, str] = {}

    # ----------------------------------------------------------------- index
    def index_codebase(self, repo_path: Optional[str] = None) -> Dict:
        repo = repo_path or self.workspace_root
        graph = self._graph_builder_cls().build(repo)
        try:
            self._graph_builder_cls.save(graph, self.graph_path)
        except Exception as e:
            print(f"[RETRIEVER] graph save failed (non-fatal): {e}")
        self.graph = self._load_graph()

        rows = []
        for nid, d in self.graph.nodes(data=True):
            if d.get("type") not in ("Function", "Class", "File"):
                continue
            text = self._chunk_text(nid, d)
            if not text.strip():
                continue
            vec = self.embedder.encode([text])[0]
            rows.append({"vector": [float(x) for x in np.asarray(vec, dtype=np.float32)], "text": text,
                         "file_path": d.get("path", ""), "symbol_name": d.get("name", ""),
                         "node_id": nid, "node_type": d.get("type", ""),
                         "line": int(d.get("line", 0) or 0)})
        try:
            self.store.replace(rows)
        except Exception:  # schema/dim change: drop & recreate once
            self.store = _NpzStore(self.vector_db_path)
            self.store.replace(rows)
        return {"chunks": len(rows), "nodes": self.graph.number_of_nodes(),
                "edges": self.graph.number_of_edges(), "embedder": getattr(self.embedder, "name", "?")}

    def _chunk_text(self, nid, d) -> str:
        head = f"{d.get('type')} {d.get('name')}"
        if d.get("signature"):
            head += f"  [{d['signature']}]"
        if d.get("doc"):
            head += f"\ndoc: {d['doc']}"
        body = self._body_lines(d.get("path", ""), int(d.get("line", 0) or 0))[:50] if d.get("type") in ("Function", "Class") else self._read(d.get("path", ""))[:3000]
        return f"{head}\n{body}"

    def _body_lines(self, rel: str, start: int) -> List[str]:
        lines = self._read(rel).splitlines()
        return lines[max(start - 1, 0): max(start - 1, 0) + 50]

    def _read(self, rel: str) -> str:
        if not rel:
            return ""
        if rel not in self._file_cache:
            try:
                self._file_cache[rel] = (Path(self.workspace_root) / rel).read_text(encoding="utf-8", errors="replace")
            except Exception:
                self._file_cache[rel] = ""
        return self._file_cache[rel]

    def _load_graph(self):
        if self.graph is not None:
            return self.graph
        p = Path(self.graph_path)
        if p.exists():
            self.graph = self._graph_builder_cls.load(str(p))
        else:
            self.graph = self._graph_builder_cls().build(self.workspace_root)
        return self.graph

    # ---------------------------------------------------------------- search
    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        vector_hits = self._vector_search(query, max(top_k * 2, 10))
        graph_hits = self._graph_search(query, max(top_k, 5))

        rrf: Dict[str, Dict] = {}

        def add(nid, rank, src):
            if not nid:
                return
            e = rrf.setdefault(nid, {"node_id": nid, "score": 0.0, "sources": set()})
            e["score"] += 1.0 / (_RRF_K + rank)
            e["sources"].add(src)

        for rank, h in enumerate(vector_hits, 1):
            add(h.get("node_id"), rank, "vector")
        for rank, (nid, why) in enumerate(graph_hits, 1):
            add(nid, rank, "graph")
            rrf[nid].setdefault("match", why)

        ranked = sorted(rrf.values(), key=lambda x: -x["score"])[:top_k]
        out = []
        for r in ranked:
            nid = r["node_id"]
            d = self.graph.nodes.get(nid, {})
            out.append({
                "file": d.get("path", ""), "symbol": d.get("name", nid), "node_id": nid,
                "node_type": d.get("type", ""), "line": d.get("line", 0),
                "snippet": self._chunk_text(nid, d)[:1200],
                "score": round(r["score"], 5),
                "via": sorted(r["sources"]) + ([r["match"]] if "match" in r else []),
            })
        return out

    def _vector_search(self, query: str, k: int) -> List[Dict]:
        q = self.embedder.encode([query])[0]
        try:
            if isinstance(self.store, _LanceStore):
                return self.store.search(np.asarray(q, dtype=np.float32), k)
            return list(self.store.search(np.asarray(q, dtype=np.float32), k))
        except Exception:
            return []

    def _graph_search(self, query: str, top_k: int) -> List[tuple]:
        g = self.graph
        toks = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query) if len(t) > 2]
        scores: Dict[str, float] = {}
        why: Dict[str, str] = {}
        name_index = {n: g.nodes[n].get("name", "") for n in g.nodes}
        for n, name in name_index.items():
            if not name:
                continue
            if any(t == name for t in toks):
                scores[n] = scores.get(n, 0) + 3.0; why[n] = "symbol-exact"
            elif any(t in name.lower() or name.lower() in t for t in toks):
                scores[n] = scores.get(n, 0) + 1.0; why[n] = "symbol-substr"
        for n in list(scores):
            for m in g.predecessors(n):
                scores[m] = scores.get(m, 0) + 0.5; why.setdefault(m, "graph-neighbour")
            for m in g.successors(n):
                scores[m] = scores.get(m, 0) + 0.5; why.setdefault(m, "graph-neighbour")
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
        return [(n, why.get(n, "graph")) for n, _ in ranked]


if __name__ == "__main__":  # quick manual probe: python -m core.memory.retriever <repo> <query>
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("repo"); ap.add_argument("query"); ap.add_argument("--top_k", type=int, default=5)
    a = ap.parse_args()
    hr = HybridRetriever(a.repo)
    print("index:", hr.index_codebase(a.repo))
    for r in hr.search(a.query, a.top_k):
        print(f"- {r['file']}::{r['symbol']} ({r['score']}, via={','.join(r['via'])})")
