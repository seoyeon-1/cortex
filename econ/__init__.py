"""Phase 9 - autonomous economic agent (SIMULATED money, real discipline).

wallet.py  : simulated balance + ledger; runtime budget enforcement lives in the Orchestrator
             (`econ.budget_usd` -> 60% pressure -> model downgrade -> hard stop). No private
             keys are generated, stored, or handled anywhere in cortex - by design.
bounty.py  : bounty board scoring -> accept only above confidence threshold -> command plan +
             simulated escrow ledger (hold/release). dry_run default; there is no network call.
No crypto dependencies, no transactions. To go real you would plug a signer in behind
Wallet.sign() deliberately - this module refuses to be accidentally dangerous.
"""
