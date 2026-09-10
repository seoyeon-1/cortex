import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.tools.base import BaseTool
from core.tools.code_edit import CodeEditTool
from core.tools.test_runner import TestRunnerTool
from core.tools.file_ops import ReadFileTool, ListFilesTool
from core.tools.search_tool import SearchCodeTool
from core.tools.patch_set import ApplyPatchSetTool
from core.memory.context import ContextBuilder
from core.memory.retriever import HybridRetriever
from core.runtime.events import SessionLog
from core.llm.client import LLMClient
from rich.console import Console
from rich.panel import Panel
from rich.json import JSON

console = Console()

class Orchestrator:
    def __init__(self, workspace_root: str, config: Dict):
        self.workspace_root = workspace_root
        self.config = config
        self.llm = LLMClient(config["llm"])
        self.tools: Dict[str, BaseTool] = {}
        self.session = None  # Phase 5: created in run(); None-safe emit everywhere

        # Base tools (manifest v1.0 contract; file_ops kept for back-compat, deprecated in prompt)
        self._register(ReadFileTool(workspace_root))
        self._register(ListFilesTool(workspace_root))
        # Phase 15.1: static pre-flight simulator (in-memory apply + ast/lint/imports), advisory-first.
        self._preflight = None
        _pf_cfg = config.get("preflight", {}) or {}
        if _pf_cfg.get("enabled", True):
            try:
                from core.simulation import PreflightSimulator
                self._preflight = PreflightSimulator(workspace_root, _pf_cfg)
            except Exception as e:
                console.print(f"[yellow]Preflight simulator unavailable ({e})[/yellow]")
        self._register(CodeEditTool(workspace_root, preflight=self._preflight))
        self._register(TestRunnerTool(workspace_root, config["sandbox"]["timeout_seconds"]))

        # Phase 2: hybrid retrieval (graph + vectors). Degrade gracefully if indexable deps fail.
        self.retriever = None
        try:
            self.retriever = HybridRetriever(workspace_root)
            stats = self.retriever.index_codebase(workspace_root)
            console.print(f"[dim]Retriever indexed: {stats}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Retriever unavailable ({e}) - falling back to trace-based context.[/yellow]")
        self._register(SearchCodeTool(workspace_root, retriever=self.retriever))
        self._register(ApplyPatchSetTool(workspace_root, timeout=config["sandbox"]["timeout_seconds"],
                                         preflight=self._preflight))

        # Phase 3: persistent memory (episodic store / ADR journal / skill library) - cortex-side,
        # survives sandbox teardown. Degrades gracefully.
        self.cortex_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.episodes = self.adr = self.skills = self.skill_learner = None
        self._applied_patches: List[Dict] = []
        try:
            from core.memory.episodic import EpisodeStore
            from core.memory.adr import ADRGenerator
            from core.memory.skills import SkillLibrary, SkillLearner
            self.episodes = EpisodeStore(self.cortex_root)
            self.adr = ADRGenerator(workspace_root, adr_dir=os.path.join(self.cortex_root, "docs", "adr"))
            self.skills = SkillLibrary(os.path.join(self.cortex_root, "skills"))
            self.skill_learner = SkillLearner(self.cortex_root)
            console.print(f"[dim]Episodic memory: {self.episodes.count()} episode(s)[/dim]")
        except Exception as e:
            console.print(f"[yellow]Persistent memory unavailable ({e})[/yellow]")

        # Phase 4: observability + infra + chaos (all degrade to a clear ToolResult when unconfigured)
        self.deploy_guard = self.slo_checker = None
        try:
            from core.tools.observability import build_observability_tools, SLOChecker
            from core.tools.infra import KubectlTool, TerraformTool, DeployGuard
            from core.chaos import ChaosTool
            obs_tools, mc = build_observability_tools(config)
            for t in obs_tools:
                self._register(t)
            self.slo_checker = SLOChecker(mc, config.get("slo", {}))
            infra_cfg = config.get("infra", {}) or {}
            kctl = KubectlTool(infra_cfg)
            self._register(kctl)
            self._register(TerraformTool(infra_cfg, workspace_root))
            self.deploy_guard = DeployGuard(kctl, self.slo_checker, config)
            self._register(ChaosTool(config.get("chaos", {}), kubectl=kctl, slo_checker=self.slo_checker))
            console.print("[dim]Phase 4 tools: query_metrics/logs/traces, kubectl, terraform, chaos_experiment[/dim]")
        except Exception as e:
            console.print(f"[yellow]Phase 4 ops tools unavailable ({e})[/yellow]")

        # Phase 15.2: structured-reasoning enforcement (parser fallback + protocol prompt).
        so_cfg = config.get("structured_output", {}) or {}
        self.structured = None
        extra_sys = ""
        if so_cfg.get("enabled", True):
            try:
                from core.parsing import StructuredOutputParser
                self.structured = StructuredOutputParser()
                if so_cfg.get("append_protocol_prompt", True):
                    _pp = os.path.join(self.cortex_root, "prompts", "react_structured.md")
                    if os.path.exists(_pp):
                        extra_sys = "\n\n" + open(_pp, encoding="utf-8").read()
            except Exception as e:
                console.print(f"[yellow]Structured parser unavailable ({e})[/yellow]")
        self.context_builder = ContextBuilder(
            workspace_root, 
            config["context"]["max_total_tokens"], 
            config["llm"]["model"],
            retriever=self.retriever,
            extra_system_prompt=extra_sys
        )
        self.max_iterations = config["sandbox"]["max_iterations"]
        self.history: List[Dict] = []

        # Phase 6: expert sub-agents over the A2A protocol + consensus policy.
        # agents.enabled=false (or import failure) degrades to the classic single-agent loop.
        self.society_cfg = config.get("agents", {}) or {}
        self.policy: Dict[str, Any] = self.society_cfg.get("policy", {}) or {}
        self.sub_agents: Dict[str, Any] = {}
        self._last_verdicts: Dict[str, Any] = {}
        self._feedback_ctx = ""
        self._review_blocked = False
        self.negotiation_enabled = bool((config.get("negotiation", {}) or {}).get("enabled", True))

        # Phase 8: model routing + self-improvement hooks; Phase 9: budget-enforced economics.
        self.router = None
        try:
            from core.llm.router import ModelRouter
            self.router = ModelRouter(config.get("llm", {}))
        except Exception:
            pass
        self.econ_cfg = config.get("econ", {}) or {}
        self.selfdev_cfg = config.get("selfdev", {}) or {}
        self._budget = self.econ_cfg.get("budget_usd") or (config.get("econ", {}) or {}).get("budget_usd")
        self._budget = float(self._budget) if self._budget else None
        self._downgraded = False
        self._last_task_route_done = False
        self.wallet = None
        # Phase 10.2: hard cost governance (any limit present activates the governor)
        self.governor = None
        _cg = config.get("cost", {}) or {}
        if any(_cg.get(k) for k in ("max_tokens", "max_usd", "max_wall_time_sec", "max_calls_per_min")):
            try:
                from core.runtime.cost_governor import Budget, CostGovernor
                self.governor = CostGovernor(Budget.from_cfg(_cg))
                console.print(f"[dim]cost governor: { {k: v for k, v in _cg.items() if v} }[/dim]")
            except Exception as e:
                console.print(f"[yellow]cost governor off ({e})[/yellow]")
        self.tracing = False
        if (config.get("tracing", {}) or {}).get("enabled", True):
            try:
                from observability.tracing import init_tracing
                self.tracing = init_tracing("cortex-agent",
                                            Path(self.cortex_root) / ".cortex_memory" / "traces")
            except Exception:
                pass
        # Phase 10.3: guardrails (entry sanitizer + tool-call capability checks)
        self.guard = None
        _gc = config.get("guardrails", {}) or {}
        if _gc.get("enabled", True):
            try:
                from security.guardrails import Guardrails, SecurityPolicy
                pol = SecurityPolicy(**{k: v for k, v in _gc.items() if k in SecurityPolicy.model_fields})
                self.guard = Guardrails(pol, journal=Path(self.cortex_root) / ".cortex_memory" / "security.jsonl")
                extra = _gc.get("extra_allowed_tools") or []
                if extra:
                    self.guard.allow_extra_tools(extra)
                console.print(f"[dim]guardrails: mode={pol.mode} allowed_tools={len(pol.allowed_tools)}"
                              f"{' +extra:' + str(len(extra)) if extra else ''}[/dim]")
            except Exception as e:
                console.print(f"[yellow]guardrails off ({e})[/yellow]")
        # Phase 16: secretary kit - registered here (AgentLoop shim inherits), each tool degrades
        # gracefully when playwright/credentials are absent; policy-allowed explicitly.
        self.browser = self.email_tool = self.intel = self.form_filler = None
        _sec_cfg = config.get("secretary", {}) or {}
        if _sec_cfg.get("enabled", True):
            try:
                import types as _ty
                from core.tools.secretary import (BrowserTool, CompetitionIntelTool, EmailTool,
                                                   FormFillerTool)
                self.browser = BrowserTool(workspace_root, _sec_cfg)
                self.email_tool = EmailTool(workspace_root, _sec_cfg)
                self.intel = CompetitionIntelTool(workspace_root, _sec_cfg, llm_cfg=config.get("llm", {}))
                self.form_filler = FormFillerTool(workspace_root, _sec_cfg, llm_cfg=config.get("llm", {}))
                _grec = lambda _s, p, c, m: (self.governor.record(self._durable_id, p, c, m)
                                             if getattr(self, "governor", None)
                                             and getattr(self, "_durable_id", None) else None)
                _gov = _ty.SimpleNamespace(record=_grec)   # LLM sub-calls join the loop's budget
                self.intel.governor = _gov
                self.form_filler.governor = _gov
                for _t in (self.browser, self.email_tool, self.intel, self.form_filler):
                    self._register(_t)
                if self.guard:
                    self.guard.allow_extra_tools(["browser", "email", "analyze_competition", "fill_form"])
                console.print("[dim]Phase 16 secretary: browser, email, analyze_competition, fill_form[/dim]")
            except Exception as e:
                console.print(f"[yellow]secretary tools unavailable ({e})[/yellow]")

        # Phase 10.1/14.2: durable event-sourced steps + time-machine snapshots.
        self.executor = None
        self.recorder = None
        if (config.get("runtime", {}) or {}).get("durable", {}).get("enabled", True):
            try:
                from core.runtime.durable_executor import DurableExecutor
                self.executor = DurableExecutor(os.path.join(self.cortex_root, ".cortex_memory", "durable.db"))
            except Exception as e:
                console.print(f"[yellow]durable executor off ({e})[/yellow]")
        if (config.get("observability", {}) or {}).get("recorder", {}).get("enabled", True):
            try:
                from observability.session_recorder import SessionRecorder
                self.recorder = SessionRecorder(os.path.join(self.cortex_root, ".cortex_memory", "sessions.db"))
            except Exception as e:
                console.print(f"[yellow]session recorder off ({e})[/yellow]")
        if self.econ_cfg.get("enabled", False):
            try:
                from econ.wallet import Wallet
                self.wallet = Wallet(self.cortex_root, self.econ_cfg)
            except Exception as e:
                console.print(f"[yellow]wallet disabled ({e})[/yellow]")
        if self.society_cfg.get("enabled", True):
            try:
                from agents.security import SecurityAgent
                from agents.architect import ArchitectAgent
                from agents.qa import QAAgent
                for _key, _cls in (("security", SecurityAgent), ("architect", ArchitectAgent), ("qa", QAAgent)):
                    _sc = (self.society_cfg.get("sub_agents", {}) or {}).get(_key, {}) or {}
                    if _sc.get("enabled", True):
                        self.sub_agents[_key] = _cls(_sc)
                console.print(f"[dim]Agent society online: {', '.join(self.sub_agents) or '(none - single-agent mode)'}[/dim]")
            except Exception as e:
                console.print(f"[yellow]Sub-agents unavailable ({e}) - single-agent mode.[/yellow]")
        self.agent_versions: Dict[str, str] = {}
        reg_cfg = config.get("registry", {}) or {}
        if reg_cfg.get("enabled", False) and self.sub_agents is not None:
            try:
                from registry.manifest import Registry
                self.registry = Registry(os.path.join(self.cortex_root, reg_cfg.get("dir", "registry"))).load()
                for m in self.registry.all():
                    sc = (self.society_cfg.get("sub_agents", {}) or {}).get(m.key, {}) or {}
                    if not sc.get("enabled", True):
                        continue
                    agent = self.registry.instantiate(m, {**m.config_defaults, **sc})
                    self.sub_agents[m.key] = agent          # version-pinned persona (registry = marketplace)
                    self.agent_versions[m.key] = f"{m.name}@{m.version}"
                console.print(f"[dim]Registry: {len(self.agent_versions)} persona(s) -> "
                              + ", ".join(f"{k} v{v.split('@')[1]}" for k, v in self.agent_versions.items()) + "[/dim]")
            except Exception as e:
                console.print(f"[yellow]Registry unavailable ({e}) - using built-in personas.[/yellow]")
        else:
            self.registry = None

    def _register(self, tool) -> None:
        # Phase 15.4: bounded auto-recovery for transient tool failures (policy denies never retry).
        rec = self.config.get("recovery", {}) or {}
        if rec.get("enabled") and not type(tool).__name__ == "ResilientToolWrapper":
            try:
                from core.tools.resilient_wrapper import RECOVERY_STRATEGIES, ResilientToolWrapper
                strats = RECOVERY_STRATEGIES.get(tool.name, RECOVERY_STRATEGIES["default"])
                if "max_retries" in rec:
                    strats = [dict(s, max=int(rec["max_retries"])) for s in strats if int(s.get("max", 1))]
                tool = ResilientToolWrapper(tool, strategies=strats,
                                            backoff_sec=float(rec.get("backoff_sec", 0.2)))
            except Exception as e:
                console.print(f"[yellow]Recovery wrapper unavailable ({e})[/yellow]")
        self.tools[tool.name] = tool

    def run(self, task: str, resume: bool = False, task_id: Optional[str] = None) -> bool:
        import hashlib as _hl
        if self.guard:
            try:
                safe = self.guard.sanitize_input(task, where="task")
                if safe != task:
                    console.print("[bold red]guardrails: injection patterns REDACTED from task (see .cortex_memory/security.jsonl)[/bold red]")
                    task = safe
            except Exception as e:  # SecurityViolationError in block mode
                console.print(f"[bold red]GUARDRAILS BLOCKED TASK: {e}[/bold red]")
                return False
        self._durable_id = task_id or _hl.sha1(task.encode("utf-8")).hexdigest()[:12]
        self._replayed = {}
        if self.executor:
            if resume:
                self._replayed = self.executor.completed_outputs(self._durable_id)
                crashed = self.executor.get_incomplete(self._durable_id)
                console.print(f"[cyan]RESUME task={self._durable_id}: replaying {len(self._replayed)} completed step(s)"
                              + (f", crash-point was '{crashed[0].step_name}'" if crashed else "") + "[/cyan]")
                for name, out in self._replayed.items():
                    console.print(f"[dim]  replay {name}: {str(out)[:110]}[/dim]")
                if any(out.get("accepted") for out in self._replayed.values()):
                    console.rule("[bold green]RESUME: an accepted step is already durable - task complete, nothing to redo.")
                    self._done(task, type("R", (), {"data": {}})(), True, len(self._replayed))
                    return True
            else:
                done = self.executor.completed_outputs(self._durable_id)
                if done:
                    console.print(f"[yellow]task already has {len(done)} completed durable step(s) - pass resume=True to continue, "
                                  f"or this run will append fresh steps.[/yellow]")
        self._trace_stack = None
        try:
            if self.tracing:
                from contextlib import ExitStack
                from observability.tracing import span as _sp
                self._trace_stack = ExitStack()
                self._trace_stack.enter_context(_sp("cortex.run", task=task[:160]))
        except Exception:
            self._trace_stack = None
        console.rule("[bold blue]CORTEX AUTONOMOUS LOOP STARTED")
        console.print(f"[yellow]TASK:[/yellow] {task}")
        if self.session is not None:
            self.session.task = task
        elif getattr(self, "cortex_root", None):
            try:
                self.session = SessionLog(self.cortex_root, task=task)
            except Exception:
                self.session = None
        if self.session is not None:
            self.session.emit("run_start", task=task, workspace=str(self.workspace_root), tools=sorted(self.tools))

        if self.router and self._last_task_route_done is not True:
            m, why = self.router.route(task)
            if m:
                self.llm.model = m
                console.print(f"[dim]Model route: {m} ({why})[/dim]")
            if self.session:
                self.session.emit("model_route", model=self.llm.model, reason=why)
        if self.selfdev_cfg.get("prompt_injection", True):
            try:
                frag = Path(self.cortex_root, "selfdev", "out", "promoted_prompt.md")
                if frag.exists():
                    self._promoted_prompt = frag.read_text(encoding="utf-8")[:900]
                    console.print("[dim]selfdev: promoted prompt fragment active[/dim]")
            except Exception:
                pass

        console.print("[cyan]Running initial tests to establish baseline...[/cyan]")
        test_result = self.tools["run_tests"].execute(thought="Establish baseline test status")
        console.print(JSON.from_data(test_result.data))
        self._emit_test("baseline", test_result)

        if test_result.success:
            console.print("[green]All tests already passing. Nothing to do.[/green]")
            self._done(task, test_result, True, 0)
            return True

        # Phase 3: recall - skills + episodic memory injected as context
        memory_ctx = self._memory_context(task)
        if memory_ctx:
            console.print("[cyan]Long-term memory recalled (skills + past episodes).[/cyan]")
        if getattr(self, "_promoted_prompt", ""):
            memory_ctx = (memory_ctx + "\n\n" if memory_ctx else "") + self._promoted_prompt

        self._iter_patches: List[Dict] = []
        self._iter_thoughts: List[str] = []

        for iteration in range(1, self.max_iterations + 1):
            console.rule(f"[bold magenta]ITERATION {iteration}/{self.max_iterations}")
            _step_name = f"iter:{iteration}"
            if _step_name in self._replayed:
                console.print(f"[cyan]durable: skipping {_step_name} (already COMPLETED)[/cyan]")
                continue
            self._iter_patches, self._iter_thoughts = [], []
            self._durable_evt = self.executor.begin(self._durable_id, _step_name,
                                                     {"iteration": iteration}) if self.executor else None
            
            extra = memory_ctx
            if self._feedback_ctx:  # Phase 6: expert review feedback rides along until the review passes
                extra = (extra + "\n\n" if extra else "") + self._feedback_ctx
            messages = self.context_builder.build_initial_context(task, test_result.data, extra_context=extra)

            if self.history:
                reflection = self._generate_reflection()
                messages.append({"role": "user", "content": f"## PREVIOUS ATTEMPTS REFLECTION\n{reflection}\n\n## CURRENT TEST STATUS\n{self._format_test_results(test_result.data)}"})

            tool_schemas = [t.to_openai_function() for t in self.tools.values()]

            _est_in = sum(len(m.get("content", "") or "") for m in messages) // 4
            if self.session:
                self.session.emit("llm_request", iteration=iteration, est_prompt_tokens=_est_in,
                                  messages=len(messages), tools=len(tool_schemas))
            if self.governor:  # HARD limit: refuse BEFORE paying
                try:
                    self.governor.pre_check(self._durable_id, _est_in, self.llm.model)
                except Exception as e:  # BudgetExceededError
                    console.print(f"[bold red]COST GOVERNOR: {e} - run stopped (hard limit).[/bold red]")
                    if self.session:
                        self.session.emit("budget_exceeded", iteration=iteration, governor=str(e))
                    if getattr(self, "_durable_evt", None):
                        self.executor.finish(self._durable_evt, "FAILED", {"reason": "cost governor"})
                    self._record_episode(task, test_result, False)
                    self._done(task, test_result, False, iteration)
                    return False
            try:
                if self.tracing:
                    from observability.tracing import span as _sp
                    with _sp("llm.chat", model=self.llm.model, est_prompt_tokens=_est_in, tools=len(tool_schemas)):
                        response = self.llm.chat(messages, tools=tool_schemas, tool_choice="auto")
                else:
                    response = self.llm.chat(messages, tools=tool_schemas, tool_choice="auto")
                msg = response.choices[0].message
                est_out = len(msg.content or "") // 4 + sum(len(tc.function.arguments or "") for tc in (msg.tool_calls or [])) // 4
                if self.session:
                    self.session.emit("llm_response", iteration=iteration, est_completion_tokens=est_out,
                                      called=[tc.function.name for tc in (msg.tool_calls or [])])
                if self.governor:
                    _u = getattr(response, "usage", None)
                    try:
                        self.governor.record(self._durable_id,
                                             getattr(_u, "prompt_tokens", 0) or _est_in,
                                             getattr(_u, "completion_tokens", 0) or est_out,
                                             self.llm.model)
                    except Exception as e:
                        console.print(f"[bold red]COST GOVERNOR (post-call): {e} - stopping.[/bold red]")
                        if self.session:
                            self.session.emit("budget_exceeded", iteration=iteration, governor=str(e))
                        if getattr(self, "_durable_evt", None):
                            self.executor.finish(self._durable_evt, "FAILED", {"reason": "cost governor"})
                        self._record_episode(task, test_result, False)
                        self._done(task, test_result, False, iteration)
                        return False

                if not msg.tool_calls and self.structured is not None and (msg.content or "").strip():
                    # Phase 15.2: recover a protocol-conformant text reply (Thought/Plan/ToolCalls)
                    # instead of wasting the iteration on a generic nudge.
                    _sr = self.structured.parse(msg.content)
                    if _sr.ok:
                        try:
                            msg.tool_calls = self.structured.build_native_calls(_sr)
                            console.print(f"[cyan]Structured recovery: executing {len(_sr.tool_calls)} recovered call(s).[/cyan]")
                            if self.session:
                                self.session.emit("structured_recovery", iteration=iteration,
                                                  calls=[c["name"] for c in _sr.tool_calls])
                        except Exception:
                            msg.tool_calls = None
                if not msg.tool_calls:
                    console.print("[yellow]LLM did not call tools. Forcing action...[/yellow]")
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    if self.structured is not None:
                        _errs = "; ".join(self.structured.parse(msg.content or "").errors) or "no tool calls"
                        messages.append({"role": "user", "content": self.structured.retry_prompt(_errs)})
                    else:
                        messages.append({"role": "user", "content": "You MUST use tools (edit_file, run_tests) to proceed. Analyze the errors and produce a patch."})
                    continue

                for tc in msg.tool_calls:
                    tool_name = tc.function.name
                    args = json.loads(tc.function.arguments)

                    if self.session:
                        self.session.emit("tool_call", iteration=iteration, tool=tool_name,
                                          thought=args.get("thought", ""),
                                          summary_args={k: (str(v)[:160]) for k, v in args.items() if k != "thought"})

                    console.print(Panel(f"[bold]TOOL CALL:[/bold] {tool_name}\n[dim]{json.dumps(args, ensure_ascii=False, indent=2)}[/dim]", border_style="yellow"))

                    if tool_name not in self.tools:
                        console.print(f"[red]Unknown tool: {tool_name}[/red]")
                        continue

                    if self.guard:  # capability gate: denied call becomes a lesson, not a crash
                        try:
                            self.guard.validate_tool_call(tool_name, args)
                        except Exception as ge:
                            console.print(Panel(f"[bold red]GUARDRAILS REFUSED {tool_name}[/bold red]\n{ge}",
                                                border_style="red"))
                            if self.session:
                                self.session.emit("tool_result", iteration=iteration, tool=tool_name, success=False,
                                                  summary=f"blocked by guardrails: {ge}"[:400], error="SecurityViolation")
                            self.history.append({"iteration": iteration, "tool": tool_name, "args": args,
                                                 "result": f"guardrails: {ge}", "success": False})
                            continue

                    tool = self.tools[tool_name]
                    if self.tracing:
                        from observability.tracing import trace_tool_call
                        with trace_tool_call(tool_name, args):
                            result: ToolResult = tool.execute(**args)
                    else:
                        result: ToolResult = tool.execute(**args)

                    border = "green" if result.success else "red"
                    console.print(Panel(f"[bold]RESULT:[/bold] {result.summary}\n{result.error or 'OK'}", border_style=border))
                    if self.session:
                        self.session.emit("tool_result", iteration=iteration, tool=tool_name, success=result.success,
                                          summary=(result.summary or "")[:400], error=(result.error or "")[:400])

                    self.history.append({"iteration": iteration, "tool": tool_name, "args": args, "result": result.summary, "success": result.success})

                    if result.success and tool_name in ("edit_file", "apply_patch_set"):
                        ps = ([{"path": args.get("path", "?"), "unified_diff": args.get("unified_diff", "")}]
                              if "unified_diff" in args else list(args.get("patches", [])))
                        if ps:
                            if self.session:
                                self.session.emit("patch", iteration=iteration, files=[p.get("path", "?") for p in ps],
                                                  added=sum(1 for p in ps for l in p.get("unified_diff", "").splitlines() if l.startswith("+")),
                                                  removed=sum(1 for p in ps for l in p.get("unified_diff", "").splitlines() if l.startswith("-")))
                            self._applied_patches.extend(ps)
                            self._iter_patches.extend(ps)
                            if args.get("thought"):
                                self._iter_thoughts.append(args["thought"])

            except Exception as e:
                console.print(f"[bold red]LLM/Tool Execution Error: {e}[/bold red]")
                self.history.append({"iteration": iteration, "error": str(e)})
                continue

            console.print("[cyan]Verifying changes...[/cyan]")
            test_result = self.tools["run_tests"].execute(thought="Verify if the applied patch fixed the tests")
            console.print(JSON.from_data(test_result.data))
            self._emit_test(f"iteration {iteration} verify", test_result)

            if self._iter_patches:
                self._on_verified(task, test_result)
                if self.sub_agents:
                    self._run_society_review(task, iteration)

            if self._budget:  # Phase 9: "don't spend more than $X" is a HARD runtime constraint
                spent = self._run_cost_usd()
                if spent > self._budget * 0.6 and not self._downgraded and not self._review_blocked:
                    cheap = self.router.cheapest() if self.router else None
                    if cheap and cheap != self.llm.model:
                        self.llm.model = cheap
                        console.print(f"[yellow]budget pressure ({spent:.4f} > 60% of {self._budget:.4f}) - downgraded model to {cheap}[/yellow]")
                    self._downgraded = True
                    if self.session:
                        self.session.emit("model_downgrade", iteration=iteration, model=self.llm.model, spent=round(spent, 6))
                elif spent > self._budget:
                    console.print(f"[bold red]BUDGET EXCEEDED: ${spent:.5f} > ${self._budget:.5f} - stopping (no half-paid runs).[/bold red]")
                    if self.session:
                        self.session.emit("budget_exceeded", spent=round(spent, 6), cap=self._budget)
                    self._record_episode(task, test_result, False)
                    self._done(task, test_result, False, iteration)
                    return False

            if test_result.success and not self._review_blocked:
                console.rule("[bold green]SUCCESS: All tests passing! (society verdict: accepted)")
                self._snapshot(iteration, test_result, accepted=True)
                if self.executor and getattr(self, "_durable_evt", None):
                    self.executor.finish(self._durable_evt, "COMPLETED",
                                         {"accepted": True, "files": sorted({str(p.get("path", "?")) for p in self._iter_patches})})
                self._record_episode(task, test_result, True)
                self._done(task, test_result, True, iteration)
                return True
            if test_result.success and self._review_blocked:
                console.print("[bold red]Tests are green, but the expert society BLOCKED acceptance - see feedback above.[/bold red]")
            self._snapshot(iteration, test_result, accepted=False)
            if self.executor and getattr(self, "_durable_evt", None):
                self.executor.finish(self._durable_evt, "COMPLETED",
                                     {"accepted": False, "files": sorted({str(p.get("path", "?")) for p in self._iter_patches}),
                                      "test": {k: int((test_result.data or {}).get(k, 0) or 0) for k in ("passed", "failed")}})
            
            time.sleep(1)

        self._record_episode(task, test_result, False)
        self._done(task, test_result, False, self.max_iterations)
        console.rule("[bold red]MAX ITERATIONS REACHED. TASK FAILED.")
        return False

    # ------------------------------------------------ Phase 6: agent society (A2A)
    def _run_society_review(self, task_desc: str, iteration: int) -> None:
        """Dispatch a review Task to every specialist (concurrently), then vote consensus."""
        import asyncio as _aio
        from protocol.a2a import EventKind, Task

        negotiation_enabled = self.negotiation_enabled
        files = sorted({str(p.get("path", "?")) for p in self._iter_patches})
        skill_of = {"security": "scan_security", "architect": "check_architecture", "qa": "verify_quality",
                    "dba": "review_sql", "performance": "profile_changes"}
        parent_run = getattr(self.session, "run_id", "") if self.session else ""
        # Decompose -> ROUTE: with a registry, persona manifests decide who reviews this patch.
        targets = self._route_targets(task_desc, files)
        if not targets:
            return

        async def _all():
            async def one(key, agent):
                t = Task(summary=f"review changes for: {task_desc[:120]}", skill=skill_of.get(key, "review"),
                         payload={"root": self.workspace_root, "files": files}, parent_run=parent_run)

                def log(ev):
                    if self.session is not None and ev.kind == EventKind.PROGRESS:
                        self.session.emit("agent_progress", agent=key, message=(ev.message or "")[:300])

                if getattr(self, "tracing", False):
                    from observability.tracing import span as _sp
                    with _sp(f"a2a.{key}", skill=t.skill, files=len(t.payload.get("files", []))):
                        return key, await agent.run_task(t, on_event=log)
                return key, await agent.run_task(t, on_event=log)
            return await _aio.gather(*[one(k, targets[k]) for k in targets])

        try:
            results = _aio.run(_all())
        except Exception as e:
            console.print(f"[yellow]Society review skipped ({e})[/yellow]")
            self._review_blocked = False
            return

        verdicts = {k: ev.artifact for k, ev in results}
        self._last_verdicts = verdicts
        ok, score, blocked_by = self._consensus(verdicts)
        for k, a in verdicts.items():
            console.print(Panel(f"[bold]{a.summary}[/bold]\nscore={a.score} findings={len(a.findings)}",
                                title=f"{'PASS' if a.passed else 'FAIL'} :: {k}-agent",
                                border_style="green" if a.passed else "red"))
            if self.session is not None:
                self.session.emit("verdict", agent=k, iteration=iteration, passed=a.passed, score=a.score,
                                  summary=a.summary[:300], findings=a.findings[:12])

        failed = [k for k, v in verdicts.items() if not v.passed]
        if self.session is not None:
            self.session.emit("consensus", iteration=iteration, accepted=ok, weighted_score=round(score, 3),
                              blocked_by=blocked_by, verdicts={k: (v.passed, v.score) for k, v in verdicts.items()})

        # SYNTHESIZE: merge the society's views into one decision record (versions included).
        synthesis = {
            "decision": "accepted" if ok else "rejected",
            "weighted_score": score,
            "blocked_by": blocked_by,
            "personas": {k: {"passed": v.passed, "score": v.score, "summary": v.summary[:200],
                             "version": self.agent_versions.get(k, "builtin")} for k, v in verdicts.items()},
            "advisories": [f"{k}: {f['description'][:90]}" for k, v in verdicts.items()
                           for f in v.findings if f.get("severity") in ("LOW", "INFO")][:6],
        }
        if self.session is not None:
            self.session.emit("synthesis", iteration=iteration, **{k: synthesis[k] for k in
                              ("decision", "weighted_score", "blocked_by")})
        console.print(Panel("[bold]" + synthesis["decision"].upper() + f"[/bold] w={score:.2f} | "
                            + " ".join(f"{k}:{'PASS' if v['passed'] else 'FAIL'}" for k, v in synthesis["personas"].items())
                            + (f" | follow-ups: {len(synthesis['advisories'])}" if synthesis["advisories"] else ""),
                            title="SYNTHESIS (decompose -> route -> synthesize)", border_style="cyan"))
        if ok and negotiation_enabled:
            self._log_decision(task_desc, iteration, verdicts, ok, score, blocked_by)
        self._review_blocked = not ok
        if not ok:
            lines = []
            for k in (blocked_by or failed or list(verdicts)):
                for f in verdicts[k].findings:
                    if f.get("severity") in ("CRITICAL", "HIGH", "MEDIUM"):
                        lines.append(f"- [{f['severity']}] {k}-agent: {f['description'][:200]} "
                                     f"@ {f.get('file','')}:{f.get('line',0)}  FIX: {str(f.get('suggested_fix',''))[:140]}")
            self._feedback_ctx = ("## EXPERT REVIEW FEEDBACK (BLOCKING - must fix before acceptance)\n"
                                  + "\n".join(lines[:10])
                                  + "\nKeep tests green while addressing these. Re-submit the patch set after fixing.")
            if negotiation_enabled:
                self._log_decision(task_desc, iteration, verdicts, ok, score, blocked_by, negotiate=True)
            console.print(f"[bold red]CONSENSUS: REJECTED (weighted {score:.2f}) by {', '.join(blocked_by) or 'policy'} - feedback injected[/bold red]")
        else:
            if self._feedback_ctx:
                console.print("[green]Expert feedback resolved - clearing review context.[/green]")
            self._feedback_ctx = ""
            console.print(f"[green]CONSENSUS: ACCEPTED (weighted score {score:.2f})[/green]")

    def _route_targets(self, task_desc: str, files: List[str]) -> Dict[str, Any]:
        if self.registry is None:
            return dict(self.sub_agents)  # no marketplace -> everyone reviews everything (Phase 6.1 behavior)
        out = {}
        for key, agent in self.sub_agents.items():
            m = self.registry.latest(key)
            if m is None or getattr(agent, "manifest", None) is None:
                out[key] = agent  # built-in without a listing: always on (back-compat)
            elif m.route_hit(task_desc, files):
                out[key] = agent
        if out:
            console.print(f"[dim]Route -> {', '.join(out)} (of {len(self.sub_agents)} personas)[/dim]")
        return out

    def _log_decision(self, task_desc: str, iteration: int, verdicts: Dict[str, Any],
                      accepted: bool, score: float, blocked_by: List[str], negotiate: bool = False) -> None:
        try:
            from core.mediator import DecisionLog
            self._decision_log = getattr(self, "_decision_log", None) or DecisionLog(self.cortex_root)
            self._decision_log.record(self, task_desc, iteration, verdicts, accepted, score, blocked_by,
                                       negotiate=negotiate)
        except Exception as e:
            console.print(f"[yellow]decision log skipped ({e})[/yellow]")

    def _consensus(self, verdicts: Dict[str, Any]):
        """Disagreement resolution: veto agents bypass weights; otherwise weighted average vs threshold.

        weights: security 0.5 > architect 0.3 > qa 0.2 (config agents.policy).
        """
        w: Dict[str, float] = self.policy.get("weights", {}) or {}
        thr = float(self.policy.get("consensus_threshold", 0.5))
        veto_on = bool(self.policy.get("veto", True))
        total = sum(w.get(k, 0.0) for k in verdicts) or 1.0
        score = sum(w.get(k, 0.0) * (1.0 if v.passed else float(v.score or 0.0)) for k, v in verdicts.items()) / total
        veto_fail = [k for k, v in verdicts.items() if (not v.passed) and veto_on and k in w and w[k] >= 0.4]
        low_weight = score < thr
        blocked = veto_fail + ([k for k, v in verdicts.items() if not v.passed and k not in veto_fail] if low_weight else [])
        return (not veto_fail and not low_weight), round(score, 3), sorted(set(blocked))

    def _snapshot(self, iteration: int, test_result, accepted: bool) -> None:
        if not self.recorder:
            return
        try:
            sid = getattr(self.session, "run_id", None) or self._durable_id
            d = test_result.data or {}
            self.recorder.save_snapshot(sid, iteration, {
                "task_id": self._durable_id, "iteration": iteration, "accepted": accepted,
                "history_tail": self.history[-4:],
                "patch_files": sorted({str(p.get("path", "?")) for p in self._iter_patches}),
                "tests": {k: int(d.get(k, 0) or 0) for k in ("passed", "failed", "errors")},
                "verdicts": {k: (v.passed, v.score) for k, v in self._last_verdicts.items()},
                "feedback_head": (self._feedback_ctx or "")[:300]})
        except Exception:
            pass

    def _run_cost_usd(self) -> float:
        try:
            if not self.session:
                return 0.0
            pin = pout = 0
            for ev in self.session.read():
                if ev["kind"] == "llm_request":
                    pin += ev["data"].get("est_prompt_tokens", 0)
                elif ev["kind"] == "llm_response":
                    pout += ev["data"].get("est_completion_tokens", 0)
            pricing = ((self.config.get("dashboard", {}) or {}).get("pricing", {}) or {}).get(
                self.llm.model, {"prompt": 0.15, "completion": 0.60})
            self._last_task_route_done = True
            return pin / 1e6 * pricing.get("prompt", 0) + pout / 1e6 * pricing.get("completion", 0)
        except Exception:
            return 0.0

    # ------------------------------------------------------- Phase 5 event telemetry
    def _emit_test(self, phase: str, test_result) -> None:
        if not self.session:
            return
        d = test_result.data or {}
        self.session.emit("test", phase=phase, success=test_result.success,
                          passed=d.get("passed", 0), failed=d.get("failed", 0), errors=d.get("errors", 0),
                          failures=[f.get("test", "?") for f in d.get("failures", [])][:6])

    def _done(self, task: str, test_result, success: bool, iterations: int) -> None:
        if getattr(self, "_trace_stack", None):
            try:
                self._trace_stack.close()
            except Exception:
                pass
            self._trace_stack = None
        # Phase 8: failed runs become DPO preference pairs; a later success closes the pair.
        try:
            from selfdev.failures import close_open_pair, record_failure
            if success and self._applied_patches:
                kw = [w for w in re.findall(r"[a-z가-힣]{4,}", task.lower())][:3]
                if close_open_pair(kw, self._applied_patches):
                    console.print("[dim]selfdev: closed a DPO pair from this success[/dim]")
            elif not success:
                record_failure(prompt_tail=" | ".join(str(h.get("tool", h.get("error", ""))) for h in self.history[-3:]),
                               rejected_patches=[{"path": p.get("path"), "diff_head": str(p.get("unified_diff"))[:220]}
                                                 for p in (self._iter_patches or self._applied_patches[-1:] or [])],
                               verdicts={k: (v.passed, v.score) for k, v in self._last_verdicts.items()}, task=task)
                console.print("[dim]selfdev: failure mined -> .cortex_memory/feedback/dpo_pairs.jsonl[/dim]")
        except Exception as e:
            console.print(f"[dim]selfdev mining skipped ({e})[/dim]")
        if self.wallet is not None and self.session is not None:
            try:
                self.wallet.charge(self._run_cost_usd(), purpose=f"run:{getattr(self.session, 'run_id', '?')}")
            except Exception:
                pass
        if not self.session:
            return
        d = test_result.data or {}
        self.session.emit("done", success=success, iterations=iterations,
                          passed=d.get("passed", 0), failed=d.get("failed", 0))
        self.session.finalize(success=success, iterations=iterations,
                              applied_files=sorted({str(p.get("path", "?")) for p in self._applied_patches}),
                              model=self.config["llm"].get("model", "?"))

    # ------------------------------------------------------------- Phase 3 memory
    def _git_head(self) -> str:
        try:
            return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.workspace_root,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    def _memory_context(self, task: str) -> str:
        blocks: List[str] = []
        try:
            if self.skills:
                sk = self.skills.context_for(task)
                if sk:
                    blocks.append(sk)
                    console.print(f"[cyan]Active skill(s): {', '.join(s.name for s in self.skills.match(task))}[/cyan]")
        except Exception:
            pass
        try:
            if self.episodes and self.episodes.count():
                ctx = self.episodes.as_context(self.episodes.similarity_search(task, top_k=3))
                if ctx:
                    blocks.append(ctx)
                # Phase 15.3: successful past trajectories as worked few-shot examples
                if (self.config.get("context", {}) or {}).get("episodic_fewshot", True):
                    fs = self.episodes.fewshot_for(task, top_k=2)
                    if fs:
                        blocks.append(fs)
        except Exception:
            pass
        try:
            if self.adr:
                rec = self.adr.recent(2)
                if rec:
                    blocks.append("## RECENT DECISION LOGS (cortex/docs/adr)\n" + "\n".join(f"- {r['file']}" for r in rec))
        except Exception:
            pass
        return "\n\n".join(blocks)

    def _on_verified(self, task: str, test_result) -> None:
        """ADR auto-generation trigger (Phase 3.2) + skill pattern observation (Phase 3.3)."""
        try:
            data = test_result.data or {}
            counts = {k: int(data.get(k, 0) or 0) for k in ("passed", "failed", "errors")}
            if self.adr:
                adr = self.adr.record(task, " | ".join(dict.fromkeys(t for t in self._iter_thoughts if t)),
                                      list(self._iter_patches), counts,
                                      reflection=self._generate_reflection() if self.history else "",
                                      git_commit_hash=self._git_head())
                if adr:
                    console.print(f"[green]ADR recorded:[/green] docs/adr/{os.path.basename(str(adr))}")
            if test_result.success and self.skill_learner:
                tools = sorted({h["tool"] for h in self.history if h.get("success") and "tool" in h})
                files = sorted({str(p.get("path", "?")) for p in self._iter_patches})
                proposal = self.skill_learner.observe_success(task, tools, files)
                if proposal:
                    console.print(Panel(f"[bold]Repeating pattern detected[/bold] ({proposal['count']}x): {proposal['pattern']}\n"
                                        f"Draft skill: {proposal['draft']}\n"
                                        f"Register as a skill?  ->  [bold]python -m core.memory.skills promote {proposal['slug']}[/bold]",
                                        border_style="cyan"))
        except Exception as e:
            console.print(f"[yellow]ADR/skill persist failed (non-fatal): {e}[/yellow]")

    def _record_episode(self, task: str, test_result, success: bool) -> None:
        if not self.episodes:
            return
        try:
            data = test_result.data or {}
            self.episodes.record(
                task=task,
                plan=json.dumps([{"iter": h.get("iteration"), "tool": h.get("tool"), "ok": h.get("success")}
                                for h in self.history if "tool" in h], ensure_ascii=False)[:1800],
                patches=[str(p.get("unified_diff", ""))[:800] for p in self._applied_patches],
                test_result=json.dumps({k: int(data.get(k, 0) or 0) for k in ("passed", "failed", "errors")} | {"ok": success}),
                reflection=(self._generate_reflection() if self.history else "") or ("green on first accepted patch set" if success else ""),
                git_commit_hash=self._git_head(),
                success=bool(success),
                trajectory=[{"tool": h.get("tool"), "success": h.get("success"),
                             "args": {"path": (h.get("args") or {}).get("path", "")}}
                            for h in self.history if "tool" in h][-25:])
            console.print(f"[green]Episode recorded:[/green] {self.episodes.count()} total in agent memory")
        except Exception as e:
            console.print(f"[yellow]Episode record failed (non-fatal): {e}[/yellow]")

    def _format_test_results(self, tr: Dict) -> str:
        return f"Passed: {tr.get('passed', 0)}, Failed: {tr.get('failed', 0)}, Errors: {tr.get('errors', 0)}"

    def _generate_reflection(self) -> str:
        log = "\n".join([f"Iter {h['iteration']}: {h['tool']} -> {'OK' if h['success'] else 'FAIL'} ({h['result'][:100]})" for h in self.history[-3:]])
        return f"Recent history:\n{log}\n\nAnalyze why previous patches failed. Avoid repeating same mistakes. Formulate a NEW hypothesis."

AgentLoop = Orchestrator  # back-compat alias (Phase 6 rename)
