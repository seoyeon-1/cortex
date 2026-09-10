"""Phase 14.2 - SessionRecorder: time-machine state snapshots (sqlite WAL + zstd).

pickle -> zstd(level 3) blob per (session_id, step). The orchestrator records a compact
state snapshot at every iteration (history tail, patch targets, test counts, verdict map) -
enough to REPLAY/debug "what the agent knew at step N", not a memory dump. If zstandard is
missing the codec falls back to gzip transparently.
"""
import gzip
import pickle
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

try:
    import zstandard as _zstd
    _CCTX = _zstd.ZstdCompressor(level=3)
    _DCTX = _zstd.ZstdDecompressor()

    def _compress(b: bytes) -> bytes:
        return _CCTX.compress(b)

    def _decompress(b: bytes) -> bytes:
        return _DCTX.decompress(b)

    CODEC = "zstd"
except Exception:  # pragma: no cover - zstandard is in requirements; fallback kept honest
    def _compress(b: bytes) -> bytes:
        return gzip.compress(b, 6)

    def _decompress(b: bytes) -> bytes:
        return gzip.decompress(b)

    CODEC = "gzip"


@dataclass
class SessionSnapshot:
    session_id: str
    step: int
    timestamp: float
    state: Any


class SessionRecorder:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            session_id TEXT, step INTEGER, timestamp REAL, blob BLOB, codec TEXT,
            PRIMARY KEY (session_id, step));""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_session_time ON snapshots(session_id, timestamp);")
        self.conn.commit()

    def save_snapshot(self, session_id: str, step: int, state: Any) -> None:
        blob = _compress(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
        self.conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
                          (session_id, step, time.time(), sqlite3.Binary(blob), CODEC))
        self.conn.commit()

    def load_snapshot(self, session_id: str, step: int) -> Optional[SessionSnapshot]:
        cur = self.conn.execute("SELECT blob, timestamp, codec FROM snapshots WHERE session_id=? AND step=?",
                                (session_id, step))
        row = cur.fetchone()
        if not row:
            return None
        blob, ts, codec = row
        data = _decompress_per(codec, bytes(blob))
        return SessionSnapshot(session_id, step, ts, pickle.loads(data))

    def get_latest_step(self, session_id: str) -> int:
        cur = self.conn.execute("SELECT MAX(step) FROM snapshots WHERE session_id=?", (session_id,))
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else -1

    def list_steps(self, session_id: str) -> List[dict]:
        cur = self.conn.execute("SELECT step, timestamp, length(blob) FROM snapshots WHERE session_id=? ORDER BY step",
                                (session_id,))
        return [{"step": s, "ts": round(t, 1), "bytes": n} for s, t, n in cur.fetchall()]

    def sessions(self) -> List[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT session_id FROM snapshots ORDER BY session_id")]


def _decompress_per(codec: Optional[str], blob: bytes) -> bytes:
    if codec == "gzip":
        return gzip.decompress(blob)
    if CODEC == "zstd":
        return _DCTX.decompress(blob)
    return gzip.decompress(blob)
