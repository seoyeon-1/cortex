"""Phase 7.2 - Hardware-in-the-loop pipeline as a state machine.

    patch -> flash(sim) -> telemetry -> verify -> commit | rollback

Demo scenario (the spec's own): "reduce drone propeller vibration by 20%".
The pipeline generates a PID grid, runs 400+ seeded sims on the twin, "flashes" (dry_run:
journals the sha256+size instead of touching hardware), streams telemetry, verifies the
measured vibration dropped by >= target vs the baseline firmware, then commits or rolls the
firmware file back. Fully offline; deterministic seeds.

Run:  python -m skills.hardware.hil            # drone scenario
"""
import argparse
import json
from pathlib import Path
from typing import Dict, List

from skills.hardware.twin import DigitalTwinClient, DroneTwin, TwinAction

HERE = Path(__file__).resolve().parent
FIRMWARE = HERE / "firmware" / "drone_pid.json"

BASELINE_GAINS = {"kp": 4.0, "ki": 0.0, "kd": 0.02}
CANDIDATE_GRID: List[Dict] = [
    {"kp": 4.0, "ki": 0.0, "kd": 0.02},
    {"kp": 6.0, "ki": 0.0, "kd": 0.02},
    {"kp": 4.0, "ki": 0.0, "kd": 0.35},
    {"kp": 3.0, "ki": 0.4, "kd": 0.5},
    {"kp": 2.2, "ki": 0.8, "kd": 0.72},
    {"kp": 1.6, "ki": 1.1, "kd": 0.9},
]


class HILState:
    IDLE, PATCHED, FLASHED, VERIFIED, COMMITTED, ROLLED_BACK = range(6)
    NAMES = ["idle", "patched", "flashed(sim)", "verified", "committed", "rolled-back"]


class HILPipeline:
    def __init__(self, target_reduction: float = 0.20, dry_run: bool = True, iters: int = 400):
        self.target = target_reduction
        self.client = DigitalTwinClient(profile="drone", dry_run=dry_run)
        self.iters = iters
        self.state = HILState.IDLE
        self.steps: List[Dict] = []

    def _to(self, s: int, **info) -> None:
        self.state = s
        self.steps.append({"state": HILState.NAMES[s], **info})

    def run(self, resume_firmware: bool = True) -> Dict:
        # 0) baseline firmware (persisted from previous commits if any)
        if resume_firmware and FIRMWARE.exists():
            base = json.loads(FIRMWARE.read_text())
        else:
            base = dict(BASELINE_GAINS)
        base_run = DroneTwin(**base, seed=7).run(gust_amp=0.6)
        self._to(HILState.IDLE, baseline=base, baseline_rms=base_run["rms_vibration"])

        # 1) PATCH: "code change" = gain tuning proposal from the twin sweep
        sweep = DroneTwin.tune(CANDIDATE_GRID, iters=self.iters)
        best = {k: v for k, v in sweep["best"].items() if k in BASELINE_GAINS}
        self._to(HILState.PATCHED, generated_patch=best, sims_runned=self.iters // 20 * len(CANDIDATE_GRID),
                 best_mean_rms=sweep["best"]["mean_rms_vibration"])

        # 2) FLASH: dry_run -> journal the image hash instead of touching a device
        image = json.dumps(best, sort_keys=True)
        flash = self.client.deploy(image, version=f"pid-{''.join(str(int(v*100)) for v in best.values())}")
        self._to(HILState.FLASHED, flash=flash)

        # 3) TELEMETRY + VERIFY on the post-flash twin state (same physics, new gains)
        post = DroneTwin(**best, seed=7).run(gust_amp=0.6)
        reduction = 1.0 - post["rms_vibration"] / max(base_run["rms_vibration"], 1e-9)
        stream = []
        import asyncio
        async def _collect():
            async for sd in self.client.telemetry_stream(12):
                stream.append(sd.model_dump())
        asyncio.run(_collect())
        ok = reduction >= self.target
        self._to(HILState.VERIFIED, telemetry_samples=len(stream), post_rms=post["rms_vibration"],
                  reduction_pct=round(reduction * 100, 1), bar_pct=self.target * 100)

        # 4) COMMIT or ROLLBACK
        if ok:
            FIRMWARE.parent.mkdir(parents=True, exist_ok=True)
            FIRMWARE.write_text(json.dumps(best, indent=1))
            self._to(HILState.COMMITTED, firmware_file=str(FIRMWARE.relative_to(HERE.parent.parent)))
        else:
            self.client.deploy(json.dumps(base, sort_keys=True), version="rollback")
            self._to(HILState.ROLLED_BACK)
        verdict = {"success": ok, "steps": self.steps, "sweep_top3": sweep["table"][:3]}
        self.client._log({"op": "hil_run", "success": ok, "reduction_pct": verdict["steps"][-2].get("reduction_pct")})
        return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description="Cortex HIL pipeline (simulated twin; dry_run default)")
    ap.add_argument("--reduction", type=float, default=0.20, help="required vibration reduction (fraction)")
    ap.add_argument("--iters", type=int, default=400)
    ap.add_argument("--real-flash", action="store_true", help="DANGEROUS: attempt real hardware (refused without transport)")
    a = ap.parse_args()
    v = HILPipeline(target_reduction=a.reduction, dry_run=not a.real_flash, iters=a.iters).run()
    for s in v["steps"]:
        print(f"  [{s['state']:^13}] " + " ".join(f"{k}={str(val)[:48]}" for k, val in s.items() if k != "state"))
    print("HIL RESULT:", "COMMITTED" if v["success"] else "ROLLED BACK")
    return 0 if v["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
