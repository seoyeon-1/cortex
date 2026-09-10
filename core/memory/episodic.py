"""Phase 3.1 - Episodic memory (persistent across runs).

Episode = {id, task, plan, patches, test_result, reflection, timestamp, git_commit_hash}

Storage: LanceDB embedded (table `episodes`, vector col 384-d from the shared embedder).
Fallback: JSONL + .npz brute-force (same contract, no server).
Lives in <cortex_root>/.cortex_memory so memories survive sandbox teardown.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

DIM = 384


def _embedder():
    from core.memory.retriever import _make_embedder
    return _make_embedder("sentence-transformers/all-MiniLM-L6-v2")


class _LanceEpisodeStore:
    TABLE = "episodes"

    def __init__(self, root: Path):
        import lancedb
        self.db = lancedb.connect(str(root / "episodes_db"))
        self.table = None

    def append(self, rows: List[Dict]):
        if self.table is None:
            try:
                self.table = self.db.open_table(self.TABLE)
            except Exception:
                self.table = None
        if self.table is None:
            try:
                self.table = self.db.create_table(self.TABLE, data=rows, mode="create")
            except Exception:
                # schema/dim drift: drop & recreate (tip)
                self.table = self.db.create_table(self.TABLE, data=rows, mode="overwrite")
        else:
            self.table.add(rows)

    def all(self) -> List[Dict]:
        if self.table is None:
            try:
                self.table = self.db.open_table(self.TABLE)
            except Exception:
                return []
        try:
            return self.table.to_list()
        except Exception:
            return []

    def _ensure(self):
        if self.table is None:
            try:
                self.table = self.db.open_table(self.TABLE)
            except Exception:
                self.table = None
        return self.table

    def search(self, q: np.ndarray, k: int) -> List[Dict]:
        t = self._ensure()
        if t is None:
            return []
        res = t.search(q).limit(k)
        try:
            return res.to_list()
        except Exception:
            return res.to_arrow().to_pylist()

    def count(self) -> int:
        t = self._ensure()
        try:
            return 0 if t is None else t.count_rows()
        except Exception:
            return 0


class _JsonlEpisodeStore:
    def __init__(self, root: Path):
        self.file = root / "episodes.jsonl"
        self.vecs = root / "episodes.vecs.npz"
        self.rows: List[Dict] = []
        self.mat: Optional[np.ndarray] = None
        self._load()

    def _load(self):
        if self.file.exists():
            self.rows = [json.loads(l) for l in self.file.read_text(encoding="utf-8").splitlines() if l.strip()]
            if self.rows and self.vecs.exists():
                self.mat = np.load(self.vecs)["mat"]

    def append(self, rows: List[Dict]):
        self.rows.extend(rows)
        with self.file.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        new = np.stack([r["vector"] for r in rows]).astype(np.float32)
        self.mat = new if self.mat is None else np.vstack([self.mat, new])
        np.savez_compressed(self.vecs, mat=self.mat)

    def all(self) -> List[Dict]:
        return list(self.rows)

    def search(self, q: np.ndarray, k: int) -> List[Dict]:
        if self.mat is None or len(self.rows) == 0:
            return []
        sims = self.mat @ q.astype(np.float32)
        for i in np.argsort(-sims)[:k]:
            yield {**self.rows[int(i)], "_score": float(sims[i])}

    def count(self) -> int:
        return len(self.rows)


class EpisodeStore:
    def __init__(self, cortex_root: str | Path, dim: int = DIM):
        root = Path(cortex_root) / ".cortex_memory"
        root.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self._root = root
        self.embedder = _embedder()
        try:
            self.store = _LanceEpisodeStore(root)
        except Exception:
            self.store = _JsonlEpisodeStore(root)

    def record(self, task: str, plan: str, patches: List[str], test_result: str,
               reflection: str, git_commit_hash: str = "", timestamp: Optional[str] = None) -> Dict:
        ep = {
            "id": uuid.uuid4().hex,
            "task": task,
            "plan": plan,
            "patches": json.dumps(patches, ensure_ascii=False),
            "test_result": test_result,
            "reflection": reflection,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_commit_hash": git_commit_hash,
        }
        vec = self.embedder.encode([ep["task"]])[0]
        ep["vector"] = [float(x) for x in np.asarray(vec, dtype=np.float32)]
        try:
            self.store.append([ep])
        except Exception:
            self.store = _JsonlEpisodeStore(self._root)
            self.store.append([ep])
        return ep

    def similarity_search(self, query: str, top_k: int = 3) -> List[Dict]:
        q = np.asarray(self.embedder.encode([query])[0], dtype=np.float32)
        try:
            hits = self.store.search(q, top_k)
        except Exception:
            hits = []
        out = []
        for h in hits:
            h = {k: v for k, v in h.items() if k not in ("vector",)}
            out.append(h)
        return out

    def count(self) -> int:
        return self.store.count()

    @staticmethod
    def as_context(episodes: List[Dict]) -> str:
        if not episodes:
            return ""
        lines = ["## RELEVANT PAST EPISODES (agent long-term memory - reuse what worked)"]
        for e in episodes:
            try:
                outcome = json.loads(e.get("test_result", "{}"))
            except Exception:
                outcome = {"raw": e.get("test_result", "")}
            ref = (e.get("reflection") or "").replace("\n", " ")[:160]
            lines.append(f"- [{e.get('timestamp','?')}] \"{(e.get('task') or '')[:120]}\" -> "
                         f"passed={outcome.get('passed','?')} failed={outcome.get('failed','?')} "
                         f"(commit {e.get('git_commit_hash') or 'n/a'}) | lesson: {ref or 'n/a'}")
        return "\n".join(lines)
