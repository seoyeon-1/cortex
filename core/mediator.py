"""Phase 6.5d - DecisionLog + Mediator: negotiate conflicts, persist every verdict.

record() is called by the Orchestrator after each consensus vote. On a conflict (typically
"tests green but security/architect blocks") it runs one deterministic negotiation round:
positions from every specialist -> cross-concessions -> a compromise the orchestrator can
actually execute (interim guard NOW + follow-up tickets), and appends the compromise to the
next iteration's feedback so the LLM implements the agreed shape instead of re-debating it.

Every record lands in TWO places (readable + machine-readable):
  cortex/docs/decisions/DEC-*.md      (human log, transcript style)
  cortex/.cortex_memory/decisions.jsonl (audit stream)
Tickets: cortex/.cortex_memory/tickets/TICKET-*.md (follow-up work is never just prose).
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from protocol.negotiation import (Compromise, CounterOffer, DecisionRecord,
                                  NegotiationRound, Position)


class DecisionLog:
    def __init__(self, cortex_root: str | Path):
        self.md_dir = Path(cortex_root) / "docs" / "decisions"
        self.jsonl = Path(cortex_root) / ".cortex_memory" / "decisions.jsonl"
        self.tickets_dir = Path(cortex_root) / ".cortex_memory" / "tickets"

    # ------------------------------------------------------------------ api
    def record(self, orch, task: str, iteration: int, verdicts: Dict[str, Any],
               accepted: bool, score: float, blocked_by: List[str], negotiate: bool = False) -> Path:
        rec = DecisionRecord(decision_id=DecisionRecord.new_id(), task=task[:200], iteration=iteration,
                             accepted=accepted, weighted_score=round(float(score), 3), blocked_by=list(blocked_by))
        w = (getattr(orch, "policy", {}) or {}).get("weights", {}) or {}
        pos = [Position(agent=k, stance="accept" if v.passed else "block",
                        weight=float(w.get(k, 0.34)), rationale=(v.summary or "")[:160],
                        asks=[f"[{f.get('severity')}] {f.get('description', '')[:110]}"
                              for f in v.findings if f.get("severity") in ("CRITICAL", "HIGH", "MEDIUM")][:3])
               for k, v in verdicts.items()]
        rnd = NegotiationRound(positions=pos)
        if negotiate and pos:
            rnd.counters = self._counters(pos)
            rec.compromise = self._mediate(task, verdicts, blocked_by, pos)
            if rec.compromise:
                rnd.resolved = True
                rec.outcome = ("conflict -> compromise: " + rec.compromise.adopted
                               + (" (+%d ticket(s))" % len(rec.compromise.tickets) if rec.compromise.tickets else ""))
                # the deal is binding for the next iteration: append it to the feedback context
                guard = f"\n- interim guard: {rec.compromise.interim_guard}" if rec.compromise.interim_guard else ""
                tix = "\n- follow-up ticket(s): " + ", ".join(Path(t).name for t in rec.compromise.tickets) if rec.compromise.tickets else ""
                orch._feedback_ctx = (orch._feedback_ctx or "") + (
                    "\n\n## MEDIATED COMPROMISE (agreed - implement this shape)\n"
                    f"- adoption: {rec.compromise.adopted}{guard}{tix}")
        rec.rounds = [rnd]
        if not rec.outcome:
            rec.outcome = ("accepted" if accepted else "rejected") + f" (weighted {score:.2f})"

        self.md_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        md = self.md_dir / f"{rec.decision_id}.md"
        md.write_text(self._render(rec), encoding="utf-8")
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(rec.model_dump_json() + "\n")
        return md

    # ------------------------------------------------------------ internals
    @staticmethod
    def _counters(pos: List[Position]) -> List[CounterOffer]:
        """Deterministic cross-concessions - who gives what for whom."""
        blockers = [p for p in pos if p.stance == "block"]
        allies = [p for p in pos if p.stance == "accept"]
        counters: List[CounterOffer] = []
        for b in blockers:
            for a in allies:
                if a.agent == "qa":
                    counters.append(CounterOffer(from_agent="qa", to_agent=b.agent,
                                                 concession="test suite is green and stays green",
                                                 requires="no public-interface changes in this patch"))
                elif a.agent == "architect":
                    counters.append(CounterOffer(from_agent="architect", to_agent=b.agent,
                                                 concession="guard can live at the adapter/middleware layer",
                                                 requires="layering direction stays downward-only"))
        if blockers and "backend effort" not in " ".join(c.concession for c in counters):
            counters.append(CounterOffer(from_agent="orchestrator", to_agent=blockers[0].agent,
                                         concession="full fix scheduled as its own ticket (tracked, not dropped)",
                                         requires="interim guard merged now"))
        return counters

    def _mediate(self, task: str, verdicts: Dict[str, Any], blocked_by: List[str],
                 pos: List[Position]) -> Compromise:
        """The orchestrator's ruling - template rules keep it deterministic & reviewable."""
        blockers = [p for p in pos if p.stance == "block"]
        sec = next((p for p in blockers if p.agent == "security"), None)
        if sec:
            tickets = [self._ticket("security", a) for a in sec.asks[:2]]
            return Compromise(
                adopted=f"merge now ONLY with interim guard; durable fix ticketed",
                interim_guard=self._guard_for(sec),
                tickets=tickets,
                rationale="security veto is absolute for the durable bar, but a compensating "
                          "control at the boundary unblocks delivery without accepting the risk")
        if blocked_by and "weighted-score" not in blocked_by and all(not v.passed for v in verdicts.values()):
            return Compromise(adopted="re-scope: minimal patch that makes tests green, then re-enter review",
                              rationale="every persona objects - shrink the change")
        return Compromise(adopted="accept current guardrails; advisory findings ticketed only",
                          tickets=[self._ticket(k, a) for p in blockers for a in p.asks[:1] for k in [p.agent]],
                          rationale="no veto - low weight total; advisories do not block the release")

    @staticmethod
    def _guard_for(sec: Position) -> str:
        text = " ".join(sec.asks).lower()
        if "sql" in text:
            return "validate/whitelist the input at the caller (API layer) before the SQL sink; no raw interpolation even in the interim"
        if "eval" in text or "exec" in text:
            return "deny-list check at entry; replace eval path with dispatch table when ticket lands"
        return "add an explicit validation hook at the boundary with a fail-closed default"

    def _ticket(self, agent: str, ask: str) -> str:
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", ask.lower()).strip("-")[:40] or "item"
        path = self.tickets_dir / f"TICKET-{datetime.now(timezone.utc).strftime('%H%M%S%f')[:9]}-{agent}-{slug}.md"
        path.write_text(f"# Ticket ({agent} follow-up)\n\n- created: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                        f"- origin: mediated compromise (docs/decisions)\n\n> {ask}\n\n"
                        f"- [ ] durable fix\n- [ ] regression test proving the bar\n", encoding="utf-8")
        return str(path)

    @staticmethod
    def _render(rec: DecisionRecord) -> str:
        lines = [f"# {rec.decision_id}", "",
                 f"- task: {rec.task}", f"- iteration: {rec.iteration}",
                 f"- outcome: **{rec.outcome}**", f"- accepted: {rec.accepted} | weighted: {rec.weighted_score}",
                 "", "## Positions"]
        for r in rec.rounds:
            for p in r.positions:
                lines.append(f"- **{p.agent}** ({p.stance}, w={p.weight}): {p.rationale}")
                for a in p.asks:
                    lines.append(f"    - ask: {a}")
            if r.counters:
                lines.append("")
                lines.append("## Counter-offers")
                for c in r.counters:
                    lines.append(f"- {c.from_agent} -> {c.to_agent}: {c.concession}" + (f" (requires: {c.requires})" if c.requires else ""))
        if rec.compromise:
            lines += ["", "## Compromise (binding)",
                      f"- adopted: {rec.compromise.adopted}"]
            if rec.compromise.interim_guard:
                lines.append(f"- interim guard: {rec.compromise.interim_guard}")
            for t in rec.compromise.tickets:
                lines.append(f"- ticket: {Path(t).name}")
            lines.append(f"- rationale: {rec.compromise.rationale}")
        return "\n".join(lines) + "\n"
