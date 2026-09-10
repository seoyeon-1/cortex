"""Phase 10.1 - DurableExecutor: event-sourced execution state (SQLite WAL).

Every step of a run is journaled (STARTED -> COMPLETED/FAILED) with serializable in/out
state. After a crash (spot reclaim / OOM / network loss) `resume` replays completed steps'
outputs and continues at the first non-completed step - long tasks never restart from zero.

Fixes on top of the spec draft: rows are deserialized back into ExecutionEvent (JSON
columns), plus crash forensics (get_incomplete: STARTED rows whose step never completed)
and replayable outputs per step.
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ExecutionEvent:
    event_id: str
    task_id: str
    step_name: str
    input_state: dict      # serializable input
    output_state: dict     # serializable output
    status: str            # STARTED, COMPLETED, FAILED, COMPENSATING
    timestamp: float
    error: str = ""

    @classmethod
    def from_row(cls, row) -> "ExecutionEvent":
        return cls(row[0], row[1], row[2], json.loads(row[3] or "{}"), json.loads(row[4] or "{}"),
                   row[5], row[6], row[7] or "")


class DurableExecutor:
    def __init__(self, db_path: str | Path = "cortex_execution.log.db"):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_name TEXT NOT NULL,
                input_state TEXT NOT NULL,
                output_state TEXT,
                status TEXT NOT NULL,
                timestamp REAL NOT NULL,
                error TEXT
            );""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_task_step ON events(task_id, timestamp);")
        self.conn.commit()

    @contextmanager
    def step(self, task_id: str, step_name: str, input_state: dict):
        """`with executor.step(t, name, in) as save: ...; save(out_dict)`"""
        event_id = str(uuid.uuid4())
        self._append(ExecutionEvent(event_id, task_id, step_name, input_state, {}, "STARTED", time.time()))
        output: Dict = {}
        error = ""
        status = "COMPLETED"
        try:
            yield lambda out: output.update(out or {})
        except Exception as e:
            status, error = "FAILED", f"{type(e).__name__}: {e}"
            raise
        finally:
            self._update(event_id, status, output, error)

    def _append(self, evt: ExecutionEvent):
        self.conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                          (evt.event_id, evt.task_id, evt.step_name, json.dumps(evt.input_state, default=str),
                           json.dumps(evt.output_state, default=str), evt.status, evt.timestamp, evt.error))
        self.conn.commit()

    def _update(self, event_id: str, status: str, output: dict, error: str):
        self.conn.execute("UPDATE events SET output_state=?, status=?, error=? WHERE event_id=?",
                          (json.dumps(output, default=str), status, error, event_id))
        self.conn.commit()

    # ---------------------------------------------------------------- read
    def get_last_completed_step(self, task_id: str) -> Optional[ExecutionEvent]:
        cur = self.conn.execute(
            "SELECT * FROM events WHERE task_id=? AND status='COMPLETED' ORDER BY timestamp DESC LIMIT 1",
            (task_id,))
        row = cur.fetchone()
        return ExecutionEvent.from_row(row) if row else None

    def get_failed_steps(self, task_id: str) -> List[ExecutionEvent]:
        cur = self.conn.execute("SELECT * FROM events WHERE task_id=? AND status='FAILED' ORDER BY timestamp", (task_id,))
        return [ExecutionEvent.from_row(r) for r in cur.fetchall()]

    def get_incomplete(self, task_id: str) -> List[ExecutionEvent]:
        """STARTED rows that never reached a terminal state = the crash point."""
        cur = self.conn.execute(
            "SELECT * FROM events e WHERE task_id=? AND status='STARTED' "
            "AND NOT EXISTS (SELECT 1 FROM events m WHERE m.event_id=e.event_id AND m.status!='STARTED')",
            (task_id,))
        return [ExecutionEvent.from_row(r) for r in cur.fetchall()]

    def completed_outputs(self, task_id: str) -> Dict[str, dict]:
        """resume map: step_name -> last COMPLETED output (later re-runs overwrite earlier)."""
        cur = self.conn.execute(
            "SELECT step_name, output_state FROM events WHERE task_id=? AND status='COMPLETED' ORDER BY timestamp",
            (task_id,))
        return {r[0]: json.loads(r[1] or "{}") for r in cur.fetchall()}

    # --------------------------------------------- manual begin/finish (loop integration)
    def begin(self, task_id: str, step_name: str, input_state: Optional[dict] = None) -> str:
        event_id = str(uuid.uuid4())
        self._append(ExecutionEvent(event_id, task_id, step_name, input_state or {}, {}, "STARTED", time.time()))
        return event_id

    def finish(self, event_id: str, status: str = "COMPLETED", output: Optional[dict] = None, error: str = "") -> None:
        self._update(event_id, status, output or {}, error)

    def step_done(self, task_id: str, step_name: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM events WHERE task_id=? AND step_name=? AND status='COMPLETED' LIMIT 1",
                                (task_id, step_name))
        return cur.fetchone() is not None

    def mark(self, task_id: str, step_name: str, status: str, output: Optional[dict] = None):
        """manual completion marker (used by resume to close out a replayed step)."""
        self._append(ExecutionEvent(str(uuid.uuid4()), task_id, step_name, {"replayed": True},
                                    output or {}, status, time.time()))
