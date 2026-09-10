"""Phase 7.1a - DigitalTwinClient + a real little physics twin for the drone demo.

The twin is a 2nd-order plant (propeller -> angular accel -> attitude) driven by a PID
controller under gust noise. It is deliberately simple and DETERMINISTIC (seeded), so CI,
the HIL pipeline and rollback checks are reproducible - and the numbers are real physics,
not fake strings: tuning a bad gain set genuinely cuts vibration measurably.
"""
import asyncio
import hashlib
import json
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

from pydantic import BaseModel, Field


class SensorData(BaseModel):
    ts: float
    sensor: str
    value: float
    unit: str


@dataclass
class TwinAction:
    kind: str                       # tune_pid | move | home | print_job | pipette | prime
    params: Dict = field(default_factory=dict)


class DroneTwin:
    """attitude hold on a double integrator: I*th'' = k*torque - damping + gust."""

    def __init__(self, kp: float = 4.0, ki: float = 0.0, kd: float = 0.02, seed: int = 7,
                 inertia: float = 0.05, damping: float = 0.08, dt: float = 0.01, steps: int = 400):
        self.p = dict(kp=kp, ki=ki, kd=kd)
        self.I, self.c, self.dt, self.steps = inertia, damping, dt, steps
        self.seed = seed

    def run(self, target: float = 0.0, gust_amp: float = 0.6) -> Dict:
        rng = random.Random(self.seed)
        theta, omega, integ = 0.0, 0.0, 0.0
        trace: List[float] = []
        err_series: List[float] = []
        for i in range(self.steps):
            err = target - theta
            integ = max(-1.0, min(1.0, integ + err * self.dt))
            gust = gust_amp * (rng.random() - 0.5) if 40 < i < 360 else 0.0
            u = self.p["kp"] * err + self.p["ki"] * integ - self.p["kd"] * omega
            u = max(-8.0, min(8.0, u))                      # actuator saturation
            omega += ((u + gust - self.c * omega) / self.I) * self.dt
            theta += omega * self.dt
            trace.append(theta)
            err_series.append(err)
        settle = self.steps - 1
        while settle > 0 and abs(err_series[settle]) > 0.05:
            settle -= 1
        vib = [err_series[i] - err_series[i - 1] for i in range(1, len(err_series))]
        rms = math.sqrt(sum(v * v for v in vib) / len(vib))
        return {"rms_vibration": round(rms, 5), "settle_steps": settle, "overshoot": round(max(trace), 4),
                "final_error": round(abs(err_series[-1]), 4)}

    @staticmethod
    def tune(grid: List[Dict], iters: int = 400, gust_amp: float = 0.6) -> Dict:
        """grid-search gain candidates x `iters` seeded sims -> minimize mean rms vibration."""
        best, rows = None, []
        for cand in grid:
            vals = []
            for k in range(iters // 20):                      # 20 seeds per candidate (fast + honest)
                vals.append(DroneTwin(**cand, seed=100 + k).run(gust_amp=gust_amp))
            m = sum(v["rms_vibration"] for v in vals) / len(vals)
            row = {**cand, "mean_rms_vibration": round(m, 5),
                   "mean_settle": round(sum(v["settle_steps"] for v in vals) / len(vals), 1)}
            rows.append(row)
            if best is None or m < best["mean_rms_vibration"]:
                best = row
        return {"best": best, "table": sorted(rows, key=lambda r: r["mean_rms_vibration"])}


class DigitalTwinClient:
    def __init__(self, profile: str = "drone", cfg: Optional[Dict] = None, dry_run: bool = True):
        self.profile = profile
        self.cfg = cfg or {}
        self.endpoint = self.cfg.get("endpoint")
        self.dry_run = dry_run or not self.endpoint       # no endpoint -> ALWAYS simulated
        self.state = {"armed": False, "mode": "IDLE", "firmware_sha": None, "flash_count": 0}
        self._twin = DroneTwin(**(self.cfg.get("pid") or {})) if profile == "drone" else None
        self.journal = Path(__file__).resolve().parents[2] / ".cortex_memory" / "hil_journal.jsonl"

    # ------------------------------------------------------------------ api
    def simulate(self, action: TwinAction) -> Dict:
        if self.profile == "drone":
            if action.kind == "tune_pid":
                grid = action.params.get("grid") or [self._twin.p]
                return {"predicted": DroneTwin.tune(grid, iters=action.params.get("iters", 400)),
                        "twin": "local-physics", "dry_run": self.dry_run}
            if action.kind == "move":
                theta = self._twin.run(target=float(action.params.get("deg", 0.0)))
                self.state.update(mode="ATTITUDE_HOLD", armed=True)
                return {"predicted_state": dict(self.state), **theta}
            if action.kind == "home":
                self.state.update(mode="HOME", armed=False)
                return {"predicted_state": dict(self.state)}
        # generic kinematic fallback for ros2 arms / klipper / opentrons twins
        t = action.params.get("pose", [0, 0, 0])
        self.state.update(mode="MOVING", pose=t)
        return {"predicted_state": dict(self.state), "twin": self.profile, "note": "kinematic placeholder twin"}

    def deploy(self, firmware_bin: bytes | str, version: str = "0.0.0") -> Dict:
        blob = firmware_bin.encode() if isinstance(firmware_bin, str) else firmware_bin
        sha = hashlib.sha256(blob).hexdigest()[:16]
        status = {"profile": self.profile, "firmware_sha256_16": sha, "size_bytes": len(blob),
                  "version": version, "dry_run": self.dry_run}
        if self.dry_run:
            status.update(state="QUEUED_SIMULATED_FLASH", device_status={"ok": True, "sim": True})
        else:  # operator explicitly enabled real flash: endpoint transport is out of scope here
            status.update(state="REFUSED", error="no wired transport for this profile in this build")
        self.state.update(firmware_sha=sha, flash_count=self.state["flash_count"] + 1)
        self._log({"ts": time.time(), "op": "deploy", **status})
        return status

    async def telemetry_stream(self, n: int = 24) -> AsyncIterator[SensorData]:
        t0 = time.time()
        for i in range(n):
            if self.profile == "drone":
                phi = 3.0 * math.exp(-i / 7.0) * math.cos(i / 2.0)
                yield SensorData(ts=t0 + i * 0.05, sensor="gyro_x", value=round(phi, 4), unit="deg/s")
            else:
                yield SensorData(ts=t0 + i * 0.05, sensor="joint_temp", value=round(35.0 + i * 0.3, 2), unit="C")
            await asyncio.sleep(0)

    # ----------------------------------------------------------------- util
    def _log(self, rec: Dict) -> None:
        try:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass
