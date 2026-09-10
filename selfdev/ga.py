"""Phase 8.3 - prompt evolution: genetic population over system-prompt sections.

fitness(prompt) defaults to a deterministic SIMULATOR (this sandbox has no live LLM):
a model agent is modeled per task class - strictness genes raise format-adherence and
patch-correctness but cost tokens; verbosity genes trade tokens for fewer missing-thought
errors. It is honest about this: `--fitness real` documents the hook where you plug actual
task-success rate (run per candidate) in.

    python -m selfdev.ga [--gens 8] [--pop 10] [--seed 3]

The winner is written to selfdev/out/promoted_prompt.md - the Orchestrator injects it into
context when selfdev.prompt_injection is on (config default: off).
"""
import argparse
import hashlib
import random
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "selfdev" / "out"

GENES = {  # name: (0|1 presence gene, effect on class-success, token cost)
    "json_strict":  (+0.18, 120),
    "few_shot_example": (+0.10, 300),
    "diff_linecount_warning": (+0.22, 90),
    "thought_required": (+0.06, 60),
    "step_order_plan_first": (+0.09, 80),
    "anti_repeat_guard": (+0.14, 70),
    "korean_echo_policy": (+0.05, 40),
    "verbose_preamble": (-0.06, 240),
}
TASK_CLASSES = ["multi-file-patch", "single-edit", "sql-refactor", "test-fix", "refactor-safe", "chat-style"]
FRAGMENTS = {
    "json_strict": "Return ONLY valid JSON tool calls; no prose outside arguments.",
    "few_shot_example": "Example:\n  {{\"name\":\"apply_patch_set\",\"arguments\":{{\"patches\":[...]}}}}",
    "diff_linecount_warning": "Unified diff hunk header MUST equal actual +/- line counts.",
    "thought_required": "Every call includes a non-empty 'thought' (plan-level reasoning).",
    "step_order_plan_first": "1) inspect 2) patch set atomically 3) verify; never edit-then-think.",
    "anti_repeat_guard": "Never re-submit a patch set with the same target files after a rejection.",
    "korean_echo_policy": "Restate the user's intent in their language before acting.",
    "verbose_preamble": "Begin with a detailed essay about general software engineering practice.",
}


@dataclass
class Prompt:
    genes: Tuple[str, ...] = ()
    seed: int = 0

    def text(self) -> str:
        base = "You are Cortex. Operate the tools.\n"
        return base + "\n".join(f"- {FRAGMENTS[g]}" for g in sorted(self.genes))

    @property
    def tokens(self) -> int:
        return 60 + sum(GENES[g][1] for g in self.genes)


def sim_fitness(p: Prompt, tasks: int = 12, rng: random.Random = None) -> float:
    """deterministic stand-in for measured task-resolution rate (0..0.99).

    Diminishing returns: each matching gene adds less than the last, and every 100 prompt
    tokens cost accuracy (context pressure) - so a bigger, well-chosen set wins, but an
    all-genes blob does not."""
    score = 0.0
    for i in range(tasks):
        tclass = TASK_CLASSES[i % len(TASK_CLASSES)]
        vals = []
        for g in p.genes:
            eff = GENES[g][0] * (1.5 if (g, tclass) in _synergy() or tclass in _needs(g) else 0.7)
            vals.append(eff)
        vals.sort(reverse=True)
        base = 0.42 + sum(v * (0.9 ** k) for k, v in enumerate(vals))       # diminishing returns
        penalty = 0.006 * p.tokens / 100                                     # context pressure
        h = int(hashlib.md5(f"{p.seed}:{i}:{p.genes}".encode()).hexdigest()[:6], 16) / 0xFFFFFF
        score += max(0.0, min(0.99, base - penalty + 0.10 * (h - 0.5)))
    return round(score / tasks, 4)


def _needs(g: str) -> List[str]:
    return {"diff_linecount_warning": ["multi-file-patch", "single-edit", "sql-refactor", "test-fix"],
            "anti_repeat_guard": ["refactor-safe", "multi-file-patch"],
            "json_strict": TASK_CLASSES,
            "step_order_plan_first": ["refactor-safe", "sql-refactor"]}.get(g, [])


def _synergy():
    return {("thought_required", "refactor-safe"), ("few_shot_example", "multi-file-patch"),
            ("anti_repeat_guard", "sql-refactor")}


def evolve(pop: int, gens: int, seed: int) -> Tuple[Prompt, List[Dict]]:
    rng = random.Random(seed)
    pool = [Prompt(genes=tuple(rng.sample(list(GENES), rng.randint(1, 4))), seed=seed) for _ in range(pop)]
    history = []
    for g in range(gens):
        scored = sorted(((sim_fitness(p, rng=rng), p) for p in pool), key=lambda t: -t[0])
        history.append({"gen": g, "best": round(scored[0][0], 4), "mean": round(sum(s for s, _ in scored) / len(scored), 4),
                        "genes": list(scored[0][1].genes)})
        elite = [p for _, p in scored[: max(2, pop // 5)]]
        nxt = list(elite)
        while len(nxt) < pop:
            mom, dad = rng.sample(elite + [p for _, p in scored], 2) if len(scored) > 1 else (elite[0], elite[0])
            union = set(mom.genes) | set(dad.genes)
            child = tuple(x for x in union if rng.random() < 0.65)
            child = tuple(sorted(set(child) ^ {m for m in GENES if rng.random() < 0.10}))
            nxt.append(Prompt(genes=child, seed=seed))
        pool = nxt
    best = max(pool, key=lambda p: sim_fitness(p, rng=rng))
    return best, history


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pop", type=int, default=10)
    ap.add_argument("--gens", type=int, default=8)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--fitness", choices=["sim", "real"], default="sim",
                    help="'real' requires a live LLM harness (not wired in this build)")
    a = ap.parse_args()
    if a.fitness == "real":
        print("[ga] real fitness not configured in this build; falling back to sim")
    best, hist = evolve(a.pop, a.gens, a.seed)
    print("gen  best   mean   genes")
    for h in hist:
        print(f"{h['gen']:>3}  {h['best']:.4f} {h['mean']:.4f}  {','.join(h['genes'])[:58]}")
    assert hist[-1]["best"] >= hist[0]["best"] - 1e-9, "GA must not degrade"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "promoted_prompt.md").write_text(
        f"## PROMPT EVOLUTION (gen {hist[-1]['gen']}, fitness {hist[-1]['best']})\n" + best.text() + "\n", encoding="utf-8")
    print(f"\npromoted -> selfdev/out/promoted_prompt.md\nfitness: {hist[0]['best']} -> {hist[-1]['best']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
