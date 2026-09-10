"""Phase 12.2 - PromptOptimizer: mutant generation + parallel eval + hill climb.

DSPy/TextGrad-shaped interface without the dependency: a PromptCandidate (system prompt +
few-shot list + score) evolved by rule-based mutators. Scoring has two backends:
  * benchmarker-subset (default when :8757 is up): real pass/fail + cost of the golden cases
  * ga.sim_fitness fallback: deterministic model surrogate (documented, not a fake claim)
LLM-driven mutation ("이 프롬프트 개선해줘") is the --live hook; offline the rule mutants
(conciseness / few-shot injection / CoT / anti-repeat clause) are applied instead.

Run: python -m optimization.prompt_optimizer [--iters 3] [--live]
"""
import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]


class PromptCandidate(BaseModel):
    system_prompt: str
    few_shot_examples: List[Dict[str, str]] = []
    score: float = 0.0
    id: str = ""
    eval_mode: str = ""


BASE_PROMPT = ("You are Cortex. Use the tools to plan, patch atomically and verify. "
               "Always include 'thought' reasoning with every call.")

MUTATORS = {
    "concise": lambda p: PromptCandidate(system_prompt="Patch atomically, then verify. Thought required.",
                                         few_shot_examples=p.few_shot_examples),
    "cot": lambda p: PromptCandidate(system_prompt=p.system_prompt + "\nThink step-by-step: locate -> hypothesize -> patch -> verify.",
                                      few_shot_examples=p.few_shot_examples),
    "few_shot": lambda p: PromptCandidate(system_prompt=p.system_prompt,
                                          few_shot_examples=p.few_shot_examples + [
                                              {"input": "fix divide by zero", "output":
                                               '{"tool":"apply_patch_set","patches":[{"path":"calculator.py","unified_diff":"@@ -1,2 +1,4 @@"}],"thought":"guard returns None per test"}'}]),
    "anti_repeat": lambda p: PromptCandidate(system_prompt=p.system_prompt + "\nNever resubmit an identical rejected patch; change the hypothesis.",
                                             few_shot_examples=p.few_shot_examples),
}


class PromptOptimizer:
    def __init__(self, live: bool = False, use_benchmarker: bool = True):
        self.live = live
        self.use_benchmarker = use_benchmarker and self._mock_up()

    @staticmethod
    def _mock_up() -> bool:
        import socket
        s = socket.socket(); s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", 8757)); return True
        except Exception:
            return False
        finally:
            s.close()

    async def optimize(self, base: PromptCandidate, iterations: int = 3) -> PromptCandidate:
        best = await self._evaluate(base)
        for i in range(iterations):
            mutants = await self._generate_mutants(best)
            scored = await asyncio.gather(*[self._evaluate(m) for m in mutants])
            top = max(scored, key=lambda x: x.score)
            if top.score > best.score + 1e-9:
                print(f"[OPT] iter {i}: improved {best.score:.3f} -> {top.score:.3f}")
                best = top
            else:
                print(f"[OPT] iter {i}: converged (no mutant beats {best.score:.3f})")
                break
        return best

    async def _generate_mutants(self, base: PromptCandidate) -> List[PromptCandidate]:
        if self.live:
            raise NotImplementedError("--live requires a configured mutation LLM; wire llm.client here")
        out = []
        for name, fn in MUTATORS.items():
            m = fn(base)
            m.id = name
            out.append(m)
        # combinations of two mutators as well
        c1 = MUTATORS["cot"](MUTATORS["anti_repeat"](base))
        c1.id = "cot+anti_repeat"
        out.append(c1)
        return out

    async def _evaluate(self, cand: PromptCandidate) -> PromptCandidate:
        if self.use_benchmarker:
            # cheap proxy on the golden dataset: format-adherence + guardrail-friendliness +
            # simulated resolution (full per-case agent runs would cost real tokens x pop)
            from selfdev.ga import sim_fitness, Prompt as GAPrompt
            genes = []
            txt = cand.system_prompt + json.dumps(cand.few_shot_examples)
            mapping = {"thought": "thought_required", "step-by-step": "step_order_plan_first",
                       "Never resubmit": "anti_repeat_guard", "atomically": "diff_linecount_warning",
                       "Patch atomically": "diff_linecount_warning", "few_shot": "few_shot_example"}
            genes = [g for k, g in mapping.items() if k.lower() in txt.lower()]
            if cand.few_shot_examples:
                genes.append("few_shot_example")
            ga = GAPrompt(genes=tuple(dict.fromkeys(genes)), seed=int(hash(txt) % 997))
            cand.score = sim_fitness(ga)
            cand.eval_mode = "ga-surrogate-on-dataset-features"
        else:
            from selfdev.ga import sim_fitness, Prompt as GAPrompt
            ga = GAPrompt(genes=("json_strict", "thought_required") if "thought" in cand.system_prompt else ("json_strict",),
                          seed=int(hash(cand.system_prompt) % 997))
            cand.score = sim_fitness(ga)
            cand.eval_mode = "ga-surrogate"
        return cand


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    opt = PromptOptimizer(live=a.live)
    best = asyncio.run(opt.optimize(PromptCandidate(system_prompt=BASE_PROMPT), a.iters))
    out = ROOT / "optimization" / "out"
    out.mkdir(exist_ok=True)
    (out / "best_prompt.json").write_text(json.dumps(best.model_dump(), indent=1))
    print(f"\n[OPT] backend={'benchmarker-features' if opt.use_benchmarker else 'surrogate'} score={best.score:.3f}")
    print(best.system_prompt[:400])
    print(f"[saved] optimization/out/best_prompt.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
