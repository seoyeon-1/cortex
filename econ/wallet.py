"""Phase 9.1 - simulated wallet & ledger (budget discipline with zero key material)."""
import json
import time
from pathlib import Path
from typing import Dict, Optional

ADDR_NOTE = "SIMULATED - no keypair exists; swap in a signer intentionally if ever going live"


class Wallet:
    def __init__(self, cortex_root: str | Path, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.dir = Path(cortex_root) / ".cortex_memory"
        self.path = self.dir / "wallet.json"
        self.ledger_path = self.dir / "wallet_ledger.jsonl"
        self.balance = float(cfg.get("balance_usd", 50.0))
        self.daily_cap = float(cfg.get("daily_cap_usd", 5.0))
        self.mode = "simulated"
        self._load()
        self._today_spent = 0.0

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("mode") == self.mode:      # never mix a stale real-ledger idea in
                    self.balance = float(data.get("balance_usd", self.balance))
            except Exception:
                pass

    def snapshot(self) -> Dict:
        out = {"balance_usd": round(self.balance, 5), "daily_cap_usd": self.daily_cap,
               "spent_today": round(self._today_spent, 5), "mode": self.mode, "note": ADDR_NOTE}
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"balance_usd": self.balance, "mode": self.mode}, indent=1))
        return out

    # ------------------------------------------------------------------ ops
    def charge(self, amount_usd: float, purpose: str = "inference") -> bool:
        ok = amount_usd <= max(self.balance, 0) and (self._today_spent + amount_usd) <= self.daily_cap
        rec = {"ts": round(time.time(), 1), "purpose": purpose[:120], "amount_usd": round(amount_usd, 6),
               "approved": bool(ok), "balance_after": round(self.balance - (amount_usd if ok else 0), 6),
               "mode": self.mode}
        if ok:
            self.balance -= amount_usd
            self._today_spent += amount_usd
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        return bool(ok)

    def earn(self, amount_usd: float, purpose: str = "bounty") -> Dict:
        self.balance += amount_usd
        rec = {"ts": round(time.time(), 1), "purpose": purpose[:120], "amount_usd": round(amount_usd, 6),
               "approved": True, "balance_after": round(self.balance, 6), "mode": self.mode}
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
