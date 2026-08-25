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

## Next up

- [x] Kernel (Phases 1.1–1.8): events, task models, lifecycle, scheduler, runtime
      recovery, chaos, demo CLI — gate PASSED (hash equality at kill-rate 0.9)
- [x] Pipeline phase: DAG executor, harnesses, quality gates, durable HITL,
      store harness, semantic cache + token budgets
- [ ] Phase 5: LeadOps flagship — fixtures, INGEST→ENRICH→DEDUPE→SCORE→DRAFT→QA→
      REVIEW pipeline over the kernel, supervisor agent, chaos integration,
      integrity checker
- [ ] Phase 6: OTel tracing + Prometheus metrics + Grafana dashboards + bench suite
- [ ] Phase 7: packaging & release
