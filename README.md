# Cortex v1.0 - Autonomous Coding Agent

> "Intent is Execution."

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API Key
Edit `config.yaml`:

```yaml
llm:
  api_key: "sk-xxxxxxxxxxxxx"  # Or set env var: export OPENAI_API_KEY="sk-..."
  model: "gpt-4o-mini"
```

### 3. Prepare Target Project (Must be a Git Repo)
```bash
mkdir ../my_project && cd ../my_project
git init
# Create source files (see example below)
git add . && git commit -m "init"
```

### 4. Run Cortex
```bash
cd ../cortex
python main.py "Your task description here" --repo ../my_project
```

## Example Target Project (../my_project)

### calculator.py

```python
def divide(a, b):
    return a / b

def add(a, b):
    return a + b
```

### test_calculator.py

```python
import pytest
from calculator import divide, add

def test_add():
    assert add(2, 3) == 5

def test_divide_normal():
    assert divide(10, 2) == 5.0

def test_divide_by_zero():
    assert divide(10, 0) is None
```

### Run Command

```bash
python main.py "test_divide_by_zero 테스트가 통과하도록 calculator.py의 divide 함수를 수정해줘. 0으로 나누면 None을 반환하게 해." --repo ../my_project
```

## Supported LLM Endpoints

- OpenAI (`gpt-4o`, `gpt-4o-mini`)
- Ollama (`base_url: "http://localhost:11434/v1"`, `api_key: "ollama"`, `model: "codestral"`)
- vLLM, OpenRouter, Azure, Anthropic (via OpenAI proxy)


## Phase 2 - Code Knowledge Graph & Hybrid Retriever

### New Tools (auto-registered in AgentLoop)
| Tool | Purpose |
|---|---|
| `search_code(query, thought, top_k=7)` | RRF-fused hybrid retrieval: semantic vectors (LanceDB, 384-d) + AST knowledge graph (CALLS/IMPORTS/INHERITS) + symbol keywords. Replaces `read_file`/`list_files` as the primary exploration tool. |
| `apply_patch_set(patches[], thought, run_tests=True)` | Atomic multi-file unified-diff application: one `git apply --check` pass for the whole set -> apply -> pytest -> full auto-rollback on failure. |

### Standalone CLI
```bash
python -m core.memory.graph_builder ../my_project --out workspace_graph.json
python -m core.memory.retriever ../my_project "UserService.get_user calls what DB method?" --top_k 5
```

Notes: embeddings use `sentence-transformers/all-MiniLM-L6-v2` when installed; offline the retriever transparently falls back to a deterministic 384-d hash embedder (same dimension contract, same LanceDB schema — drop & recreate on dim change). `read_file`/`list_files`/`edit_file` are kept for back-compat (deprecated in the system prompt).

## Phase 3 - Persistent Memory & Self-Improvement

| Feature | File | Behavior |
|---|---|---|
| Episodic memory | `core/memory/episodic.py` | Every run records an Episode {id, task, plan, patches, test_result, reflection, timestamp, git_commit_hash} into LanceDB (`<cortex>/.cortex_memory/episodes_db`, JSONL+npz fallback). New tasks get top-3 similar episodes injected as context ("already fixed a similar bug 3 weeks ago"). |
| ADR decision log | `core/memory/adr.py` | On every verified patch (`edit_file`/`apply_patch_set` success + test run), writes `docs/adr/YYYYMMDD_HHMMSS_<task-slug>.md` with Context / Decision / Consequences / Alternatives - human-readable and re-injectable. |
| Skill library | `core/memory/skills.py` | Skills = YAML (trigger regex + prompt + Jinja2 template + validate_cmd) in `skills/`, injected when a task matches. Learner counts repeating patch patterns; after 3 successes it drafts `skills/candidates/*.yaml` and proposes: `python -m core.memory.skills promote <slug>`. |

Memory is cortex-side (`.cortex_memory/`, git-ignored) so it survives sandbox teardown and target-repo resets. `python -m core.memory.skills list|candidates|promote|template` for management.

## Phase 4 - Infra / Deploy / Ops Autonomy

Tools (registered by `AgentLoop` when `config.yaml` has the sections; every tool degrades to a clear `ToolResult(False)` - never crashes a run):

| Tool | Backing | Safety |
|---|---|---|
| `query_metrics` / `query_logs` / `query_traces` | Prometheus / Loki / Tempo HTTP | read-only, `observability.*_url` config |
| `kubectl` | kubectl wrapper | verbs allow-list; mutations need `infra.allow_mutations` + `infra.allowed_namespaces` |
| `terraform` | terraform wrapper | plan/`drift` (`-detailed-exitcode`) read-only; `apply` gated by `infra.allow_mutations` |
| `chaos_experiment` | chaos-mesh manifests via kubectl | `chaos.dry_run: true` renders only; live runs SLO-gate the fault window |
| `SLOChecker` + `DeployGuard` | config `slo.rules` | rollout -> poll SLO -> auto `rollout undo` on breach (`core/loop.py` keeps `deploy_guard` wired) |

`config.yaml` ships `observability/slo/infra/chaos` sections with localhost defaults - point them at your OpenTelemetry collector / Prometheus / Loki / Tempo and opt into mutations per environment. `skills/*.yaml` can wrap ops recipes (e.g. "canary: query_metrics + chaos_experiment gate") with the same promotion flow as Phase 3.

## Phase 5 - Human-Agent Collaboration UX

**Runtime event bus** (`core/runtime/events.py`) - every run streams JSONL events
(`run_start / llm_request / llm_response / tool_call / tool_result / patch / test / done`)
to `.cortex_memory/sessions/<run_id>.jsonl` + a `.meta.json`. File-based so any process
(CLI, GitHub bot, dashboard) tails the same feed with no shared memory.

| Piece | Entry point | Notes |
|---|---|---|
| Web dashboard (Cortex Cloud) | `python -m dashboard.server --port 8321` | stdlib-only, **SSE live tail** + session timeline + cost/token dashboard (pricing from `config.yaml:dashboard.pricing`). `GET /`, `/api/sessions`, `/api/sessions/<id>`, `/api/stream`, `/api/stats`. |
| GitHub App / bot | `python -m github_app.server --port 8323` | Verifies `X-Hub-Signature-256` (env `CORTEX_WEBHOOK_SECRET`). Label `cortex:fix` on an issue -> branch `cortex/issue-<n>` -> headless `main.py --yes` -> PR. Label `cortex:verify` on a PR -> pytest -> review comment. `github.dry_run` (auto-on-no-`gh`) journals the exact command plan. |
| VS Code extension | `core/lsp/server.py` | Real LSP over stdio (`documentSymbol`/`workspace/symbol` served from the Phase 2 graph) + scaffold client in `ide/vscode/` (`npm install && npm run compile`, `main.py --yes` terminal command, one-click dashboard). |

`main.py --yes/-y` auto-applies the generated patch for non-interactive use.

## Phase 6 - Agent Society (Orchestrator + Specialist Sub-agents)

The single loop became an **orchestrator commanding a society of specialists** speaking an
A2A protocol (`protocol/a2a.py`: `Task / Event / Artifact` Pydantic models, JSON round-trip,
finding schema `{severity, rule, file, line, description, suggested_fix}`).

`AgentLoop` was renamed **`core.orchestrator.Orchestrator`**; `from core.loop import AgentLoop`
still works (compat shim alias). After every applied patch set the orchestrator dispatches a
review Task to each enabled specialist **concurrently** (`asyncio.gather`), consumes their
progress/verdict events into the session log, and only accepts a change when tests are green
**and** the consensus policy passes.

| Specialist | Engines (degradation-first) | Artifact |
|---|---|---|
| `SecurityAgent` (`agents/security.py`) | semgrep (local ruleset `agents/rules/security.yaml`), bandit, builtin SQLi/eval/shell heuristics - slow semgrep is skipped, never stalls the loop | `security-report` |
| `ArchitectAgent` (`agents/architect.py`) | import-linter (when repo ships `.importlinter`), graph engine: declared layer stack direction + cycle detection + fan-in report (pydeps-style) | `architecture-report` |
| `QAAgent` (`agents/qa.py`) | pytest, pytest-cov gate (`coverage_min`), mutmut (opt-in), playwright e2e if installed | `qa-report` |

**Consensus when opinions conflict** (`config.yaml: agents.policy`): weighted vote
(security 0.5 > architect 0.3 > qa 0.2) vs `consensus_threshold`; `veto: true` lets a security
FAIL block regardless of the other two (an advisory qa fail is overridden by weights). Blocking
findings are injected into the next iteration's context as
`## EXPERT REVIEW FEEDBACK (BLOCKING ...)` until the review passes - the loop keeps fixing.

```yaml
agents:
  enabled: true                 # false -> classic single-agent loop
  sub_agents:
    security: {enabled: true, block_on: [CRITICAL, HIGH, MEDIUM], tools: [semgrep, bandit, builtin_sqli_scan]}
    architect: {enabled: true, layers: [[api], [service], [repo], [db]]}   # imports may point downward only
    qa: {enabled: true, coverage_min: 0, mutation: false}
  policy: {weights: {security: 0.5, architect: 0.3, qa: 0.2}, consensus_threshold: 0.5, veto: true}
```

**Society demo** (SQLi refactor-and-retry, end to end):

```bash
bash demo/setup_demo.sh phase6   # my_project: green suite + legacy vulnerable db/search.py + failing search test
python main.py "검색 기능 리팩토링해줘" --repo ../my_project
```

Typical trace: iteration 1 patches service/repo/db (tests go green) -> SecurityAgent returns
`SQLi 발견: f-string interpolation in db/search.py` -> consensus REJECTED -> feedback injected ->
iteration 2 rewrites to `?` parameter binding -> all three verdicts PASS ->
`CONSENSUS: ACCEPTED (weighted score 1.00)`.

## Phase 6.5 - Agent Registry (marketplace) & Negotiation

**Registry = packaging for whole sub-agents** (`registry/`): every persona is a versioned
manifest (`registry/agents/*.yaml`: semver, `entrypoint`, capabilities, dedicated tools,
validation bar, routing keywords, config_defaults). The Orchestrator only does
**Decompose -> Route -> Synthesize**: manifests decide *who* reviews each patch set
(always-on: security/architect/qa; keyword-routed: dba/performance), and the final verdict
is one synthesis record with persona versions pinned.

| Persona | Dedicated tools | Validation bar |
|---|---|---|
| 🏗 Architect 1.1.0 | import-linter + graph layer/cycle + `adg_generator` (mermaid ADG in artifact, `write_adg` opt-in) + C4/SOLID advice | layer-downward imports only, no cycles |
| 🔒 Security 1.1.0 | semgrep(local rules)/bandit/trivy + STRIDE `threat_modeling` skill | CVE 0, OWASP Top 10 |
| 🗄 DBA 1.0.0 | EXPLAIN QUERY PLAN (real sqlite, offline), partition advisor, dbt/GX hooks | P99 < 10ms (plan-based), quality checks |
| ⚡ Performance 1.0.0 | cProfile hotspots + folded flame-text, py-spy/locust hooks | bottleneck flagged, -30% goal |
| 🧪 QA 1.0.0 | pytest/cov/mutmut/playwright | green suite (+gates), mutation opt-in |

**Marketplace RPC**: `python -m registry.server --port 8331` - JSON-RPC 2.0 over HTTP with
NDJSON streaming (`registry.list / registry.resolve / agent.card / agent.invoke /
agent.stream`); a sub-agent is therefore usable out-of-process like any A2A peer.

**Negotiation instead of bare vetoes** (`protocol/negotiation.py`, `core/mediator.py`): on a
conflict the mediator collects typed positions, generates counter-offers (QA trades
"green suite, no interface churn" for the guard) and rules a **binding compromise** -
interim guard now + durable fix as real ticket files - which is appended to the next
iteration's feedback. Every consensus vote lands in `docs/decisions/DEC-*.md` (transcript) +
`.cortex_memory/decisions.jsonl` (audit stream); follow-ups in `.cortex_memory/tickets/`.

## Phase 7 - Closing the loop with the physical world

* `skills/hardware/twin.py` - `DigitalTwinClient(profile)`: `simulate(action) -> predicted_state`,
  `deploy(firmware) -> device_status` (dry_run default; journals sha256+size, never touches
  hardware), `telemetry_stream() -> AsyncIterator[SensorData]`. The drone twin is real 2nd-order
  physics + PID + gusts, seeded - numbers are reproducible, not decorative.
* `python -m skills.hardware.hil` - the spec's scenario end-to-end: "reduce propeller vibration
  by 20%" -> PID grid over 120 sims -> flash(sim) -> telemetry verify -> **commit** firmware or
  **roll back** (a re-run against an already-tuned baseline correctly rolls back).
* `research/` - `python -m research.report "<query>"`: arXiv crawler (live API; offline fixture
  fallback), hypothesis mining, experiment run + wandb-shaped dry tracker
  (`research/out/runs/*/metrics.jsonl`), and a compilable LaTeX draft (`research/out/techreport.tex`).

## Phase 8 - Recursive self-improvement (proposals & data, merges stay gated)

* `python -m selfdev.audit` - reads our own session logs; finds waste (repeat-send contexts ->
  prompt-caching headroom, dup-patch retries, over-heavy iterations) and writes `PROPOSE:` lines.
* `python -m selfdev.ga` - prompt evolution: gene population over prompt sections, diminishing-
  returns fitness with context-pressure cost (verified 0.946 -> 0.987 over 8 gens, junk preamble
  genes selected out). Winner promotes to `selfdev/out/promoted_prompt.md`, auto-injected when
  `selfdev.prompt_injection: true`. `--fitness real` is the documented plug-point for live LLM scoring.
* `core/llm/router.py` - `llm.models: {fast, coding, reasoning}` routing ("리팩토링해줘" -> coding,
  "design/security review" -> reasoning); no-op without config.
* `selfdev/failures.py` - every failed run mines a DPO preference pair into
  `.cortex_memory/feedback/dpo_pairs.jsonl`; a later success on the same task **closes** the pair
  (rejected attempt vs chosen patch set) - demonstrated in the budget-drill below.
* `python -m selfdev.evolve` - self-hosting scan of cortex itself (rust-pyo3 / sync->async /
  module-split candidates) -> checklist-only proposals in `docs/selfdev/`. Nothing auto-applies.

## Phase 9 - Economic agency (simulated money, real discipline)

* `econ/wallet.py` - simulated balance + ledger; **no keys, no chain, no payments** by design.
  The Orchestrator charges each run's est. cost (`dashboard.pricing`).
* **Budget as a hard constraint**: `econ.budget_usd` -> model downgrade at 60% pressure ->
  **BUDGET EXCEEDED** stop at 100% (verified: capped run failed at $0.00061/$0.00050, the mined
  DPO pair closed when the uncapped re-run succeeded).
* `python -m econ.bounty` - transparent confidence scoring (episodes + scope + labels - red flags);
  accepts only >= `auto_accept_threshold`, emits the exact branch->run->push->PR plan, escrow
  hold/release in a simulated ledger (`.cortex_memory/escrow.jsonl`).
* Maintainer mode (`github.maintainer.enabled`): webhook triage (bug/feature/question + reply
  drafts) and dependabot verify-then-merge **plans** - journal-only unless post gates are flipped.

## Phase 10 - Production hardening

* **Durable execution** (`core/runtime/durable_executor.py`): SQLite-WAL event store; every
  iteration is a `step` (STARTED→COMPLETED/FAILED with serializable in/out). `main.py --resume
  [TASK_ID]` replays completed steps, reports the crash point (incomplete STARTED rows) and -
  if an accepted step is already durable - finishes instantly instead of re-running anything.
* **Cost governor** (`core/runtime/cost_governor.py`): hard limits on tokens / USD / wall-clock
  / calls-per-minute. Checked BEFORE the request (projected refusal) and fail-fast after each
  call; per-task registry, model-aware pricing. `cost: {}` = off (back-compat).
* **Guardrails** (`security/guardrails.py`): injection patterns are masked (journal in
  `security.jsonl`) or the task is blocked (`mode: block`); every tool call passes a
  capability whitelist + sensitive-path deny-list (`.ssh/.aws/.git/config/.env/...`, patch
  targets included) + patch-size ceiling. A refused call becomes a failed ToolResult the LLM
  learns from - never a crash.

## Phase 11 - Multi-tenancy & GitOps

* `server/rbac.py` + `server/api.py` - JWT(HS256) `Principal{user,roles,tenant,allowed_repos}`;
  Role×Permission matrix + tenant isolation + repo allow-list, enforced on a stdlib multi-tenant
  API (`POST/GET /tenants/<tid>/tasks`, `/cost`), every decision audit-journalled.
  `python -m server.rbac mint ...` for tokens; tasks are journal-only unless `?run=true`.
* `vcs/gitops.py` - policy as code: `cortex/` branch prefix, Conventional-Commits/JIRA regex,
  protected branches, sign_commits (gpg-aware fallback), squash/rebase/merge strategy, dry_run.
  `python -m vcs.gitops --selftest` proves it against a throwaway bare origin.

## Phase 12 - Eval & prompt optimization CI

* `eval/benchmarker.py` - golden dataset (`eval/dataset.jsonl`) runs the REAL pipeline per case
  and asserts on durable artifacts (success, iterations, changed files, which personas
  reviewed, governor stop). Reports in `eval/results/`, compared to the previous run -
  regression = exit 1 for CI. All 3 golden cases PASS, incl. g-102 governor hard-stop.
* `optimization/prompt_optimizer.py` - DSPy-shaped `PromptCandidate{system_prompt,
  few_shot_examples, score}` with async mutate->parallel-eval->hill-climb; offline the
  benchmarker-features/GA surrogate scores (0.72 -> 0.93 in 3 iters), `--live` is the
  documented mutation-LLM plug point.

## Phase 13 - Air-gapped delivery

* `deployment/airgap_bundle.py` - one tarball: pip wheels per profile (`core|+agents|+eval`),
  `git archive` source snapshot, `install.sh` (venv + `pip install --no-index`), config
  template (API key stays placeholder - point base_url at the in-network gateway), SPDX SBOM
  generated from package metadata (no syft needed), SHA256SUMS. `--verify-into DIR` extracts
  into a fresh venv and imports offline - verified: 99 MB / 203 files / 17 pkgs SBOM /
  checksums VALID. `deployment/Dockerfile.offline` for the container route.

## Phase 14 - Observability & time machine

* `observability/tracing.py` - OpenTelemetry-native: one connected trace per run
  (`cortex.run -> llm.chat / tool.* / a2a.<persona>` spans with durations), JSONL file
  exporter always on, OTLP exporter auto-attaches when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
  No otel installed = no-op spans, never breaks a run.
* `observability/session_recorder.py` - per-iteration state snapshots (pickle→zstd→SQLite);
  replay what the agent knew at step N (`recorder.list_steps/load_snapshot`), the backend of
  a "jump to 3 hours ago" debugger.

## Phase 15 - Preflight simulation, structured reasoning, experience learning, resilient tools
- **Preflight** (`core/simulation/preflight.py`, config `preflight:`): candidate unified diffs are applied
  in memory and run through ast/ruff/import-resolution/minimal-diff checks BEFORE `git apply` - deterministic
  breakage costs zero LLM turns. `lint: warn|block|off`; context drift returns an explicit "re-read" instruction.
  Wired into both `edit_file` and `apply_patch_set` (one advisory gate per set, all-or-nothing).
- **Structured protocol** (`core/parsing/structured_parser.py`, `prompts/react_structured.md`, config
  `structured_output:`): when a model replies in text instead of function calls, the Thought/Plan/ToolCalls
  format is parsed and EXECUTED (recovered calls run through the same guardrails); non-conforming replies get a
  precise FORMAT ERROR retry prompt. `prompts/system.md` adds CoVe self-verification checklist, Hypothesis
  Ledger and a hardened MINIMAL DIFF rule (mirrored by the preflight changed-line heuristic).
- **Episodic few-shot** (`core/memory/episodic_store.py` facade over the Phase 3 store): runs are recorded with
  `success` + compact `trajectory`; similar PAST SUCCESSFUL trajectories are rendered into
  `## REFERENCE CASES` few-shot blocks injected at task start (config `context.episodic_fewshot`).
- **Recovery** (`core/tools/resilient_wrapper.py`, config `recovery:`): bounded retries for transient tool
  failures (flaky tests, timeouts) at the `_register` choke point - policy/deterministic failures (guardrails,
  preflight, rollbacks) are never retried and success is never fabricated.
- **Harness** `scripts/verify_agent.py --task ... --repo ...` -> JSON metrics (iterations, preflight_blocks,
  guardrail_blocks, structured_recoveries, episode_recorded) for CI. Suite: `pytest tests/` = 45 tests offline.

## Phase 16 - Secretary kit (browser, mailbox, competition intel, form automation)
- `core/tools/secretary/browser.py` — **BrowserTool** (`browser`): Playwright persistent-context session
  (profile at `~/.cortex_browser_profile`, logins/cookies survive). goto/click/fill/scroll/press/
  screenshot/get_dom/download/wait/eval/close; every action atomically refreshes
  `<workspace>/.cortex_browser/preview.png` + `state.json` (the IDE contract), downloads are
  filename-sanitized + extension-whitelisted, navigation has a randomized politeness delay,
  raw `eval` is off unless `secretary.browser.allow_eval`. Degrades to a clear ToolResult when
  playwright is missing - the agent loop never dies on it.
- `email_client.py` — **EmailTool** (`email`): IMAP/SMTP with **XOAUTH2 first** (env token or
  `~/.cortex/email_token.json` auto-refresh via stdlib), app-password only from `EMAIL_APP_PASSWORD`
  env; search/read/send/download_attachments, IMAP query sanitizer, workspace-confined attachments,
  credential redaction in every error path. Config ships placeholders (`you@example.com`) that are
  refused loudly rather than dialed out to.
- `competition_intel.py` — **CompetitionIntelTool** (`analyze_competition`): announcement URL ->
  structured JSON (title/host/**deadline (KST)**/eligibility/required_documents/submission/contact/
  schedule). Browser-render first with scroll passes, stdlib HTTP fallback, LLM forced-JSON with
  brace-salvage parsing and an honest heuristic fallback (`partial: true`) when no LLM is reachable.
  Cached per-URL under `data/competitions/<sha>.json`; `force_refresh` supported. Sub-calls are
  metered into the loop's cost governor.
- `form_filler.py` — **FormFillerTool** (`fill_form`): intel JSON + `user_profile` -> LLM field
  mapping (heuristic profile-matcher offline) -> fills only confidence >= 0.8, low-confidence
  fields are reported for manual review, NEVER guessed. **Dual submit lock**: no click unless
  BOTH `confirm_before_submit=false` AND config `secretary.form.allow_submit=true`. `mode=email_draft`
  returns a ready payload (`send_now:false`) for the email tool. Full mapping report in
  `data/form_filler/last_report.json`.
- All four register in the Orchestrator (AgentLoop shim) under `secretary.enabled` and are
  auto-allowlisted in guardrails; skills-style templates: `data/secretary/user_profile.template.json`.
- **VS Code** (`ide/vscode`): `CortexChatViewProvider` sidebar webview live-polls `.cortex_browser/`
  (fs.watch + interval) and renders the browser screenshot + url/title/action badge in real time;
  one-click task run + dashboard link. `npx tsc` green.
