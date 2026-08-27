# Flow — Living Progress Log

> Everything done on swarmd, newest first. Updated in the same commit as any feature.
> Format: `[date] PHASE-N: what was done → why / notes`.

## Documentation protocol (binding — see PLAN.md §0)

Every feature commit must include:

1. **Progress entry** below (what → why → alternatives → trade-off accepted).
2. **Decision blocks** for non-obvious choices:
   ```
   DECISION: <choice>
   ALTERNATIVES: <B> · <C>
   WHY THIS: <reason that survives follow-ups>
   TRADE-OFF ACCEPTED: <what we consciously gave up>
   ```
3. **Anatomy blocks** for every new command/flag/config knob — what it does, why this
   value, what changing it causes (understanding-level, like explaining LLM
   `temperature`: "low = deterministic, right for extraction; high = creative; we use
   0.2 because verifiers need reproducible output" — not a man-page).
4. **Interview answers** for every question the feature invites, in `interview_prep.md`.
5. **Gate evidence** pasted under the matching heading.

Architectural decisions graduate to numbered ADRs (`docs/adr/`). Rule of thumb:
reversible → decision block here; one-way door → ADR.

---

## 2026-08-24 · Phase 0 — Inception

- **PHASE-0:** Pivoted from "1000-agent kernel" to **multi-agent orchestration runtime**
  (10–50 purposeful agents in per-stage pools). Rationale (ADR-001): the demonstrable
  skills are coordination, quality gates, HITL durability, and recovery — not headcount.
  Concurrency still real and benchmarked via parallel-vs-serial speedup.
- **PHASE-0:** Selected flagship application: **LeadOps** — sales/leads operations engine
  over open data (INGEST → ENRICH → DEDUPE → SCORE → DRAFT → QA → REVIEW QUEUE).
  Multiple outreach/draft agents run concurrently within stages. Kernel stays pure;
  LeadOps lives in examples/ as the reference embedder (ADR-002). Outreach never
  auto-sends — review queue is a durable pipeline state (ADR-003).
- **PHASE-0:** Wrote PRD v2.0 (goals G1–G6, FRs, acceptance criteria), SPEC v2.0
  (7 phases with gates), ARCHITECTURE, ADRs 1–5.
- **PHASE-0:** Decided stack: Python 3.12+, asyncio stdlib core, httpx, asyncpg,
  OTel, prometheus-client, uv. Offline-first with deterministic mock provider (ADR-004).

### Gate evidence

(none yet)

---

## 2026-08-25 · Phase 1.1 — Toolchain bring-up

- **PHASE-1.1:** Initialized git repo, generated `uv.lock`, synced venv with dev+otel
  extras (pytest, pytest-asyncio, ruff, mypy, OTel SDK). Python 3.12.11 via uv.

  DECISION: uv for dependency/env management
  ALTERNATIVES: pip + requirements.txt · poetry
  WHY THIS: single tool for lockfile + venv + build; lockfile is deterministic and
  committed, so CI and any future contributor get byte-identical installs; fastest
  resolution of the three options.
  TRADE-OFF ACCEPTED: uv is younger than pip/poetry — a risk if it breaks on some
  platform, mitigated by the committed lockfile pinning exact versions.

- **PHASE-1.1:** Added `.gitignore` (Python caches, venvs, env files). Installed the
  speckit agent skills (`.agents/skills/speckit-*`) as the structured spec→plan→tasks→
  implement workflow layer — these drive *how* features are specified and built, while
  SPEC.md/PLAN.md remain the source of truth for *what* gets built.

### Gate evidence

(none yet)

---

## 2026-08-25 · Kernel core + LLM provider layer

- **Kernel:** Event bus, Task/Checkpoint models, agent lifecycle state machine,
  priority scheduler, and the runtime worker pool with lease-based recovery —
  all committed individually with tests (36 passing).

  DECISION: lease/heartbeat requeue modeled on Kafka consumer-group leases +
  Temporal's event-sourced replay
  ALTERNATIVES: whole-task retries on failure · central dispatcher reassignment
  WHY THIS: checkpoint-at-step-boundary + lease expiry means completed work is
  never redone and dead agents are detected without any health-check chatter;
  replacement agents skip completed steps deterministically.
  TRADE-OFF ACCEPTED: a lease that's too short can double-execute an in-flight
  step; mitigated by requiring steps to be idempotent-by-construction (their
  outputs live in checkpoints keyed by step name).

  DECISION: workers respawn automatically when killed (pool size invariant)
  ALTERNATIVES: let pool shrink · require manual restart
  WHY THIS: chaos kills mid-run must not degrade capacity; the reaper loop
  replaces dead workers each tick, keeping `concurrency` alive.
  TRADE-OFF ACCEPTED: slight complexity in `_reap_loop`; worth it because
  recovery-without-replacement isn't recovery.

  DECISION: AgentHandle state resets per task (worker persists, agent instance
  is per-task)
  ALTERNATIVES: one handle per worker lifetime with DONE as terminal
  WHY THIS: terminal states make illegal transitions loud, but a long-lived
  worker must be able to start fresh work after finishing a task; separating
  "worker process" from "agent instance" keeps both properties.
  TRADE-OFF ACCEPTED: two concepts where one might do; documented in agent.py.

- **LLM providers:** Provider interface + deterministic MockProvider +
  OpenRouter adapter restricted to free models (`:free` suffix) with a
  health-sorted fallback chain, plus FallbackRouter across providers. httpx added.

  DECISION: OpenRouter free-model chain with mock as last resort
  ALTERNATIVES: single model · paid models with budget caps
  WHY THIS: free-tier models keep demo costs at zero; the chain gives real
  resilience (rate limits/outages on one free model fall through to the next);
  mock last-resort guarantees the pipeline never hard-fails in demos.
  TRADE-OFF ACCEPTED: free models have lower rate limits and quality variance;
  acceptable for a resume project where correctness of orchestration, not model
  IQ, is the product.

  ANATOMY: temperature (LLMRequest.temperature, default 0.7)
    Controls sampling randomness. Low (~0.2) = near-deterministic output, right
    for extraction/QA stages where verifiers need reproducible pass/fail; high
    (~0.9) = diverse output for draft ideation. The mock provider buckets it into
    its hash so different temperatures yield different deterministic outputs —
    mirroring how real models behave without costing anything.

  ANATOMY: max_tokens (LLMRequest.max_tokens, default 512)
    Hard cap on response length. Bounds cost and latency; too low truncates
    structured JSON outputs mid-field (which verifiers then catch), too high
    wastes tokens on rambling. 512 fits our stage outputs comfortably.

  ANATOMY: OPENROUTER_API_KEY (env var)
    Auth for the OpenRouter adapter. Absent key -> OpenRouterProvider refuses to
    construct (fail fast) but make_router("openrouter") degrades gracefully to
    mock so demos never crash.

### Gate evidence

```
tests/kernel: 26 passed · tests/router: 10 passed · ruff clean · mypy strict clean
Kill-and-resume proven: kill agent mid-step → lease expires → task requeued WITH
checkpoint → replacement skips completed steps → output identical to clean run.
```

---

## 2026-08-25 · Chaos harness + demo CLI — kernel gate PASSED

- **Chaos harness:** seeded `ChaosHook` (kill/latency injection) + `ChaosRunner`
  kill loop attached to the runtime. Seeded RNG means chaos is reproducible —
  same seed kills the same agents, so integrity comparisons are meaningful and
  CI never flakes on wall-clock luck.

- **Demo CLI:** `swarmd demo kernel --kill-rate F --tasks N --seed I` runs the
  pipeline clean vs under chaos, prints both output hashes, exits 0 iff equal.

  DECISION: integrity hash over task OUTPUTS, not task IDs
  ALTERNATIVES: hash including IDs · ordered hash of results list
  WHY THIS: task IDs are random UUIDs that legitimately differ between runs;
  the guarantee is about work done (which tasks finished with what output),
  not internal identifiers. Sorting makes it order-independent.
  TRADE-OFF ACCEPTED: two tasks with identical payloads are indistinguishable
  in the hash; acceptable because payload uniqueness is a caller concern.

  ANATOMY: --kill-rate F (default 0.3)
    Probability a live agent is killed per chaos tick. 0 = clean run; 0.3 hits
    every recovery path in seconds while progress stays visible; 0.9 proves
    recovery under extreme pressure (hundreds of kills) but slows throughput
    dramatically — workers die faster than they complete steps. Above ~0.95
    with this tick rate the run effectively livelocks.
  ANATOMY: --seed I (default 42)
    Chaos RNG seed. Same seed + same workload = identical kill sequence, which
    is what makes "chaos output == clean output" a deterministic assertion
    rather than a probabilistic one.

  BUG FOUND BY THE GATE (and fixed):
    1. Steps received their OWN previous output instead of the PREVIOUS STEP's
       output after a resume-skip — the skip path didn't track chain position.
       Fixed by carrying prev_output across skipped steps in _run_steps.
    2. Demo steps slept 10ms while the chaos tick fired every 50ms: the whole
       run finished before the first kill. Chaos was a no-op, not a test.
       Fixed by tuning step latency (150ms) vs tick (500ms) vs lease (2s).

### Gate evidence

```
swarmd demo kernel --kill-rate 0.3   -> clean dfe1f2286da7d426 == chaos dfe1f2286da7d426 (13 kills, 13 requeues)
swarmd demo kernel --kill-rate 0.7   -> MATCH (97 kills, 69 requeues)
swarmd demo kernel --kill-rate 0.9   -> MATCH (317 kills, 201 requeues)
tests: 42 passed · ruff clean · mypy strict clean
```

---

## 2026-08-25 · Pipeline, harnesses, gates, HITL, router completion

- **Stage DAG executor:** dependency levels execute in order; stages within a
  level run concurrently with bounded pools. Cycle/unknown-dep detection at
  definition time.

  DECISION: level-by-level execution vs fully-streaming DAG
  ALTERNATIVES: per-item streaming (stage N+1 starts as soon as any item arrives)
  WHY THIS: level barriers make stage completion well-defined without end-of-
  stream sentinels — the sentinel approach deadlocked with multi-worker pools
  (only one worker ever saw the _END marker; the rest waited forever). Level
  semantics also make quality-gate reporting per-stage trivial.
  TRADE-OFF ACCEPTED: downstream stages wait for full upstream batches; fine at
  our scale where stages are seconds, not hours.

  BUG FOUND BY TESTS: initial sentinel-based design hung on the very first test.
  Replaced with queue.join() + task cancellation per level.

- **Harnesses:** base contract (tools+prompt+loop policy), LLM structured output
  with one-round JSON repair, robots-aware allowlisted fetch with token-bucket
  rate limits, composable verifiers, deterministic draft envelope rendering,
  pluggable store (in-memory / lazy Postgres via asyncpg).

  DECISION: LLM structured output uses pydantic schema + repair round
  ALTERNATIVES: function-calling APIs · regex parsing · no validation
  WHY THIS: provider-agnostic (works over OpenRouter free models that lack
  native function calling); the repair round (re-ask with validation error)
  fixes most malformed JSON; loud failure after retry beats silent garbage.
  TRADE-OFF ACCEPTED: one extra LLM call on repairable failures.

- **Quality gates:** verifier -> bounded repair -> requeue -> dead-letter, with
  a failure taxonomy (schema/range/content/other) accumulated per run.

  DECISION: bounded repair loop (max_repairs) instead of unlimited fixing
  ALTERNATIVES: repair until pass · no repair, straight to dead-letter
  WHY THIS: unbounded repair is a livelock dressed up as diligence; zero repair
  wastes recoverable items. The bound converts bad input into a countable,
  visible outcome either way.
  TRADE-OFF ACCEPTED: some fixable-in-3-tries items dead-letter at 1; the
  taxonomy shows if that's happening at scale.

- **Durable HITL:** AWAITING_APPROVAL persisted via a store protocol; decisions
  immutable (double-decision raises); append-only audit trail; restart survival
  proven by test (new manager instance over same store sees pending state).

- **Router completion:** semantic cache (hash-trigram embedder default, cosine
  threshold 0.95, TTL+LRU) and TokenBudget with fail-loud breach semantics.

  ANATOMY: SemanticCache.threshold (default 0.95)
    Minimum cosine similarity to serve a cached response. Too low serves wrong
    answers (a near-miss hit is worse than a miss); too high degenerates to
    exact matching. 0.95 is the standard compromise for short prompts.
  ANATOMY: TokenBudget.budget_tokens
    Hard ceiling per scope. Breach raises BudgetExceeded -> callers produce a
    clean PARTIAL report, never silent truncation mid-item.

### Gate evidence

```
tests: 89 passed (kernel 26, router 19, pipeline 15+8+9, harnesses 12+) · ruff clean · mypy strict clean
```

---

## 2026-08-25 · LeadOps flagship + CoT tracing

- **LeadOps flagship** (`examples/leadops/`): INGEST → ENRICH → DEDUPE → SCORE →
  DRAFT → QA → REVIEW over committed messy fixtures (20 leads, 3 duplicate pairs).
  Runs fully offline via mock; `--provider openrouter` swaps in the free-model chain.

  DECISION: dedupe by canonical key (lowercase alnum) keeping richest record
  ALTERNATIVES: embedding similarity clustering · LLM-pairwise confirmation
  WHY THIS: fixtures' duplicates are case/spacing variants — a canonical key is
  deterministic, free, and explainable. Embedding clustering is Phase-5+ work if
  duplicates get fuzzier; the key approach documents the baseline honestly.
  TRADE-OFF ACCEPTED: genuinely different names for the same company slip through.

  DECISION: mock provider gained a JSON mode (schema-aware deterministic payloads)
  ALTERNATIVES: skip structured stages offline · hand-stub each stage
  WHY THIS: LLMHarness.structured asks for JSON schemas in prompts; the mock now
  synthesizes valid payloads from those schemas (ints within declared bounds via
  pydantic Field ge/le). Every stage runs offline with realistic shapes — no
  special-casing anywhere.
  TRADE-OFF ACCEPTED: mock responses are schema-shaped but semantically empty;
  fine for orchestration testing, not for judging model quality.

  BUGS FOUND BY TESTS: (1) enrich overwrote company names with mock output,
  destroying the dedupe key — identity fields must survive enrichment;
  (2) repair fns referenced a nonexistent 'lead' field; (3) lead keys were
  case-sensitive while fixture emails mix casing.

- **Supervisor agent** (ADR-005): samples gate dead-letters, proposes versioned
  prompt patches, applies with rollback. Patch history is an auditable artifact.

- **CoT tracing layer** (`observability/tracing.py`): provider-agnostic TraceSink
  protocol with InMemory (debug), JSONL (Langfuse-style ingest), and Composite
  (fan-out to OTel + Langfuse simultaneously) backends.

  DECISION: own span model + sink protocol instead of binding to OTel SDK
  ALTERNATIVES: OTel-only · Langfuse SDK directly
  WHY THIS: one instrumentation point feeds every backend; OTel export becomes
  just another sink (Phase 6), Langfuse-style JSONL another. Kernel stays
  dependency-free; tracing can never break a run (sink errors log & swallow).
  TRADE-OFF ACCEPTED: no OTel-native features (samplers, exporters) yet — the
  bridge sink comes in Phase 6.

  ANATOMY: --trace-jsonl PATH (swarmd leadops flag)
    Exports every span as JSONL: stage spans, llm spans (prompt_chars,
    system_hash, tokens_in/out, response_preview), approval spans, plus the CoT
    thought chain with global tick ordering. This is the file you point a
    Langfuse-style tool at; trace_id ties it to Jaeger when OTel lands.

  ANATOMY: record_thought(decision, reasoning=...) 
    CoT capture primitive. Stamps a monotonic tick so thoughts from parent and
    child spans interleave into one true chronological decision chain — sorting
    by span alone scrambles order because parents close after children.

  BUG FOUND BY TESTS: iter_thoughts sorted by span entry seq, but a parent's
  post-child thought ("queued_for_human") still landed out of order — fixed by
  stamping each thought at record time.

### Gate evidence

```
tests: 115 passed (kernel 26, router 19, pipeline 32, harnesses 12+, leadops 17, observability 8)
swarmd leadops --provider mock:
  leads_in=20 enriched=20 deduped=18 scored=18 drafted=18 qa_passed=18
  awaiting_review=18 dead_lettered=0 integrity_hash=ee682347087edafe
  review_queue_pending=18 (outreach never auto-sends)
--trace-jsonl: every LLM call exported with prompt/tokens/response_preview + CoT ticks
```

---

## 2026-08-27 - v3 pivot: generalist swarm, unknown tasks, measured learning

Flagship changed from LeadOps (sales pipeline) to a generalist swarm thrown at unknown
tasks. PRD and SPEC rewritten to v3.0, PLAN to v2.0, ADR-006 through ADR-010 added,
ADR-001 superseded, ADR-004 amended. Phases 1-4 code is untouched by the pivot, which is
the strongest available evidence for the kernel-purity claim in ADR-002.

DECISION: flagship is a generalist swarm on unscoped tasks, not a fixed-domain pipeline
ALTERNATIVES: keep LeadOps and finish phases 5-6 as specified - paper-reproduction
  pipeline (paper to code to verified claim numbers) - broader research desk
WHY THIS: LeadOps gates were shape-checks (does the JSON parse) dressed as quality
  gates. Nothing in it could fail in a way that mattered, so no number it produced was
  worth defending. The generalist swarm forces the three things that are actually hard
  and actually rare: deciding what "done" means when nobody scoped the task, generating
  the plan instead of drawing it, and proving improvement against an ablation.
TRADE-OFF ACCEPTED: a working, tested flagship stops being the headline. LeadOps stays
  in examples/ with its tests green as second-domain proof, and receives no new work.
FOLLOW-UPS:
  Q: Isn't this just AutoGPT with more steps?
  A: AutoGPT decides its own success at the end. Here the success criterion is authored
     by N independent agents, attacked by a red-team that tries to satisfy it with
     garbage, and frozen with a content hash before a single solve attempt runs
     (ADR-009). The direction of that dependency is the whole difference.
  Q: Why not the paper-reproduction flagship? It has objective ground truth.
  A: It has better ground truth and a narrower claim. It only ever tests one task shape,
     so "unknown task" stays untested. Paper reproduction survives as one domain inside
     the custom eval arm, where it does its job without being the whole story.
  Q: Doesn't throwing away a working flagship waste the last two weeks?
  A: The kernel was the work. examples/leadops is roughly 700 lines against a src/ tree
     that did not change by a line for this pivot.

DECISION: reverse the 10-50 agent stance; target 1000+, ration the LLM (ADR-008)
ALTERNATIVES: keep the 10-50 cap - scale by paying for throughput
WHY THIS: population size is load-bearing for population search, market selection, and
  N-proposal criterion synthesis. A market with twenty participants is a meeting.
TRADE-OFF ACCEPTED: runs take minutes to an hour rather than seconds, because pooled
  free-tier throughput caps at roughly 34 LLM calls per minute. This is why the live
  dashboard exists rather than being a nice-to-have.
FOLLOW-UPS:
  Q: Isn't "1000 agents, most idle" fake parallelism, exactly what ADR-001 warned about?
  A: ADR-001's concern was agent counts inflated by sleep loops doing nothing. The answer
     here is not to cap the count but to publish cost per solved task beside it, every
     time. An agent holding budget, lineage, and skill state is a live market
     participant whether or not it is mid-call; idle-but-alive is its normal condition.
  Q: What happens at saturation?
  A: Agents block on the scheduler's bounded queue - the Phase 1 backpressure path. The
     run degrades to fewer effective agents and reports that, rather than dropping work.

DECISION: hard 0.05 USD ceiling per full run, enforced at the harness boundary
ALTERNATIVES: no ceiling with cost reporting - per-stage soft budgets
WHY THIS: a self-imposed ceiling is what forces the rationing engineering to be real.
  Without it, cache hit rate and cross-provider routing are optimisations nobody has to
  finish. With it, a run's feasibility depends on them.
TRADE-OFF ACCEPTED: some runs will abort. The abort is clean and itemised, which is a
  better outcome than a silently truncated run producing numbers that look like results.
FOLLOW-UPS:
  Q: Is 0.05 not arbitrary?
  A: It is chosen, not derived: roughly 180 paid calls at GLM 5.3 Flash rates, which is
     enough overflow headroom for a run whose bulk rides free tiers, and small enough
     that it cannot be met by giving up and paying. Raising it is a config change, not
     a redesign.

RESEARCH: free-tier capacity, measured 2026-08-27
  Groq              14,400 req/day   6,000 TPM
  Cerebras          ~1M tokens/day   ~30,000 TPM
  Google AI Studio  1,500 req/day    Gemini 2.5 Flash, 1M context
  Mistral           ~1B tokens/month ~50,000 TPM, REQUIRES opting into data training
  OpenRouter :free  20 RPM, daily cap reported inconsistently (50 / 200 / 1000)
  Paid overflow     GLM 5.3 Flash, 0.075 USD/M in, 0.25 USD/M out, 1.31M context
  Pooled: ~86,000 TPM, ~34 LLM calls per minute. The OpenRouter daily-cap disagreement
  across sources is exactly why the router discovers limits from observed 429s instead
  of trusting a constant.

DECISION: mock provider confined to tests/ (ADR-006, amends ADR-004)
ALTERNATIVES: keep mock as the default offline demo path - drop the mock entirely
WHY THIS: a dashboard fed by mock output is pixel-identical to one fed by real output.
  That makes the mock a way to accidentally lie, not a convenience.
TRADE-OFF ACCEPTED: "offline-first" stops being a headline feature. It was protecting a
  claim v3 no longer makes.
FOLLOW-UPS:
  Q: Doesn't this make CI flaky and networked?
  A: Unit tests keep the mock and stay hermetic. Only the eval smoke run touches the
     network, as a separate job, whose failure reads as a provider outage rather than a
     code regression.
  Q: Why keep the mock at all?
  A: The chaos integrity gate needs a deterministic generator to prove output is
     byte-identical under random kills. That test lives in tests/, where a double is
     legitimate.

DECISION: append-only ledger is the only source of any reported number (ADR-007)
ALTERNATIVES: in-process counters with careful review - counters plus periodic audit
WHY THIS: agents are selected on reported success and paid on verified success. Anything
  an agent can write, selection pressure eventually teaches it to write dishonestly.
  Removing the capability is cheaper than policing it.
TRADE-OFF ACCEPTED: a write per event, and eval runs cost double because the control arm
  is not optional. Half the compute buys the only thing that makes the other half mean
  anything.
FOLLOW-UPS:
  Q: What stops the ledger itself from being wrong?
  A: It is append-only and written at the harness boundary, below agent code. Cache hits
     write zero-cost rows so "what did the cache save" is a query, not an estimate.
  Q: What if treatment and control intervals overlap?
  A: The report emits the string "no measured improvement". Deliberately that phrasing,
     so a non-result stays visible rather than softened.

ANATOMY: uv run swarm run "<task>" --agents 500 --kill-rate 0.2 --ceiling 0.05
  --agents 500     worker pool size. Why 500: enough that population selection and the
                   market have real variance, few enough that the ~34 calls/min pooled
                   ceiling still lets a run finish within the hour. At 50 the economy is
                   a meeting; at 5000 agents starve on tokens and never converge.
  --kill-rate 0.2  probability a running agent is killed per scheduling tick. Why 0.2:
                   hits every recovery path inside one run while leaving enough
                   survivors to prove partial progress is preserved.
  --ceiling 0.05   hard USD limit, checked at the harness boundary so no call path can
                   bypass it. Breach aborts cleanly with an itemised report.

ANATOMY: --allow-data-training
  Off by default. Enables the Mistral Experiment free tier, whose quota is granted in
  exchange for consenting to have submitted prompts used for training. Every other
  provider in the pool is used without it. It is an explicit flag rather than a config
  default because the cost of that tier is paid in data, not dollars, and that should
  be a decision someone makes on purpose.

---

## Next up

- [x] Kernel (Phases 1.1-1.8): events, task models, lifecycle, scheduler, runtime
      recovery, chaos, demo CLI - gate PASSED (hash equality at kill-rate 0.9)
- [x] Pipeline phase: DAG executor, harnesses, quality gates, HITL state machine,
      store harness, semantic cache + token budgets
- [x] LeadOps: retained as second-domain proof, frozen, tests green
- [x] v3 docs: PRD/SPEC/PLAN rewritten, ADR-006..010 added, ADR-001 superseded
- [ ] Phase 5 - production floor: Postgres-durable approvals across processes, cost
      ledger, hard ceiling at the harness boundary, multi-provider pool router with
      empirical limit discovery, WebSocket event sink, Prometheus metrics
- [ ] Track F0/F1 - Next.js shell, agent grid, live event log, cost panel
- [ ] Phase 6 - criterion synthesis, adversarial pass, content-addressed freeze
- [ ] Phase 7 - plan synthesis, generic worker pool, sandbox harness, 500-agent chaos run
- [ ] Phase 8 - red-team organ: five detectors, containment via the chaos kill path
- [ ] Phase 9 - skill library with human approval gate, economy, consolidation, curriculum
- [ ] Phase 10 - eval harness: public + custom arms, control vs treatment, CIs
- [ ] Phase 11 - hardening, README rewrite, full interview_prep, CI green
- [ ] Deferred - cloud deployment (out of scope until Phase 11 is green)
