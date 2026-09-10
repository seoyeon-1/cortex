"""Phase 7.3b - experiment tracking: wandb/mlflow-shaped dry-run client.

Writes `runs/<name>/metrics.jsonl` + summary.json in research/. If wandb is installed AND
research.live=true in config, it would log for real - default is local journal (safe).
"""
import json
import time
from pathlib import Path


class Run:
    def __init__(self, name: str, root: Path | str = None):
        self.dir = (Path(root) if root else Path(__file__).resolve().parent) / "runs" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.m = (self.dir / "metrics.jsonl").open("a", encoding="utf-8")
        self.started = time.time()

    def log(self, **metrics) -> None:
        self.m.write(json.dumps({"ts": round(time.time(), 3), **metrics}) + "\n")

    def finish(self, **summary) -> Path:
        self.m.close()
        out = {"wall_s": round(time.time() - self.started, 2), **summary}
        (self.dir / "summary.json").write_text(json.dumps(out, indent=1))
        return self.dir


def live_backend():
    """returns 'wandb' | 'mlflow' | 'local-journal' - only if config opts in."""
    try:
        import yaml
        cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config.yaml", encoding="utf-8")) or {}
        if (cfg.get("research", {}) or {}).get("live"):
            import wandb  # noqa: F401
            return "wandb"
    except Exception:
        pass
    return "local-journal"
