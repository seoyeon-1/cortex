"""Phase 5.1 - File-based event bus for collaboration UX.

Every AgentLoop run writes `<cortex>/.cortex_memory/sessions/<run_id>.jsonl`
(one JSON event per line: run_start / llm_request / tool_call / tool_result /
test / patch / done) plus a `.meta.json` summary. File-based on purpose:
the dashboard, the GitHub-App job view and the CLI can tail sessions from
SEPARATE processes without shared state or sockets.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


class SessionLog:
    def __init__(self, cortex_root: str | Path, task: str = "", run_id: Optional[str] = None):
        self.dir = Path(cortex_root) / ".cortex_memory" / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
        self.task = task
        self.path = self.dir / f"{self.run_id}.jsonl"
        self.meta_path = self.dir / f"{self.run_id}.meta.json"
        self._start = datetime.now(timezone.utc)
        self._counts: Dict[str, int] = {}

    def emit(self, kind: str, **data) -> None:
        try:  # telemetry must never break the agent
            ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, "data": data}
            self._counts[kind] = self._counts.get(kind, 0) + 1
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def finalize(self, **meta) -> Path:
        try:
            stats = self.stats()
            stats.update({k: v for k, v in meta.items() if v is not None})
            self.meta_path.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        return self.meta_path

    def stats(self) -> Dict:
        tokens_in = tokens_out = 0
        for ev in self.read():
            if ev["kind"] == "llm_request":
                tokens_in += ev["data"].get("est_prompt_tokens", 0)
            if ev["kind"] == "llm_response":
                tokens_out += ev["data"].get("est_completion_tokens", 0)
        return {"run_id": self.run_id, "task": self.task, "events": self._counts,
                "started": self._start.isoformat(timespec="seconds"),
                "duration_s": round((datetime.now(timezone.utc) - self._start).total_seconds(), 1),
                "est_prompt_tokens": tokens_in, "est_completion_tokens": tokens_out,
                "file": self.path.name}

    def read(self) -> List[Dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    @staticmethod
    def list_sessions(cortex_root: str | Path) -> List[Dict]:
        d = Path(cortex_root) / ".cortex_memory" / "sessions"
        metas = []
        if d.exists():
            for m in sorted(d.glob("*.meta.json")):
                try:
                    metas.append(json.loads(m.read_text(encoding="utf-8")))
                except Exception:
                    continue
        metas.sort(key=lambda x: x.get("started", ""), reverse=True)
        return metas
