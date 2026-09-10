"""Phase 8 - Recursive self-improvement (Cortex develops Cortex).

audit.py      : session-log waste analysis -> proposals (e.g. prompt caching)
ga.py         : prompt population evolution (deterministic simulator; swap fitness_fn for real LLM)
failures.py   : failed runs -> DPO/GRPO-ready preference pairs (local jsonl, never uploaded)
evolve.py     : architecture-evolution PROPOSALS ONLY (rust rewrite / monolith split / async)

Nothing here mutates cortex code by itself: output is reports + patches-for-review, and any
apply still goes through the normal loop + tests + (society) gates - human `y` remains the merge.
"""
