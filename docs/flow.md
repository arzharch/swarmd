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

## 2026-08-28 - Phase 5 production floor + platform track

Ledger, provider pool, cluster-wide quota, Prometheus metrics, Grafana, Kubernetes,
SLOs, runbook, deployment plan. 207 tests green, ruff and mypy clean.

DECISION: total_cost() sums ledger rows; no counter exists anywhere (ADR-007)
ALTERNATIVES: in-process counter with careful review - counter plus periodic audit
WHY THIS: agents are selected on reported success and paid on verified success.
  Anything an agent can write, selection pressure eventually teaches it to write
  dishonestly. Removing the capability is cheaper than policing it.
TRADE-OFF ACCEPTED: a write per event, and eval runs cost double because the
  control arm is not optional.
FOLLOW-UPS:
  Q: How do you actually enforce "no counter"? That is a comment, not a control.
  A: A test appends a row directly to the ledger behind the accountant's back
     and asserts the reported total moves. A counter implementation fails it.
  Q: Why is an unknown model fatal rather than defaulting to zero?
  A: A model priced at an assumed zero silently disables the ceiling, which is
     the single control between a run and an unbounded bill. UnpricedModel
     raises. The failure mode of being too loud here is a startup error; the
     failure mode of being quiet is a bill.

DECISION: JSONL for the ledger, fsync per row
ALTERNATIVES: buffered writes - Postgres table - SQLite
WHY THIS: append-only is the file format's native mode, so immutability is a
  property of the medium rather than a rule someone has to remember. fsync per
  row is slow on purpose: a ledger that loses its last rows when an agent is
  killed is exactly the ledger that cannot be trusted about a chaos run.
TRADE-OFF ACCEPTED: cross-run queries need Postgres later; a single run's ledger
  is small enough that a file is the honest choice now.
FOLLOW-UPS:
  Q: What happens to the row being written when the process is killed?
  A: A torn final line. read_durable() skips it and verify() reports the
     memory/disk discrepancy rather than raising - after a hard kill the honest
     outcome is "the last row was torn", stated, not an exception that makes it
     look like a code failure. Tested directly.

DECISION: one OpenAI-compatible adapter for all five providers
ALTERNATIVES: five vendor SDKs - one adapter plus per-provider subclasses now
WHY THIS: Groq, Cerebras, Google AI Studio, Mistral and OpenRouter all expose
  OpenAI-compatible chat-completions. They differ in base URL, key, and model
  ids. Five SDKs would be five dependency trees to express the same POST.
TRADE-OFF ACCEPTED: when a provider breaks the contract it gets a subclass. That
  cost lands on the one provider that deviates, not on the four that do not.

DECISION: rate limits discovered from observed 429s, not configured (ADR-008)
ALTERNATIVES: hardcode published limits - config file per provider
WHY THIS: published free-tier limits disagree across sources - OpenRouter's
  daily cap is documented as 50, 200, and 1000 by different sources on the same
  day - and change without notice. A constant is wrong on arrival.
TRADE-OFF ACCEPTED: the first 429 of a run is a real cost we pay to learn.
FOLLOW-UPS:
  Q: Why does Retry-After win over your own backoff?
  A: It is the provider stating its own limit. Our exponential backoff is a
     guess for providers that say nothing.
  Q: Why does a 429 back off the whole provider rather than just that model?
  A: Quota is per account. Trying the next model only deepens the hole.

DECISION: quota is a cluster resource coordinated in Redis (ADR-011)
ALTERNATIVES: in-process buckets only - a dedicated quota service - one replica
WHY THIS: provider limits are per account, not per process. Three pods each
  correctly limiting to 45 RPM present 135 RPM to the account. Horizontal
  scaling silently breaks a limiter that was correct on one node, and the bug
  first appears during a rolling update - i.e. in production, under load.
TRADE-OFF ACCEPTED: Redis becomes a production dependency, but only in the
  multi-replica deployment. Single-node and CI keep zero new infrastructure.
FOLLOW-UPS:
  Q: What happens when Redis is down?
  A: Degrade to local buckets at 25% of the real rate. Fail-open produces
     exactly the stampede this prevents; fail-fully-closed turns a Redis blip
     into total outage of a system whose premise is graceful degradation.
  Q: Why a token bucket rather than a fixed window?
  A: A fixed window lets a caller spend the whole minute's allowance in its
     first second, which reads to the provider as a burst and earns a 429
     despite a legal average.
  Q: Why is the Redis logic one Lua script?
  A: Check-then-decrement across two round trips is a race, and with hundreds
     of agents contending it is not a rare one.
  Q: Whose clock refills the bucket?
  A: Redis's. Pod clocks drift, and a bucket refilled against a fast clock
     hands out permits that do not exist.

DECISION: metrics live on a private CollectorRegistry, not the client default
ALTERNATIVES: default global registry - no metrics module
WHY THIS: swarmd is a library as well as a service. A host application that
  also uses prometheus_client would collide on the global registry - a
  duplicate-timeseries error at import time, the worst place to discover an
  observability conflict. It also makes the module reloadable, which is what
  lets the no-prometheus fallback be tested rather than assumed.
TRADE-OFF ACCEPTED: process/platform/GC collectors must be registered
  explicitly, since dropping the default registry drops its freebies.
FOLLOW-UPS:
  Q: Prometheus or the ledger - which is the source of truth?
  A: Neither, for different things. Prometheus is for OPERATING the system and
     is allowed to lose a scrape or reset on restart. The ledger is for CLAIMS
     and does neither. If a dashboard and BENCHMARKS.md disagree, the ledger is
     right and the dashboard is stale. This is written at the top of the module.
  Q: How is the cardinality policy enforced?
  A: A test asserts no metric declares run_id, task_id, or agent_id as a label.
     One unbounded label on a busy counter kills a Prometheus instance and is
     not recoverable without dropping the series.
  Q: Why count 429s separately from errors?
  A: They mean different things - an error suggests the provider is broken, a
     429 suggests it works and we asked too fast. Conflating them makes the
     error alert fire during normal throttling, which trains people to ignore it.

DECISION: a run is a Kubernetes Job; one run stays in one pod
ALTERNATIVES: Deployment - distributed run across pods
WHY THIS: a run has a beginning, an end, and a result: that is a batch
  workload. A Deployment would restart a "finished" process forever and make
  "did this run succeed" a question answered by reading logs. Distributing a
  single run needs a distributed scheduler nobody has built, and PRD section 6
  lists it as a v1 non-goal.
TRADE-OFF ACCEPTED: horizontal scale means MORE CONCURRENT RUNS, not a faster
  single run. That is the honest claim and it is the one that gets made.
FOLLOW-UPS:
  Q: backoffLimit 0 - why no retries?
  A: Re-running burns provider quota, the scarcest resource in the system, and
     the ledger already holds everything needed to diagnose the failure.
     Recovery WITHIN a run is the checkpoint system's job; retrying a whole run
     is a human decision.
  Q: Spot instances for runs - isn't that reckless?
  A: Runs are checkpointed and interruption-tolerant by construction; that is
     the product claim. Refusing Spot would be an odd lack of confidence in it.
     A Spot interruption is just another chaos event.

DECISION: no CPU limit on the control plane
ALTERNATIVES: set a CPU limit like everything else
WHY THIS: CFS throttling on a latency-sensitive asyncio loop produces
  tail-latency spikes that look exactly like provider slowness - which sends
  you debugging the wrong system entirely. The request reserves a floor and the
  namespace ResourceQuota caps the top.
TRADE-OFF ACCEPTED: a runaway loop could consume node CPU. Bounded by the
  namespace quota rather than per-container.

DECISION: chaos stays ON in production
ALTERNATIVES: chaos in dev only
WHY THIS: turning it off would make production the one environment where the
  recovery guarantee is never tested. That is precisely backwards.
TRADE-OFF ACCEPTED: production runs are slower than they would be without kills.

RESEARCH: infrastructure vs inference cost
  Prod infrastructure lands at roughly $280/month (EKS control plane $73, nodes
  ~$60, Multi-AZ RDS ~$120, ALB ~$20, S3 ~$5). LLM spend is ~$0 - the workload
  rides free tiers and the per-run ceiling is $0.05. Infrastructure therefore
  costs about 5,600x more than inference. Recorded because it is the number
  that would justify revisiting Fargate if the goal ever shifts from
  demonstrating Kubernetes competence to minimising cost.

BUGS FOUND BY TESTS:
  (1) Windows monotonic clock ticks at ~15ms, so a 429 and the success right
      after it can report a zero delta. A strict `>` in the backoff-decay check
      silently disabled decay entirely. Fixed to `>=`.
  (2) Reloading the metrics module re-registered collectors on the global
      registry and raised duplicate-timeseries, which is also a real embedding
      bug for any host app using prometheus_client. Fixed by owning a registry.
  (3) cli.py had a pre-existing mypy failure on the trace-sink assignment,
      meaning `mypy src` in CI was already red before this batch.

ANATOMY: --ceiling 0.05 (swarmd, per run)
  Hard USD limit, checked at the harness boundary so no call path bypasses it.
  Checked BEFORE issuing using a conservative estimate and again after real
  usage is known - discovering a breach only after the tokens are spent means
  the ceiling was advisory. Breach raises CeilingExceeded carrying the itemised
  report; callers turn it into a cleanly aborted run, never a truncated one,
  because a truncated run still emits numbers that look like results. 2% is
  held in reserve so the abort path can afford to run.

ANATOMY: quota burst (default = rate/4)
  How many permits may be spent instantaneously. Why rate/4: enough that a
  handful of agents starting together do not serialise behind the refill, small
  enough that a burst cannot consume the window and trip a provider measuring
  more finely than per-minute. Setting burst == rate reproduces the fixed-window
  behaviour the bucket exists to avoid.

ANATOMY: quota safety_margin 0.9
  Fraction of the published rate we decline to use. Published limits are
  enforced with the provider's clock, not ours, so our 30th request in a minute
  can arrive inside their previous window. Spending 90% keeps margin for that
  skew. At 1.0 the 429 rate rises measurably for no extra throughput, since
  rejected calls have to be retried anyway.

ANATOMY: GROQ_API_KEYS (plural) vs GROQ_API_KEY
  Plural is comma-separated and yields one pool slot per credential, each with
  its own quota bucket - two accounts genuinely are two quotas. Credential ids
  are indices (groq#0, groq#1), never key prefixes, because the id reaches
  logs, metrics, and the dashboard and must never carry key material.

### Gate evidence

```
pytest: 207 passed (kernel 26, router 63, pipeline 32, harnesses 12,
                    leadops 17, observability 22, ledger 23, misc 12)
ruff check .        All checks passed
mypy src            Success: no issues found in 26 source files
kubectl kustomize deploy/k8s/overlays/dev   -> 23 resources
kubectl kustomize deploy/k8s/overlays/prod  -> 24 resources
  prod: 0 plain Secrets (placeholder replaced by ExternalSecret)
  prod: images pinned by digest, replicas 3
swarmd providers probe (no keys configured):
  pool unavailable: no usable providers. Skipped: groq (no GROQ_API_KEY), ...
```

Not yet done in Phase 5: Postgres-durable approvals across processes, ceiling
wired through the LLM harness call path, WebSocket sink, and the frontend.

---

## 2026-08-28 - Simulated provider, fenced by data taint

Development needs a provider before any credential exists. Rather than relaxing
ADR-006, the fence moved from configuration to data. 250 tests green.

DECISION: simulated responses allowed outside tests/, fenced by ledger taint (ADR-012)
ALTERNATIVES: block development until keys exist - allow the mock behind a config
  flag - keep the mock in tests/ and stub each caller by hand
WHY THIS: ADR-006's real concern was never "synthetic data exists", it was
  "synthetic data is indistinguishable downstream". It enforced that by keeping
  the mock out of certain code paths, which is a property of code organisation
  and therefore one refactor away from being false. Marking the DATA survives
  refactors, stale environment variables, copied .env files, and a ledger file
  reused from a development session by mistake.
TRADE-OFF ACCEPTED: every report now carries a `simulated` field that consumers
  must handle. That is the mechanism, not an inconvenience.
FOLLOW-UPS:
  Q: Isn't this just re-permitting what ADR-006 banned?
  A: The ban on REPORTING from synthetic data is not relaxed at all - it moved
     from convention to a raise. refuse_simulated() aborts eval, benchmarks, and
     any improvement claim. What is newly permitted is developing against it.
  Q: Why is a config flag not enough?
  A: Flags get set three shells ago and forgotten, and .env files get copied
     between machines. A flag fences the run; taint fences the artefact, which
     is what someone eventually reads.
  Q: What stops someone deleting the taint flag to make a report look real?
  A: Nothing structural - anyone with commit access can remove a control. The
     distinction that matters is accident versus decision: presenting synthetic
     data as real now requires a deliberate, visible code change rather than a
     forgotten environment variable.
  Q: Does the taint survive a restart?
  A: Yes, it is a column in the append-only JSONL ledger. That is precisely why
     it lives on the row rather than in a run-level config object.

DECISION: simulated mode is exclusive - it replaces the pool, never joins it
ALTERNATIVES: simulated as a last-resort fallback after real providers
WHY THIS: a pool mixing real and synthetic providers produces a run that is part
  measured and part invented, with no way to say which half a given figure came
  from. Being entirely one thing, with the ledger stating which, is the only
  readable outcome.
TRADE-OFF ACCEPTED: cannot develop against a partially-keyed pool. Given the
  alternative is ambiguous numbers, that is a feature.

DECISION: simulated provider has non-zero latency and an available failure rate
ALTERNATIVES: instant, always-successful responses
WHY THIS: a fake that answers instantly hides every concurrency and backpressure
  bug the scheduler exists to handle, and one that never fails leaves fallback
  chains, repair loops, and dead-lettering completely unexercised. That is how a
  system passes every local test and falls over on first contact with a real API.
TRADE-OFF ACCEPTED: 50ms per call. A 600-call demo profile still finishes in
  under a minute.

BUG FOUND: .gitignore's `.env.*` rule was swallowing .env.example - the single
env file intended to be committed was the one silently never committed. Fixed
with a negation, and it had been true since the repo was created.

ANATOMY: SWARMD_SIMULATED_PROVIDER
  Read from the environment rather than taken as a constructor argument, so
  enabling it is visible in `env`, in a pod spec, and in a diff, instead of
  buried in a call site. Never a fallback for a missing key: a run with no
  providers fails loudly. A CI test fails the build if it appears truthy in any
  deploy manifest.

### Gate evidence

```
pytest: 250 passed  (+43: simulated 28, deploy guards 15)
ruff check .   All checks passed
mypy src       Success: no issues found in 27 source files
SWARMD_SIMULATED_PROVIDER=true swarmd providers probe
  SIMULATED PROVIDER ACTIVE -- all responses are synthetic. Ledger rows will be
  marked simulated=true and eval will refuse to run against them.
  simulated  simulated  OK  0.062s  simulated-v1
  1/1 providers live
swarmd providers probe (flag off, no keys)
  pool unavailable: no usable providers. Skipped: groq (no GROQ_API_KEY), ...
```

Deploy guards now assert, as tests rather than comments: no manifest enables
simulation, no manifest carries a provider key, prod pins digests and replaces
the placeholder Secret, dev never enables paid providers, every workload runs
non-root with dropped capabilities and a read-only rootfs and resource requests,
egress blocks the cloud metadata endpoint, and every alert has a runbook section.

---

## 2026-08-28 - Full system: Phases 5-11 built without provider keys

Durable approvals, criterion synthesis, sandbox, plan synthesis, skills, economy,
red-team, generic worker, orchestrator, control plane, Next.js dashboard, eval
harness, consolidation, curriculum, Terraform, CI. 558 tests, ruff and mypy clean,
full loop runs end to end on the simulated provider with no key.

DECISION: SQLite as the default durable approval store, Postgres for deployment
ALTERNATIVES: Postgres only - keep in-memory and document the gap
WHY THIS: Phase 3's gate says kill the process at review, restart, approve via
  CLI. It never passed: the CLI built a fresh InMemoryApprovalStore per
  invocation, so a queued approval was invisible to the next command. A gate that
  needs docker running to demonstrate is a gate people stop running.
TRADE-OFF ACCEPTED: two backends to maintain. Both sit behind one protocol and
  the state machine is untouched.
FOLLOW-UPS:
  Q: Why did the original tests not catch this?
  A: They tested ApprovalManager, which was always correct. The defect was the
     WIRING. The regression test now spans a real subprocess, because nothing
     short of that would have caught it.
  Q: Why WAL mode?
  A: Without it `swarmd list` during a live run contends with the writer and
     fails with "database is locked", which reads as a pipeline bug rather than
     a journaling mode.

DECISION: criteria are a declarative check language, not model-written Python
ALTERNATIVES: exec model-emitted `def check(candidate)` - prose criteria judged
  by a model at the end
WHY THIS: four reasons in descending order of weight. A frozen criterion is a
  run output a human may audit months later, and a typed check list can be read
  where a page of generated Python must be comprehended. It is content-
  addressable, so the same criterion hashes the same regardless of phrasing.
  It cannot DO anything - executing model-written code to decide correctness, in
  a system whose agents are selected on passing that check, is an obvious
  exploit surface. And malformed proposals fail at parse time rather than as a
  NameError halfway through a run.
TRADE-OFF ACCEPTED: bounded expressiveness. A task needing a genuinely novel
  predicate cannot be graded until a check kind is added. Preferred over running
  arbitrary model output as the arbiter of truth.
FOLLOW-UPS:
  Q: What if the swarm authors a criterion nothing can satisfy?
  A: Dead-letter rate near 100% from run start, which has its own alert and
     runbook entry. The criterion cannot change mid-run by design; correction
     happens between runs, visible as a new hash.
  Q: Isn't a fixed attack list weaker than a model-generated adversary?
  A: A model asked for garbage produces creative garbage. The boring garbage -
     empty, constant, prompt-echo, zero-valued artifacts - is what actually
     slips through weak checks. The structural is_weak() check backstops it.

DECISION: consensus threshold uses ceil, not int
ALTERNATIVES: truncation (the original)
WHY THIS: int(3 * 0.5) == 1, so three proposers at 0.5 agreement required ONE
  vote - every check any single proposer named was "agreed" and the consensus
  mechanism agreed on nothing. Caught by a test asserting aligned proposers
  score higher than split ones.

DECISION: the sandbox is defence in depth, not a security boundary
ALTERNATIVES: claim isolation - use a container per execution
WHY THIS: separate process, process-tree timeout, stripped env allowlist,
  confined tempdir, POSIX rlimits, output truncation. Real isolation is the
  Kubernetes Job with seccomp and no network, and this is the in-process layer
  beneath it. setrlimit does not exist on Windows, so limits_enforced reports
  false there rather than implying protection that is absent.
TRADE-OFF ACCEPTED: a determined attacker with code execution is not stopped by
  this alone. Saying so is better than implying otherwise.
FOLLOW-UPS:
  Q: Why do artifacts travel through a file rather than stdout?
  A: Regexing numbers out of stdout would let any program that prints a number
     claim success. artifacts.json is the only channel from sandbox to criterion.
  Q: Why kill the process GROUP?
  A: A timeout that kills only the parent leaves grandchildren holding CPU, and
     the symptom appears later as unexplained slowness in an unrelated stage.

DECISION: skill retrieval is lexical with IDF, not embedding-based
ALTERNATIVES: embedding similarity - LLM-judged relevance
WHY THIS: an embedding call per retrieval spends the binding constraint on an
  operation that happens many times per run. Lexical scoring is free,
  deterministic (so chaos-integrity hashes stay comparable), and explainable to
  the human reviewing why a skill was suggested.
TRADE-OFF ACCEPTED: genuinely synonymous phrasings miss. Embeddings become worth
  it when a measured miss rate says so, not before.

DECISION: the economy pays only on the frozen criterion's verdict
ALTERNATIVES: pay on output - pay on self-reported success - no economy
WHY THIS: paying on output rewards verbosity; paying on self-report rewards
  lying, and selection pressure finds that faster than it finds competence.
TRADE-OFF ACCEPTED: agents can die before demonstrating anything if the
  allowance is mis-sized, which selects for luck. Hence starting_balance at
  roughly three calls and reward above cost.
FOLLOW-UPS:
  Q: Why does a parent pay for its offspring?
  A: Free reproduction lets one lucky agent flood the population at no cost,
     which is drift rather than selection.
  Q: Why rank on efficiency rather than successes?
  A: On a quota-bound system, credits per success decides what is worth
     spreading. An agent that succeeds often but expensively is usually worse.

DECISION: red-team detectors are pure code; containment reuses the chaos kill path
ALTERNATIVES: LLM judge per action - a separate quarantine mechanism
WHY THIS: an organ that consumed the rationed resource it protects would be
  self-defeating, and would degrade exactly when the system is busiest. Reusing
  the chaos kill path means containment inherits checkpoint recovery and requeue
  already proven by every chaos test, so a wrongly-contained agent keeps its
  completed work.
TRADE-OFF ACCEPTED: cheap detectors miss what a judge would catch. Ambiguous
  cases escalate, capped as a fraction of run budget.
FOLLOW-UPS:
  Q: What stops an agent learning to evade the detectors?
  A: Nothing structural, and I would rather say so. Thresholds are not exposed
     to agents, library poisoning is judged against the control arm rather than
     self-report, and a human gate stands between a proposal and the library.
  Q: Who watches the red-team?
  A: Its containments are audit records subject to the same human review queue,
     and its spend is a ledger row. Kill authority, not silent authority.

DECISION: a run is a Kubernetes Job, and the dashboard is an observer
ALTERNATIVES: Deployment - dashboard with backpressure into the run
WHY THIS: a run has a beginning, an end and a result. The event hub drops on
  overflow rather than blocking, because one viewer on bad wifi must not apply
  backpressure into the agent loop.
TRADE-OFF ACCEPTED: a saturated dashboard misses events. SLO-4 budgets 5% for
  exactly this, and drops are counted rather than hidden.

DECISION: eval refuses an improvement figure without a paired control
ALTERNATIVES: report the treatment curve with a caveat
WHY THIS: a curve with nothing to compare against is the single most common way
  self-improvement claims get made. Refusing to produce one is cheaper than
  arguing about it later.
TRADE-OFF ACCEPTED: eval costs double. Half the compute buys the only thing
  that makes the other half mean anything.
FOLLOW-UPS:
  Q: Why bootstrap rather than a t-interval?
  A: Success rate over five runs is a proportion and cost-per-solved is skewed
     by the occasional expensive failure. Neither is normal at n=5, and a
     t-interval understates spread in the direction that flatters the result.
  Q: Why pair on (task, seed)?
  A: Task difficulty varies far more than the skills effect, so an unpaired
     comparison buries the signal in task noise.
  Q: What if the intervals overlap?
  A: The report prints "no measured improvement", in those words. A non-result
     that reads as a soft positive is worse than no measurement - it is quotable.

DECISION: consolidation reverts any prompt change that lowers the control score
ALTERNATIVES: accept changes that improve the treatment arm - allow a small
  tolerance
WHY THIS: a change helping only the treatment arm has taught the system to score
  better on its own benchmark. Any tolerance above zero lets that accumulate one
  small step at a time.

BUGS FOUND BY TESTS AND BY RUNNING IT:
  (1) Consensus threshold truncated: int(3*0.5)==1 meant a single proposer's
      check counted as agreement, so the consensus mechanism agreed on nothing.
  (2) Loop detector false positive: empty payloads all hashed to one signature,
      so any agent doing bookkeeping actions was contained as a looper - which
      also MASKED real detections, because the first detector to fire wins.
  (3) FastAPI resolves annotations against the module namespace, so models and
      types defined inside the app factory silently became query parameters.
      Every POST returned 422 and every websocket handshake closed with 1008.
  (4) The provider pool held no account, so runs reported calls=0 while
      spending - a cost ceiling that would never have triggered.
  (5) The simulated provider returned schema-shaped noise, which criterion
      synthesis correctly refused. No run got past stage zero, so the "develop
      without keys" story did not actually work until the provider learned the
      three prompt shapes the swarm issues.
  (6) .gitignore's `.env.*` swallowed .env.example - the one env file meant to
      be committed was the one never committed, since the repo was created.
  (7) The CI workflow triggered on `main` while the default branch is `master`,
      so it had never run on a push. A pipeline that silently never fires is
      worse than none, because it looks green.
  (8) My own WorkLostUnderChaos alert was a false positive: the kernel demo
      shows 595 kills against 404 requeues with a MATCHING integrity hash,
      because an agent killed while idle has no claimed work to requeue. A gap
      threshold would have paged during entirely healthy chaos. Now fires only
      on requeues at exactly zero.
  (9) mypy on Windows reported eleven errors for correct POSIX-only code;
      pinned to platform = "linux", the deployment target.

ANATOMY: --profile smoke|demo|deep|eval
  Derived from docs/CAPACITY.md rather than chosen. The pooled free-tier ceiling
  is ~45 requests/minute, so a profile is a statement about how many model calls
  fit in a target wall clock: smoke ~60 calls/~2min for CI, demo ~600/12-18min
  for the watchable run, deep ~1800/~40min for enough curve points to mean
  anything, eval as one task inside a sweep.

ANATOMY: --no-skills (and use_skills=False)
  THE CONTROL ARM. Disables skill retrieval with everything else identical -
  same tasks, same seeds, same chaos schedule. Every improvement figure is
  measured against this, and swarmd eval refuses to emit one without it.

ANATOMY: min_agreement 0.5 (criterion synthesis)
  Fraction of valid proposers that must include a check for it to enter the
  merged criterion. A check only one of three proposers thought of is as likely
  a misreading as an insight. Requiring all of them collapses to the
  intersection, which drifts to the weakest common denominator - usually just
  output_nonempty, exactly what the attack stage exists to reject.

ANATOMY: target band 0.4-0.7 (curriculum)
  Above 0.7 everything passes and nothing distinguishes a good approach from a
  lucky one. Below 0.4 almost everything dead-letters, burning quota on failures
  that teach nothing. The band is where outcomes actually vary.

### Gate evidence

```
pytest: 558 passed, 1 skipped
ruff check .   All checks passed
mypy src       Success: no issues found in 43 source files

swarmd demo kernel --kill-rate 0.9 --tasks 40
  clean_hash = fc290c0ca81354c4
  chaos_hash = fc290c0ca81354c4
  kills=595 requeues=404 ticks=594
  INTEGRITY: MATCH

SWARMD_SIMULATED_PROVIDER=true swarmd swarm run "<task>" --profile smoke --chaos
  criterion=0eff8402816d7ce1 (2 checks, attempts=1)
  plan=ab7f6feca1038789 (4 nodes, width=2)
  nodes_passed=4/4 contained=0  integrity_hash=ba687b1e8e66c34a
  cost=$0.000000 of $0.05 ceiling  calls=8  [SIMULATED]
  redteam: contained=0 flagged=0 llm_calls=0

swarmd serve + POST /api/runs -> 31 events streamed, run completed, metrics
  exported: swarmd_llm_calls_total{provider="simulated"} 8.0

swarmd eval --arms custom --repeats 2 -> 20 runs, both arms, bootstrap CIs
  VERDICT: no measured improvement (intervals overlap)
  BENCHMARKS.md NOT written: 20 of 20 ledger rows came from the simulated
  provider

kubectl kustomize overlays/dev  -> 23 resources
kubectl kustomize overlays/prod -> 24 resources, 0 plain Secrets, digests pinned
frontend: npm run build -> compiled, 4 routes
```

Not done, and named rather than buried: no live-provider run at 500 agents, so
the capacity plan's 60% cache-hit assumption is untested; no learning curve,
which needs 50-200 tasks against real providers; egress policy is wider than it
should be. See docs/PRR.md for the full review including what blocks a real
production deploy.

---

## Next up

- [x] Kernel, pipeline, harnesses, gates, HITL state machine, router
- [x] LeadOps retained as second-domain proof, frozen, tests green
- [x] v3 docs: PRD/SPEC/PLAN, ADR-006..012
- [x] Phase 5: ledger, hard ceiling, provider pool, cluster quota, metrics,
      Postgres/SQLite durable approvals, WebSocket event hub
- [x] Phase 6: criterion synthesis with adversarial freeze
- [x] Phase 7: sandbox harness, plan synthesis, generic worker
- [x] Phase 8: red-team organ, five detectors, containment via the kill path
- [x] Phase 9: skill library with human gate, economy, consolidation, curriculum
- [x] Phase 10: eval harness, control arm mandatory, generated BENCHMARKS.md
- [x] Track F: Next.js dashboard on the live stream, no fixture path
- [x] Platform: Grafana, alerts, K8s manifests, Terraform, CI that fires
- [x] Docs: capacity plan, SLOs, runbook, deployment plan, PRR, README rewrite
- [ ] Live validation: 500-agent run against real providers; measure the actual
      cache hit rate against the capacity plan's 60% assumption
- [ ] The learning curve: 50-200 tasks with the control arm, then and only then
      generate BENCHMARKS.md and make an improvement claim
- [ ] Narrow the egress NetworkPolicy to provider CIDRs (widest control shipped)
- [ ] Wire structured JSON logging - the ConfigMap sets it, the handler does not
- [ ] Exercise rollback against a real cluster
- [ ] Debt: FallbackRouter still lists MockProvider as last-resort (ADR-006)
