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

## 2026-08-28 - Reframed from demo to product

DECISION: treat this as a product with a restricted audience, not a demo
ALTERNATIVES: keep demo framing and accept the gaps
WHY THIS: "it's a demo" is how a system ends up with no threat model, no
  retention policy, and an API that turns out to have been reachable for a
  month. The audience being small is a deployment fact, not a licence to skip
  the work. Concretely this added an access posture with compensating controls
  (ADR-013), SECURITY.md with a threat model and per-class retention, edge
  hardening, structured logs with redaction, and a readiness review that says no.
TRADE-OFF ACCEPTED: more surface to maintain for an audience of one.

DECISION: no user auth, one operator token (ADR-013)
ALTERNATIVES: OIDC with users and roles - leave it open and rely on the network
WHY THIS: there is one principal. Building accounts, sessions and roles is a
  login for a population of one, and each component is a thing to patch and get
  wrong. Leaving it open makes a single misconfigured Ingress annotation the
  entire failure. One shared credential matches the actual threat.
TRADE-OFF ACCEPTED: no revocation granularity - rotating logs out everything -
  and the audit trail records a supplied actor string rather than a verified
  identity. Both stated in SECURITY.md rather than implied away.
FOLLOW-UPS:
  Q: Isn't that a hole with a paragraph attached?
  A: The test is whether the controls match the threat. There is no
     unauthenticated path to start a run, read a stream, or record a decision;
     the service refuses to run exposed without a credential; and the network
     layer means the port is not reachable to begin with. What is absent is
     multi-user identity, and there are no multiple users.
  Q: What changes when a second person needs access?
  A: An authenticating proxy at the Ingress, app unchanged - it already treats
     the request as pre-authenticated. That is why the boundary is at the edge.
  Q: Why is /metrics open?
  A: The alternative gives every Prometheus scraper a token that can also start
     runs, which is a wider grant than scraping needs.

DECISION: the app refuses to START bound off-host without a token
ALTERNATIVES: warn and continue - check at first request
WHY THIS: a service that binds 0.0.0.0 with no credential and only complains
  when someone finds it has already failed. The entire value of the check is
  that it happens before exposure.

DECISION: rate limiting is a quota defence, not a DoS defence
WHY THIS: the scarce resource is ~45 provider requests a minute shared by the
  whole system. A loop in a script can burn a day's free tier before anyone
  looks, which is far more likely than a deliberate flood. Sliding window rather
  than fixed, because a fixed window permits twice the intended rate across its
  boundary.

DECISION: renamed the `demo` profile to `standard`
WHY THIS: the name invited the mindset. It is the ordinary run size, derived
  from the capacity plan, not a showcase.

ANATOMY: SWARMD_API_TOKEN
  Gates every mutating endpoint and the event stream. Optional on loopback so
  local work stays frictionless; required to bind any other interface. Compared
  in constant time, accepted as `Authorization: Bearer` or `X-Swarmd-Token`, and
  as a query parameter on the websocket because a browser cannot set headers on
  a handshake. Sourced from Secrets Manager in prod; envFrom is fixed at
  container start, so rotation needs a restart - documented in the runbook
  rather than discovered during an incident.

### Gate evidence

```
pytest: 599 passed, 1 skipped   (+41: hardening 34, deploy guards 7)
ruff check .   All checks passed
mypy src       Success: no issues found in 45 source files
frontend       tsc --noEmit clean

swarmd serve --host 0.0.0.0   (no token)
  refusing to start: refusing to bind 0.0.0.0 with no SWARMD_API_TOKEN set...

kustomize prod: whitelist-source-range present, no LoadBalancer or NodePort on
  the control plane, SWARMD_API_TOKEN sourced from ExternalSecret
```

---

## 2026-08-28 - Closing the gaps the audit found

The previous entry ended with an audit that produced a gap list. This one closes
all of it except the part that needs credentials, and the interesting content is
not the features -- it is that four of the six defects fixed here were invisible
to the tests that were supposed to catch them.

### The shape of the problem

Every one of these looked correct from the outside:

| What it looked like | What was happening |
|---|---|
| Chaos gate green, integrity hash matches | Killed work was REDONE, not resumed. Deterministic work redone hashes identically to work recovered |
| Red-team unit tests green, 37 of them | One detector could never fire in production. Its test constructed it with a threshold the real config never uses |
| Rogue gate would have reported five detectors working | Four were. One rogue was caught by a different detector, which reads as a pass to anything that asks only "was it stopped?" |
| Cache hit rate 62%, cost near zero, run completed | Every plan node received the same answer |

The pattern: **a check that measures the outcome rather than the mechanism
cannot tell success from a convincing imitation of it.** Each fix below
therefore changes what is asserted, not just what is implemented.

### Decision: batching and chaos recovery are the same operation

**Problem.** Two separate gaps. `SwarmRun` wrote no checkpoints, so a chaos kill
restarted the node. And CAPACITY.md counted an 8x saving from batched
generation that did not exist -- every agent made its own call, which is why the
pool was capped at 16.

**Decision.** One mechanism. `GenericWorker.execute` takes a `Checkpoint` -- the
kernel's type, with the kernel's skip-completed-steps semantics -- and
checkpoints at attempt-scoped steps (`generate:1`, `materialise:1`, `grade:1`).
Then batching is implemented by *pre-seeding* each agent's checkpoint with its
variant, marked `generate:1`. The resume path skips the step and charges
nothing.

Batching and recovery turn out to be the same question from two sides: work
someone else already did.

**Alternatives considered.**

*Run swarm nodes through the kernel `Runtime`.* The obvious unification, and it
imposes the kernel's task/step model on a DAG whose nodes are generated at
runtime and whose agents are a population rather than a sequence. Sharing the
contract without sharing the loop is a real duplication, and STATUS.md names it
as the next structural refactor rather than pretending it is free.

*A coalescing broker for batching* -- collect concurrent identical requests,
fire one call. Needs a batching window, a timeout, and a policy for when fewer
callers arrive than expected: three sources of flakiness, replaced by generating
before the pool spawns.

*Attempt-scoped steps vs node-scoped.* Node-scoped is simpler and wrong: a kill
during repair round two would resume at round one, discarding a completed
repair.

**Why the test counts calls.** `test_a_killed_node_resumes_rather_than_repeating_the_model_call`
asserts `worker_calls == 1` after a resume. Comparing output could never have
detected the original defect, because the output was identical either way.

**Follow-up questions this invites.**
- *What happens if the checkpoint outlives the criterion it was graded against?*
  Today nothing does: checkpoints live for one run. Durable checkpoints across
  runs would need the criterion hash in the checkpoint and a refusal on
  mismatch.
- *Does pre-seeding make the population homogeneous?* It bounds diversity at K.
  Below the pool size, agents share variants -- visible in the report rather
  than hidden by re-calling to fill the gap, which would spend the requests
  batching exists to save.
- *Why cap the pool at 32 when generation is now O(1) per node?* Repairs are
  still one call each, so the worst case is linear at `max_repairs` per agent.

### Decision: a seeded rogue must be caught by ITS OWN detector

**Problem.** SPEC Phase 8 named `--seed-rogues all` and it did not exist. The
detectors were tested against hand-built `Action` fixtures, which proves a
detector parses its input and says nothing about whether a real rogue ever
produces that input.

**Decision.** `swarm/rogues.py` injects real misbehaviour into a real run: real
credits through the economy, real code through the sandbox, reported through the
same `_observe` path an honest worker uses. Nothing tells the red-team which
agents are seeded.

The gate tracks four outcomes rather than a boolean:

- **caught** -- the detector for this pattern fired
- **misattributed** -- a different detector fired first. The agent was stopped,
  so a boolean would call this a pass, while the detector under test was never
  exercised
- **blocked_upstream** -- an earlier layer refused the misbehaviour first
- **escaped** -- nothing stopped it

**What implementing it found.** Two things, and this is the argument for
seeding over fixtures:

*`BudgetSiphon` was unreachable.* Threshold 7,500 credits; allowance 2,000 with
bankruptcy at zero. No agent without a verified success could reach it. The
detector's docstring said it compared against the population MEDIAN; the code
compared against a fixed constant. The seeded siphon went bankrupt uncaught.

*The gate would have passed with four detectors.* The siphon's first
implementation repeated one payload with an index, and digits are masked before
hashing, so the LOOP detector caught it. Both fixed: the siphon now varies its
pretexts, and the seeder asks the red-team which detector actually fired.

**Alternatives considered.**

*Random assignment of patterns to agents.* Makes "all five detected" a coin flip
that passes most of the time. A gate that usually passes teaches people to
re-run rather than read. Round-robin from the front of the run instead.

*Counting `blocked_upstream` as a detection.* Convenient and false. When the
frozen criterion rejects the degenerate output before `CriterionGaming` sees it,
that is ADR-009 working -- and the detector was not tested. Reported in its own
column.

*Failing the gate on `blocked_upstream`.* The opposite error: it would fail when
the criterion does its job, which trains people to weaken the criterion to make
the red-team gate go green.

**Follow-up questions this invites.**
- *Is the seeded siphon still realistic now that its payloads vary?* More so: an
  agent burning budget on genuinely varied work is the harder case.
- *Would a real rogue evade these five patterns?* Certainly. Five named
  behaviours are the ones observed, not a taxonomy of the possible. The organ's
  value is that it runs continuously and costs no quota, not that it is
  complete.

### Decision: the cache matches exactly, and two callers opt out entirely

**Problem.** `SemanticCache` and `charge_cache_hit` had both existed for phases
with no caller, so `cache_hits` was a structural zero while CAPACITY.md counted
a 2.5x saving from caching.

**Decision.** `CachedProvider` wraps the provider, so every call goes through it
rather than the one path someone remembered to instrument. One cache per
process, shared across runs.

**What wiring it found, in the order it appeared.**

*Different plan nodes shared answers.* Cosine similarity between three genuinely
different nodes: **0.97**, above the 0.95 threshold. Worker prompts share a long
template and differ in a step name, so similarity is dominated by boilerplate.
The symptom was a fast, cheap run whose nodes all produced the same artifact.
Raising the threshold does not fix it -- similarity rises with template length,
so a longer envelope pushes any two prompts above any threshold.
`CachedProvider` now refuses a cache that is not `exact_only`.

*The plan proposers would have collapsed.* They sent an identical prompt and
relied on sampling for variety. With a cache in front, three competing DAGs
became one drawn once and copied twice -- and the selection reported a clean
winner. Fixed twice over: the proposers now decompose under different
priorities, and both proposer paths set `cache: bypass`.

*An unpriced model silently disabled batching.* A $0 cache-hit row re-priced the
model, raised `UnpricedModel`, and the batch caught it as a provider failure. A
cache hit cannot move the ceiling, so refusing it protected nothing. `charge_call`
still raises, because there the money is real.

*Batching swallowed a ceiling breach.* `CeilingExceeded` fell into the generic
handler and the pool fell back to individual generation, spending MORE past the
limit that had just fired.

**Alternatives considered.**

*Keeping similarity with a higher threshold.* Trades a known failure for an
unknown one, at a threshold that has to be re-tuned every time a prompt template
changes length.

*Per-run caches.* Safe and pointless: within a run each node's prompt differs
and each repair prompt carries its own failures. The repetition is across runs.

*Caching in eval runs.* Refused in code, not documented as a caution. An eval
measures variance across repeats; serving repeat 2 from repeat 1 does not bias
the bootstrap interval, it collapses it toward zero width -- and a zero-width
interval reads as a strong result.

**Follow-up questions this invites.**
- *What is the hit rate on genuinely novel tasks?* Near zero, by construction.
  Exact keying means identical prompts hit, and unknown tasks do not repeat. The
  100% measured on a repeated run is the ceiling. This is now the weakest number
  in CAPACITY.md and it says so.
- *Then what carries the capacity plan?* Batching, which does not depend on
  repetition.
- *Why keep the similarity path at all?* It is correct for free-text prompts,
  which is what it was built for. The mistake was assuming this system sends
  those.

### Decision: the supervisor proposes, the consolidator decides

**Problem.** PRD section 7 lists a supervisor under the flagship. One existed
only in `examples/leadops`, where stages were known at startup -- the generalist
swarm's stage names come from a plan generated minutes earlier.

**Decision.** `swarm/supervisor.py` reads the criterion's own failure taxonomy
and, when failures cluster on one check kind, writes a constraint addressing
that kind into the worker system prompt. The consolidator gates it and holds the
version history; the supervisor never judges its own patch.

Every patch is a hypothesis: measured against the pass rate before it, reverted
when it did not help. Off by default, because a patched prompt is a confound and
an eval arm must know which prompt it ran.

**Alternatives considered.**

*Let the supervisor decide whether its patch worked.* The self-assessment
failure the whole criterion-first architecture exists to avoid.

*Patch per plan node.* Node names change every run, so per-node prompts have
nowhere to persist. The worker system prompt is what carries across runs.

*Patch on the first failure.* Encodes one task's idiosyncrasy into every future
run -- the same reasoning that makes distillation require two verified
successes.

*Have an LLM write the patch.* Better generalisation, and it spends the rationed
resource on a process that runs between runs where a lookup table of constraints
per check kind is adequate. Worth revisiting when the taxonomy outgrows the
table -- and the code says so by reporting `unaddressable` rather than emitting
an empty patch.

**Follow-up questions this invites.**
- *What if a patch helps one task type and hurts another?* The pass rate is
  aggregate, so it would be kept while hurting a subset. Per-kind measurement is
  the obvious next step and is not built.
- *Do prompts grow without bound?* No: an ineffective patch is reverted, so only
  patches that measurably helped persist.

### Decision: the agent count belongs to the operator

**Problem.** `--agents` existed and was unused; the profile decided, and the
profile encodes a wall-clock target rather than the right population for a task.

**Decision.** `agents` is a per-run parameter on the API, the dashboard and the
CLI. It overrides `ADVISORY_POOL`, because a cap the operator cannot override is
a lie about who is in control, and the cost ceiling is the real protection
either way. Exceeding the advisory emits a `pool_above_advisory` event naming
the reason, so an expensive run is a decision rather than a surprise.

**Follow-up questions this invites.**
- *Why is `HARD_POOL` 64 if the ceiling protects cost?* It does not bound cost.
  It bounds concurrent in-flight work per node, so one level cannot turn a
  rate-limit into a thundering herd.

### Gate evidence

```
ruff check .                      All checks passed
mypy src                          Success: no issues in 54 source files
pytest -q                         807 passed, 1 skipped
npx tsc --noEmit                  clean
swarmd swarm run --seed-rogues all --profile smoke
                                  rogue gate PASSED: 4 caught,
                                  1 blocked before the red-team
swarmd swarm run --agents 32 --profile smoke
                                  32 agents, 8 provider calls
POST /api/runs x2 (identical)     run 1: 8 calls, 4 cache entries
                                  run 2: 4 calls, 4 hits, 0 misses
```

---

## 2026-08-28 - Building the image, and looking at the screen

Two things had never been done: the container image had never been built, and
nobody had looked at the dashboard at full size. Both were assumed fine because
their code read fine. Neither was.

### The deployment artifact did not exist

`docker build -f deploy/Dockerfile` failed on the first try, and had always
failed. Three defects in sequence, each hidden behind the one before it:

**`uv sync` could not package the project.** pyproject declares
`readme = "README.md"`; the Dockerfile copied `src/` and `examples/` and not the
readme, so the build backend died on a missing file. The CI job that builds and
scans images existed and had never run, because the workflow watched `main` in
a repo whose branch is `master` -- fixed earlier this week, so the first real CI
run would have failed at this line.

**The image installed the wrong extras.** `metrics`, `otel`, `redis` -- not
`serve`, not `postgres`. The manifest runs `swarmd serve` against Postgres, so
the container started, printed "serve needs the 'serve' extra", and exited. The
extras are the deployment contract however optional their name sounds.

**The default command bound container-loopback.** `docker run -p 8000:8000`
connected to nothing: the server was up and listening where the published port
could not reach it. Kubernetes passes `--host` explicitly and never hit this;
anyone running the image directly did.

None of the three is subtle. All three were invisible without running a build.

### Then the deployment CrashLoopBackOffed

With a working image, `kubectl apply -k overlays/dev` on a real cluster (k3s,
k8s 1.31) produced a control plane that restarted forever. The reason was in its
own logs: the manifest launches it with `--host 0.0.0.0`, and the app refuses to
bind off-host with no `SWARMD_API_TOKEN` (ADR-013). The base Secret ships that
key as an empty placeholder.

The guard was right and the manifest was wrong. Prod was fine -- its
ExternalSecret pulls a real token -- so this was dev-only, which is exactly the
environment where someone new to the project would meet it first.

**Alternatives considered.** Setting `SWARMD_SIMULATED_PROVIDER=true` in the dev
overlay would have made the cluster usable immediately, and a guard test
forbids it in any manifest with a rationale I agree with: a deployed environment
serving synthetic responses looks alive and does nothing. The dev overlay
patches in an obviously-fake token instead, and a cluster with no provider keys
stays healthy-and-not-ready, which is the honest signal.

**What the exercise also produced**, and could not have been read from the
manifests:

- A fresh cluster rejects three resources: `ServiceMonitor`, `PodMonitor` and
  `PrometheusRule` need the Prometheus Operator. `apply` reports it *after*
  everything else applied, so the exit code is non-zero on a mostly-successful
  deploy. Now a documented prerequisite.
- `rollout undo` does not update `last-applied-configuration`, so the next
  `kubectl apply` re-applies the version you just rolled back from. kubectl
  warns, and the warning scrolls past in an incident. Now in the runbook.

Rollback itself is no longer a documented intention: v1 deployed and Ready, v2
rolled out, `undo` restored v1, each step gated on `rollout status`, with
`maxUnavailable: 0` holding capacity throughout. Auth was verified in the
deployed posture too -- 401 without the token, 202 with it -- and a full chaos
run with five seeded rogues completed inside the cluster.

Four guard tests now cover the image and the token, each verified by
reintroducing its defect and watching the test fail.

### The dashboard had never been looked at

Screenshots at 1600x1000, which is the first time anyone had seen it at size.

**Two thirds of the viewport was empty.** `.card { max-height: 460px }` with
`align-content: start` meant the panels stopped at 460px on a 900px screen --
while the agent table clipped mid-row *inside* its card. Scrollable content
that looks truncated, above a half-screen of background. Cards now stretch to
the row they are in, and the `tall` prop that opted individual cards out of the
old cap is gone rather than left as a no-op.

**The agent-count field read as disabled.** It is `type="number"`, and the
stylesheet's input rule listed `input[type="text"], select`. Adding the type to
the selector did not fix the render, and rather than keep chasing the cascade I
fixed the actual UX problem: it is now a labelled pill matching the chaos and
skills controls, with `auto` as the placeholder because empty means "let the
profile decide" -- a real choice, not an unset value. A bare box with a faint
placeholder is ambiguous even when it is styled correctly.

**A long decision name printed on top of its own reasoning.**
`grid-template-columns: 136px 1fr` plus a mono identifier with no break
opportunity: `rogue_blocked_by_criterion` overflowed its track and overlapped
the text beside it. `minmax(0, 136px)` and `overflow-wrap: anywhere`.

**The reasoning panel was empty on arrival**, wasting a third of the screen. It
now selects the first agent that has recorded thoughts -- not simply the first
agent, since an idle one would trade an empty panel for one that looks broken.

**Views were not linkable.** The view was React state only, so "look at the cost
panel" was a set of instructions rather than a URL, Back did nothing, and a
reload always landed on the live run. Now in the fragment, via `replaceState` so
Back does not walk through every panel someone glanced at.

### Gate evidence

```
docker build -f deploy/Dockerfile        BUILD_OK (first successful build)
docker run ... /healthz                  {"status":"ok"}
kubectl apply -k overlays/dev            applied; control plane Ready
kubectl rollout undo                     v2 -> v1, Ready, no downtime
POST /api/runs (no token)                401
POST /api/runs (token)                   202, run completed, rogue gate passed
kubeconform -strict (dev/prod)           23 and 24 resources, 0 invalid
promtool check rules                     SUCCESS: 10 rules found
ruff / mypy / pytest                     clean, clean, 803 passed
tsc --noEmit / next build                clean, built
```

---

## 2026-08-28 - Real keys, and what they said

Six provider keys arrived. The work was meant to be "add NVIDIA and track the
limits". Most of it turned out to be finding out that the provider table this
project had shipped with was fiction.

### Every model ID in the registry was wrong

Probed with the real keys before writing anything down:

| Configured | Reality |
|---|---|
| groq `llama-3.3-70b-versatile` | 404 -- the llama models are gone from Groq |
| cerebras (all four) | **402 Payment required** -- the free tier needs a card now |
| google `gemini-2.5-flash` | 404 "no longer available to new users" |
| openrouter `nvidia/nemotron-3-ultra-550b:free` | 400 "not a valid model ID" |
| openrouter `z-ai/glm-5.2:free` | 429 rate-limited upstream |

Two of six providers had a single working model between them. A provider table
assembled from documentation and never called is a list of plausible strings,
and this one had been carried, commented and tested for the life of the
project.

The same held for NVIDIA. Its `/v1/models` lists 83 entries; this account can
call **four**. The rest return `404 Not found for account`, so the catalogue is
not an entitlement list. The model IDs I had researched from documentation --
`nvidia/llama-3.1-nemotron-nano-8b-v1`, `meta/llama-3.2-3b-instruct` -- were
not among them.

Everything in the registry is now measured on the prompt shape workers actually
send, not on a one-token ping, which measures the network and nothing else.
Groq turned out to be roughly 4x faster than anything else available (384 tok/s,
0.81s to a complete structured answer) and is ordered first for that reason.

### Decision: three kinds of limit, not one

**Problem.** "Track the limits" sounds like one number per provider. It is not.
Cerebras replenishes continuously, Google resets at midnight Pacific, and
NVIDIA hands out ~1,000 credits that never refill and **expire after 30 days**.

**Decision.** `router/budget.py` models three kinds -- RATE, QUOTA, GRANT --
because what running out MEANS differs, and that is what an operator needs
before deciding whether to wait.

The grant is the one that punishes optimism. It costs $0, so the obvious move
is to spend it first; do that and it is gone in week one and the month has no
burst capacity left. So `TIER_RANK` gained `free_grant`, sorting *behind* the
replenishing free tiers despite both being free.

**Alternatives considered.**

*One number per provider.* Simple, and it makes a finite pool look like an
income. The capacity plan would have reported NVIDIA's 40 req/min as 57,600
requests a day, for a pool holding a thousand.

*Model the grant as a very large daily quota.* Same error, more arithmetic.

### Decision: windows, including a five-hour session

Six windows: minute, hour, **session (5h)**, day, week, month. The session
window is there because it is the unit work is planned in -- "can I run this
afternoon" is not answerable from a per-minute rate.

Usage is summed from an append-only journal rather than held in counters, the
same reasoning as ADR-007, and for a sharper reason here: a per-month counter
in a process is a process wearing a month's name. Keyed per credential, because
that is the unit providers meter.

Google's daily quota resets on a wall clock, so it is modelled that way,
daylight saving included. An hour of error at that boundary is an hour in which
the system believes it has a fresh quota and does not.

**A mistake caught by using it.** The first capacity plan reported 88,450
requests/day sustainable. 86,400 of that was Mistral's per-minute rate
multiplied out to 24 hours -- a number that assumes perfect saturation for a
full day and that nobody meant as a promise. It was 98% of the headline. The
plan now counts only published *daily* allowances (2,050/day) and reports
grants and rate extrapolations beside it, labelled.

**And a test-hygiene bug the feature created.** The journal is deliberately
durable and machine-wide, which makes it exactly the wrong thing to leave
pointed at its default path in a test suite. A test run wrote 36 requests into
the operator's live journal and `swarmd providers budget` reported them as
consumed quota. Now isolated in an autouse conftest fixture, with a test
pinning the environment override that makes the isolation possible.

### Then the first real run happened, and it solved nothing

The infrastructure worked immediately -- criterion synthesis, planning,
batching, chaos, red-team, ledger, 26 calls, 51.8s, $0.00. **0 of 16 nodes
passed.**

*The criterion had frozen with unsatisfiable checks.* Models emitted
`artifact_exists` with no `key` and `contains_all` with no `substrings`. Those
raise `CheckError` on every candidate, which `evaluate` converts into a failed
outcome -- so the criterion graded every attempt as a failure forever and the
report blamed the workers. `CheckError`'s own docstring said "raised at parse
time, never at grade time"; the implementation did the opposite.

Invisible against the simulated provider, whose proposals were hand-written
with complete parameters. Now rejected at parse time.

*Then the fix taught a new failure.* The schema hint had shown `"params": {}`,
which models copied. Replacing it with concrete examples made them copy those:
every criterion demanded a file called `claims.json` and a stdout marker
reading `VERIFIED`, whatever the task was. A concrete example is an instruction
to copy it. The examples are now angle-bracket placeholders with an explicit
instruction to derive values from the task.

*Still open.* Real models answer with fenced ```python blocks; the sandbox runs
them and puts results in artifacts, but `candidate.output` stays the fenced
source, so a `json_parses` check over the output fails by construction. That is
a design question -- should a criterion grade the artifact or the output? --
rather than a bug, and it is the next thing to resolve.

**Follow-up questions this invites.**
- *Was the simulated provider worth having, if it hid all of this?* Yes, and
  the lesson is narrower than "mocks lie": it hid failures in the SHAPE of
  model output, which is precisely what it was standing in for. It never hid a
  failure in the orchestration around it.
- *Should the criterion be relaxed until runs pass?* No. A criterion tuned
  until the system passes is a benchmark the system wrote for itself, which is
  the failure ADR-009 exists to prevent. The mismatch gets fixed on the
  worker/criterion contract, not by lowering the bar.

### Gate evidence

```
swarmd providers probe        4/4 providers live
swarmd providers budget       2,050 req/day plannable, 61,500/month
                              nvidia grant 997/1000, expires 30d
GET /api/providers/budget     same figures, read by a FRESH process from the
                              journal an earlier CLI run wrote -- cross-process
                              persistence, which is the whole point
swarm run (real providers)    completed, 26 calls, 51.8s, $0.00, 0/16 solved
ruff / mypy / pytest          clean, clean, 835 passed
tsc --noEmit / next build     clean, built
```

---

## 2026-08-28 - From 0% to 20%: five mechanisms rejecting correct work

The system executed flawlessly and solved nothing. Every fix below came from
reading what agents actually produced rather than from reasoning about the
design, and four of the five were mechanisms working exactly as written while
being wrong.

### The agents were right and the system said no

The clearest moment. An agent replied:

    {"accuracy": 94.3, "baseline": 82.1}

Correct, complete, extracted from the source text. It was rejected twice over:

**`artifact_exists` did not see it.** Artifacts came only from sandbox
execution, so an agent that answered directly had, as far as the criterion was
concerned, produced nothing. Answering `{"accuracy": 94.3}` and writing that
same object to `artifacts.json` are the same claim; only one of them counted.

**The red-team contained it as gaming.** `criterion_gaming` flagged any passing
submission under 20 tokens, and the correct answer is six. The detector only
ever sees submissions that ALREADY PASSED the frozen criterion -- given that,
brevity is evidence the task had a short answer, not that the answer was empty.
It now requires short AND repetitive.

That change moves a judgement rather than deleting it. Deciding whether a short
answer is substantive requires knowing the task; the criterion knows it, a
runtime detector does not. So a bare `"ok"` joined the adversarial attack set:
a criterion that a one-word answer satisfies must not freeze in the first
place.

### The worker was never shown the specification

The largest single cause. A worker saw the task, its step, retrieved skills and
its own past failures -- never the criterion it was graded against. So the
criterion demanded a numeric artifact called `accuracy`, the plan step said
"extract the first claim", and the worker produced correct data under keys
nothing was looking for. Three attempts, three failures, no way to converge.

Solving against a hidden spec makes success accidental.

**Does this weaken ADR-009?** No, and the distinction is worth stating because
it looks like a compromise. The criterion is authored, adversarially attacked
and content-addressed BEFORE any worker exists; a worker cannot alter it, and
something else does the grading. The guarantee is that **the target cannot
move**, not that the target is secret. A test suite you are allowed to read is
still a test suite.

It does raise gaming -- an agent that knows the checks can write to them. That
is what the pre-freeze attack and the `criterion_gaming` detector are for, and
both are catching real cases in live runs.

### Consensus of two is not consensus

`ceil(2 * 0.5) = 1`, so at the smoke profile's two proposers every check EITHER
proposer thought of entered the merged criterion. That is a union, not a
consensus: 13 checks where three proposers produce 3-5. The profile meant to be
the easy one was the hardest thing the system could grade against. Three
proposers everywhere now, and a test asserts no profile can degenerate this way
again.

### The library was poisoning itself

With nodes finally passing, distillation fired for the first time -- and stored
the longest successful OUTPUT as the skill's instruction:

    {"accuracy": 94.3, "baseline": 82.1}

offered to every future run as advice. The specific answer to one task,
presented as a general method, so a later run on different numbers would be
handed the wrong ones and told they worked. The system was reliably generating
exactly what its `library_poisoning` detector exists to reject.

A skill has to describe HOW. What generalises from repeated successes is the
SHAPE they share -- which keys, of which types -- and the step that produced
them. Distillation now records that, and the values that must not carry over
are the ones it drops. Skills now read like
`"Find the first numeric value using a regular expression"`.

### Result

| | before | after |
|---|---|---|
| single run | 0/16 nodes | **8/8 nodes**, repeatably |
| eval, task-level | 0% both arms | **20% both arms**, CI[0.00, 0.60] |
| skills distilled | 0 | 4, describing approaches |

Still "no measured improvement" between arms, and that is correct: the library
starts empty, so the treatment arm has nothing to retrieve. The apparatus
reports the non-result rather than the first-pass difference, which is what it
is for.

### Decision: profiles sized to the budget, agent count owned by the operator

`standard` promised 500 agents and ~600 calls against a measured budget of
~1,146 requests/day -- half a day of total capacity for one run, and a number
that appeared nowhere anyone could act on. Now 24 agents and ~90 calls, so a
dozen fit a day.

The operator can still ask for 1000, and this is the part that changed
character. `--agents 1000` used to silently give 192: `HARD_POOL` clamped each
node's pool to 64 and nothing said so, which made the control a suggestion. The
concern behind that clamp was real -- 1000 simultaneous provider connections is
a thundering herd -- but the fix for too much work AT ONCE is to bound
concurrency, not to quietly run fewer agents. The population is now exactly
what was asked for; `MAX_IN_FLIGHT` governs how fast it moves.

And `preflight` prices the run before it starts, against what is actually left
today:

```
preflight: this run needs ~1005 calls and 588 remain today;
           it will exhaust the budget and stop partway
```

**Alternatives considered.** *Refuse a run that does not fit.* Rejected: a run
that exhausts the budget and stops partway is sometimes exactly what someone
wants at the end of a day. What is not acceptable is finding out afterwards.

*Estimate against the concurrency bound.* Tried, and it made 500 and 1000
agents quote the same price -- the opposite of informative. The estimate counts
population, because every agent asked for runs and may cost a repair.

**A mistake caught by using it.** The first `remaining_today` counted Mistral's
per-minute rate extrapolated to 24 hours, so it reported 86,988 requests
remaining against a real plannable budget of ~1,146 -- and every run, at any
size, answered "fits". A preflight that always says yes is not a preflight. It
now excludes rate extrapolations for the same reason the capacity plan does.

### Gate evidence

```
swarm run (live)              8/8 nodes, $0.00, repeatable across tasks
swarmd eval (live)            20% both arms, CI[0.00,0.60], no measured
                              improvement -- produced across 6 resumed chunks
swarmd providers budget       1,146/day plannable, token-bound on groq
preflight at 1000 agents      ~1005 calls vs 588 remaining: does not fit
ruff / mypy / pytest          clean, clean, 866 passed
tsc --noEmit / next build     clean, built
```

---

## 2026-08-29 - The ablation was not an ablation

The request was to get self-learning working and have QA sign off. What
happened instead is the most useful negative result this project has produced.

### The A/B test compared a thing to itself

`swarmd eval` built each run as `SwarmRun(pool, use_skills=use_skills, ...)`
and never passed `skills=`. In `SwarmRun.__init__`:

    self.skills = skills if use_skills else None

With `skills` defaulting to `None`, that is `None` in BOTH arms. The treatment
and control arms were the same code path, differing only in a boolean nobody
read.

So every "no measured improvement" this project ever reported was a null
result generator. Not a measurement that came back flat -- an experiment that
could not come back any other way, at any sample size. It survived because the
verdict it produced was the one an honest system is supposed to produce, and
nobody asked whether it could have produced another.

Worth stating as a general lesson: **a null result from an experiment you have
not verified can distinguish its arms is not evidence of absence.** It is
evidence of nothing at all, and it looks exactly like integrity.

### With a real library, learning made things worse

Fixed the wiring, distilled 11 skills from a training session, ran the sweep:

| | treatment | control |
|---|---|---|
| solved | 0 / 5 | 2 / 5 |
| node pass rate | 56.7% | 65.6% |
| pass@1 | 0% | 40% |

The harness reported "no measured improvement, delta -0.400" -- declining to
call it a regression at n=5 because the intervals overlap. Correct, and the
first time that verdict has been about anything real.

**Diagnosis.** Distillation was writing `"For steps like 'extract_dates': ..."`
-- and plan node names are generated fresh every run. Retrieval was injecting
confident instructions about a step the reading plan does not have. A worker
told how to do `extract_dates` while executing `tokenize` is worse off than one
told nothing.

The retrieval threshold's own docstring predicted this exactly: "a wrong skill
actively misleads a worker, while no skill just leaves it to reason from the
task." The machinery was right; the data going into it was mine, from a fix
made hours earlier.

**Fix:** skills describe the kind of work and the shape it produced, with no
node name. Rebuilt library reads `"When a step calls for this: Return a list of
all date strings found in the paragraph. Produce a JSON object with these
fields: ..."`.

**Not re-measured.** The day's budget went to the two sweeps that did run:

```
groq         101,522 / 100,000 tokens   BLOCKED
openrouter        51 / 50 requests      BLOCKED
nvidia-nim         0 / 1,000 credits    GRANT EXHAUSTED
google           496 / 1,000 requests   429 under load
```

The finite grant reaching zero on day one is the behaviour the budget module
was written to make visible, and the reason it sorts behind replenishing tiers.

### Decision: metrics that describe a population

Added because task-level success hides what a swarm is doing.

**pass@k.** A population that solves a task on its third attempt has solved it;
reporting only per-attempt success describes a single agent and understates a
population by exactly the amount the population is for. Returns None rather
than pass@fewer when there are not enough attempts -- a metric that quietly
changes meaning is worse than a missing one.

**Node pass rate.** 7 of 8 nodes and 0 of 8 both report "not solved" at task
level. For a system whose unit of work is the node, that is the difference
between nearly working and not working, and it moves long before the task rate
does. It is what showed the treatment arm was worse in a way the task counts
alone (0 vs 2, n=5) could not have distinguished from noise.

### Gate evidence

```
ruff / mypy / pytest        clean, clean, 878 passed
eval (live, real ablation)  T 0/5 vs C 2/5, node 56.7% vs 65.6%, pass@1 0%/40%
budget                      all providers exhausted; grant at 0/1000
```

---

## 2026-08-30 - Not paying twice: idempotency, a run memo, a cacheable prompt

Three places the system paid for work it had already done. A double-clicked
submit bought a second population. A task asked twice bought a second criterion
and a second plan. And every worker call in a run re-sent the same task, the
same criterion and the same retrieved skills as fresh bytes, because they sat
after the first thing that differed.

None of these is a new capability. All three are the same admission: on a
plannable budget of ~2,200 requests/day, the cheapest call is the one not made.

That figure is `docs/CAPACITY.md` section 7, which section 1 declares
authoritative over its own supply table wherever the two disagree. Re-checked
by running `swarmd providers budget`, which printed `plannable 2,195
requests/day`; the daily total moves a little day to day because groq's share
is `token cap / observed tokens per call` rather than a request cap. The
~1,146 in the dated entries above -- and in comments in `router/budget.py`
(line 1191), `server/app.py` (312), `server/idempotency.py` (8) and
`swarm/run.py` (81), each checked by grep rather than remembered -- is the
pre-recount figure. It is left where it records what was known at the time, and
it is not what the budget is today.

### The request path, with the memo on it

```
POST /api/runs              (Idempotency-Key: optional)
POST /api/runs/{id}/resume  (same contract)
  |
  |-- key fails KEY_RE ....................... 400            no run
  |-- key seen, body identical ............... 200 + ORIGINAL run_id
  |                                            Idempotent-Replay: true
  |                                                            no run
  |-- key seen, body differs ................. 422 conflict    no run
  |                                            (never names the first run's id)
  |-- key reserved, still mid-construction ... 409 in flight   no run
  |-- key unseen, or NO KEY AT ALL ........... 202 accepted -> run starts
                              |
                              v
              A RESUME SKIPS BOTH TIERS. If state.criterion or
              state.results is already set, neither lookup runs:
              swapping a criterion in mid-flight would grade the two
              halves of one run against different targets.
                              |
                              v
              EXACT tier: memo.key_for(task) = sha256 of the task,
              stripped / whitespace-collapsed / casefolded
                              |
             +----------------+-----------------+
             |                                  |
           HIT, admitted           miss, OR refused AT ADMISSION
             |                     (`_memo_lookup` returns None for both, and
             |                      `run()` reads `if memo is None`, so an
             |                      ADMISSION refusal -- provenance run not
             |                      completed, entry stale, run document
             |                      disowns it -- DOES fall through here.
             |                      Only the refused DOCUMENT is kept out, by
             |                      `by_fingerprint(exclude_key=key_for(task))`
             |                      -- a DIFFERENT entry of the same shape can
             |                      and does serve the run on the next line.
             |                      Measured: refuse the pencils memo by
             |                      marking its provenance run `interrupted`,
             |                      with a valid pens memo of the same
             |                      fingerprint in the store, and the near
             |                      lookup serves the pens memo -- 0 synthesis
             |                      calls, `served_from` naming the pens run.
             |
             |                      A DEEP-REVALIDATION refusal does NOT get
             |                      here, and that is the boundary this
             |                      diagram used to blur. `run()` computes
             |                      `near_memo = self._near_memo_lookup(task)
             |                      if memo is None else None` BEFORE either
             |                      stage. Once the exact lookup has returned
             |                      an entry, `near_memo` is already None, so
             |                      when `_criterion_from_memo` then rejects
             |                      that entry -- unparseable, hash mismatch,
             |                      malformed checks, attack now passes
             |                      garbage -- there is no near attempt left
             |                      to make and the run goes straight to full
             |                      synthesis. Measured on the same fixture:
             |                      a pencils memo carrying a criterion that
             |                      fails re-attack, with a VALID pens memo of
             |                      the same fingerprint sitting in the store,
             |                      pays all 6 synthesis calls; only the
             |                      exact-tier refusal is emitted, and no
             |                      `criterion_memo_revalidated` follows.
             |                      Whether that is right is arguable -- the
             |                      pens memo would have served -- but it is
             |                      what the code does.)
             |                                  |
             |            NEAR tier: generalise.abstract_fingerprint(task)
             |            -- literal KINDS in order, method vocabulary,
             |            and the COUNT of subject nouns. Never a score.
             |            by_fingerprint(): a scan over entries(), sorted
             |            by updated_ts descending, excluding this task's
             |            own key.
             |                                  |
             |                     +------------+------------+
             |                     |                         |
             |                HIT, admitted                miss
             |                     |                         |
  criterion read off the   literal_map(stored task,   criterion synthesis
  memo, re-parsed,         this task) -> rebind ->    N proposers -> merge
  re-hashed, and           leaked_subject_terms       -> adversarial attack
  RE-ATTACKED against      guard -> malformed()       -> freeze
  THIS task's text under   -> RE-ATTACKED against     [N calls]
  today's attack set       THIS task's text
  [0 calls]                [0 calls]                          |
             |                     |                          |
  plan read off the memo,  plan rebound and           plan synthesis
  revalidated through      revalidated through        N proposers -> validate
  planner.validate()       planner.validate()         -> select
  and hash-checked         [0 calls]                  [N calls]
  [0 calls]                                                   |
             |                     |                          |
  criterion_memo_hit       criterion_memo_revalidated          |
  plan_memo_hit            plan_memo_revalidated               |
             |                     |                          |
             |          any rebind step fails ->              |
             |          _near_refuse (entry KEPT) ------------+
             |                     |                          |
             +---------------------+--------------------------+
                            |
                            v
        workers execute EVERY node against the real task,
        graded by the frozen criterion, watched by the red-team,
        paid by the economy, distilled at the end
                            |
                            v
        memo written when each stage freezes; SETTLED at the end
        (unusable until this run's own status is `completed`)
```

Both HIT branches are what "it should fire instantaneously the second time"
turned out to mean, and the precise version is smaller than the slogan. What a
hit removes is SYNTHESIS ONLY: `2 * profile.proposers` calls, which is 6 on
every shipped profile. What it does not remove is the run. Measured against
`CountingProvider` on `smoke`, a cold run of the two-node fixture in
`tests/swarm/test_memo.py` issues 8 provider calls (6 synthesis + 2 batched
generation) and the repeat issues 2; substituting a three-node plan gives 9
and 3. Batched generation issues one call per plan node, not one per agent, so
the pool size does not multiply it, and a repair round adds calls on top. So
the honest headline is "6 calls out of 8 or 9, plus whatever repairs cost" --
not a fixed fraction, because the denominator is the plan.

Nothing on either hit branch replays a candidate, an artifact or a verdict.
Every worker still runs against the real current task, every candidate is
still graded by the frozen criterion, and a memo-served run can still fail.

### Decision: the memo key is exact, and a paraphrase misses on purpose

**Problem.** Two runs of the same question pay for the same criterion twice.

**Decision.** `memo.normalise` is strip, collapse internal whitespace, casefold.
Nothing else. No embedding, no token overlap, no similarity floor.
`router/cache.py` already records what happened the last time this repo matched
machine-assembled text by cosine: three genuinely different plan nodes measured
0.97 similar against a 0.95 threshold, so one node's answer was served to
another and the run reported a high hit rate while being wrong. Similarity on
templated text rises with template length, not with sameness.

**Trade-off accepted.** `"summarise the paper"` and `"give me a summary of the
paper"` are two keys and pay twice. Six proposer calls is a cheap price for not
grading one task against another task's definition of done.

**And what the near tier did and did not change about that.** The near tier
added a SECOND key, not a threshold on the first. It is
`generalise.abstract_fingerprint` -- literal kinds in order, method vocabulary,
and the COUNT of subject nouns -- and it is still a discrete equality test, so
it cannot reopen the 0.97-above-0.95 door. It does not rescue the paraphrase
above: verified, `abstract_fingerprint("summarise the paper")` and
`abstract_fingerprint("give me a summary of the paper")` differ, because
`summarise` and `summary` are different action tokens. What it does rescue is
the same question about a different subject -- pens to pencils -- and even
there nothing is served on the fingerprint match alone.

**Follow-up questions this invites.**
- *So the memo can never be wrong?* It can be stale, which is a different
  failure and the one the admission gates are for: content hash on load (a
  mismatch is quarantined into `memos/quarantine/`, never deleted), criterion
  re-hashed to its recorded hash, criterion re-attacked against the NEW task
  text, plan revalidated and hash-checked, entry younger than 30 days,
  provenance run `completed` AND its run document, if still present, agreeing.
  Anything else falls through to a cold run, and deletion is not one rule for
  all of them. A criterion or plan that fails re-parsing, re-hashing or
  re-attack is dropped unconditionally, because none of those get better on a
  second read. A memo whose OWN `status` field still says `completed` but
  whose run-store document disagrees -- a control-plane shutdown marking it
  `interrupted` after the fact, say -- is deleted too, specifically because
  `MemoStore.remember()` refuses to overwrite an entry that still looks
  reusable to its own check, which would otherwise refuse this task's memo
  forever with nothing able to write a replacement. A memo that is merely
  stale, or whose own status already says something other than `completed`,
  is left alone: `remember()` already treats those as safe to overwrite the
  next time this task's criterion freezes.
- *Why does casefolding not bother you?* It means `"Summarise X"` and
  `"summarise x"` share a memo, which is the widest thing this "exact" key
  forgives. Stated with the right provenance: `docs/SPEC.md` has no memo
  section and says nothing about this key, so the fold is a decision made at
  implementation time, not a documented divergence I can point at a clause of.
  `normalise` does three things -- strip, collapse internal whitespace,
  casefold -- and `test_normalisation_forgives_only_layout` pins the case fold
  as a HIT. What makes it safe rather than merely convenient is that a memo
  carries no answer and the criterion is re-attacked against the new text on
  every hit. Reversible in one line if the controller disagrees.
- *What about an eval run?* `SwarmRun(profile="eval", memo=...)` raises, for the
  same reason the response cache is banned there: an eval measures variance
  across repeats, and serving repeat 2 from repeat 1 collapses the bootstrap
  interval toward zero width, which reads as a strong result.

### Decision: no header means a new run, always

**Problem.** `POST /api/runs` answers 202 and starts a background task, so a
dropped response teaches the client nothing about whether its request landed.

**Decision.** The ordinary HTTP contract, and specifically NO body-hash fallback
and no environment flag to enable one. Re-running an identical task on purpose
-- an A/B arm, a flake hunt, a chaos comparison -- is a normal thing to do here,
so an operator who forgot the header must never be handed yesterday's run id
instead of the run they asked for.

**Follow-up questions this invites.**
- *Two requests with the same key at the same instant?* A per-key asyncio lock
  closes the check-then-create window, and a construction that fails calls
  `release()`, so a retry is never stuck behind a phantom reservation. A
  request that finds the key already reserved and still inside
  `PENDING_STALE_S` (120s) gets 409, not a second run: another request is
  mid-construction with this key, and answering with a fresh run id would be
  exactly the duplication the key was sent to prevent. Past 120s the
  reservation is disbelieved, because only a process that died mid-construction
  leaves one, and holding a key hostage forever after a crash is worse than the
  small chance of a duplicate.
- *What is the key scoped to?* The store, and nothing narrower. `_entry_key`
  passes the header through after `KEY_RE`; there is no per-operator, per-tenant
  or per-endpoint namespace on the FILENAME. What is scoped is the BODY
  FINGERPRINT: `fingerprint()` hashes `{"endpoint": ..., "payload": ...}` with
  `sort_keys=True`, so the same key reused across `submit` and `resume` is a
  body conflict (422) rather than a replay, and two clients serialising the same
  request with different field order are correctly one request. The consequence
  worth saying out loud: two unrelated callers who pick the same key collide,
  which is what the 8-character floor in `KEY_RE` is there to make unlikely
  rather than impossible.
- *Does it survive a restart?* Yes -- one file per key under the RunStore root,
  written with RunStore's exact discipline (temp file in the same directory,
  fsync, `os.replace`). A retry arriving after a deploy is precisely the retry
  worth deduplicating, so an in-memory dict would have covered only the cases
  that did not matter.
- *Two pods?* Then it can still double-accept. Stated, not solved: the lock and
  the store are per-pod. `IdempotencyStore` is a deliberately narrow surface
  (lock/get/reserve/complete/release/prune) so a Redis or Postgres backend drops
  in behind it.

### Decision: the run-stable bytes go FIRST, in the system message

**Problem.** Every provider in this pool is OpenAI-compatible, and
Groq/OpenAI-style automatic prefix caching keys on a byte-identical LEADING
prefix. The old layout sent one user message ordered TASK, STEP, REQUIRED,
checks, skills, failures -- so the first divergence between two agents was STEP,
on the second line, and everything after it (the criterion block and the skills
block, by far the largest part of the prompt) was re-read cold on every call.
Only `WORKER_SYSTEM`, 640 characters, was ever shared.

**Decision.** Placement and ORDER, not content. Every block that was sent
before is still sent, and no block is added, dropped or reworded -- but two of
them move ahead of the others as well as into a different role, so this is not
the same string cut in a new place. Stated exactly, because the loose version
of this sentence is the one a reviewer catches.

```
hoisted  (default)                      legacy  (SWARMD_PREFIX_ORDER=legacy)

system                                  system
  [run-stable, once per run]              WORKER_SYSTEM
  WORKER_SYSTEM                         user
  TASK: <task>                            TASK: <task>
  YOUR OUTPUT IS GRADED AGAINST...        STEP: <node.name>
  [node-stable, once per plan node]       REQUIRED: <node.instruction>
  APPROACHES THAT WORKED BEFORE...        YOUR OUTPUT IS GRADED AGAINST...
user                                      APPROACHES THAT WORKED BEFORE...
  [volatile, every call]                  YOUR PREVIOUS ATTEMPT FAILED...
  STEP: <node.name>                       [batch: PRODUCE K SEPARATE...]
  REQUIRED: <node.instruction>
  YOUR PREVIOUS ATTEMPT FAILED...
  [batch: PRODUCE K SEPARATE...]
```

Read the two columns downward and the reorder is visible: legacy sends TASK,
then STEP and REQUIRED, then the GRADED block. Hoisted sends TASK and the
GRADED block first and STEP/REQUIRED after.

Re-measured on the `smoke` fixture in `tests/swarm/test_run.py` (task
`summarise the source records`, `PLAN`'s two nodes `gather` and `verify`, no
skills retrieved), by capturing `len(request.system)` and `len(request.prompt)`
on every worker call under each arm:

```
                                legacy                 hoisted
batched, node gather      640 sys + 525 user     871 sys + 294 user   = 1,165
batched, node verify      640 sys + 526 user     871 sys + 295 user   = 1,166
plain, node gather        640 sys + 273 user     871 sys +  42 user   =   913
plain, node verify        640 sys + 274 user     871 sys +  43 user   =   914
```

`verify` reads one character longer in the user turn on both arms -- its
instruction is `produce report.json` against `produce notes.json`. The system
message is 871 characters on the hoisted arm, not 872, and the legacy 640 is
`WORKER_SYSTEM` alone (`len(WORKER_SYSTEM) == 640`). The total is identical
across the arms on every row: the same blocks, differently ordered and
differently split.

The two `batched` rows are what a full smoke run actually sends -- generation
is batched, one call per plan node, so an ordinary run makes no plain worker
call at all. The two `plain` rows were produced by making the batch call
return nothing, which drops every agent onto `worker.execute`. No repair-round
row is printed: a repair user turn carries the specific failure strings of one
candidate, so its length is a property of whatever bad output was fed in rather
than of the fixture, and it is not a stable figure to quote.

What is NOT identical is the concatenation: the join between two blocks is a
blank line inside one message and a role boundary between two, so
`system + prompt` is not byte-equal across the arms even though every block in
it is.

`build_run_system` runs once per run in `SwarmRun._execute`; one `NodePrefix`
per node is memoised and handed to BOTH `worker.execute` and `_batch_generate`,
because retrieval alone does not guarantee identical bytes -- `retrieve` scores
by success rate and `record_use` moves success rates DURING a run, so agent 1
and agent 12 querying the same library with the same text can be offered a
different ordering. Freezing the prefix once per node is what makes the byte
comparison hold. Retrieval itself stays node-scoped, and the hoist changes only
which message carries the retrieved text -- the query is still
`task + node.instruction`. There is NO measurement in this repo of coarsening
the retrieval KEY to run scope, in either direction. The 0.567-against-0.656
pair is the skills-on versus skills-off eval recorded in `docs/PRD.md` (G4) and
in the dated entry above, whose cause was traced to skills anchored to plan node
names that are regenerated every run -- a different ablation, and it is not
evidence about retrieval key scope. `worker.py`'s `build_node_system` docstring
(`worker.py:185`) and `tests/swarm/test_prefix_cache.py:266` do attach the pair
to retrieval-key scope; that is the misattribution, and it is uncorrected
because this pass touches no code.

**Trade-off accepted.** Moving the criterion into the system role changes how a
model WEIGHTS it, and a cheaper run that grades worse is a regression however
good the cache numbers look.

**Follow-up questions this invites.**
- *What exactly is proven equal?* Three things, and it is worth naming the
  boundary rather than waving at "same bytes".
  PROVEN: against `ScriptedProvider` -- a stub that returns one fixed worker
  output regardless of what it is sent -- a legacy run and a hoisted run of the
  same task produce the same criterion hash, the same per-node
  `Candidate.output` list and the same integrity hash
  (`test_moving_prompt_bytes_between_roles_does_not_change_the_result`). Its
  guard against tautology is
  `test_the_two_prompt_layouts_are_genuinely_different_prompts`, which asserts
  every legacy worker prompt contains `TASK:` and no hoisted one does -- so the
  two arms really did send different bytes.
  NOT PROVEN, and measurably false for a content-sensitive stub: that a
  responder reads the two layouts as equivalent. `SimulatedProvider` seeds its
  synthetic text on `sha256(system|prompt|temperature)`, and hoisting moves
  bytes across that `|`, so the same logical request returns different text
  under the two arms -- verified directly by handing it `LLMRequest(prompt=S+"\n"+U,
  system="")` and `LLMRequest(prompt=U, system=S)` and comparing: different
  output, same `tokens_in`. What a REAL model does with the same information
  reordered is the parity gate below, not this test.
- *Then how do you know quality did not get worse?* We do not. The acceptance
  gate is NODE PASS RATE parity on the `eval` profile at fixed seeds -- never
  `cached_tokens`, which measures the mechanism rather than the outcome -- and
  it has NOT been run, because it needs live providers and a corpus. `hoisted`
  is the default on the scripted-provider equivalence above alone. The
  two-command procedure is written into `worker.py`'s module comment and
  `SWARMD_PREFIX_ORDER=legacy` is the rollback.
- *Is the saving measured or estimated?* Measured or absent. `cached_tokens` and
  `cached_tokens_reported` are two fields rather than one nullable count,
  because "this provider does not report cached tokens" and "this provider
  reports that nothing was cached" are different facts, and reading the first as
  the second turns a working cache into an apparent no-op, or the reverse.
  Nothing is ever inferred from prompt length.

### Three defects the hoist exposed, each of which made a number look better

1. `SimulatedProvider.complete_with` seeded its hash on `prompt` only. After the
   hoist the only per-run bytes left in the user turn come from the PLAN, so two
   simulated runs of different tasks graded against different criteria would
   have produced identical synthetic output -- and every offline integrity hash
   would have been blind to the task and the criterion. `request.system` is now
   part of the seed.
2. Both offline providers computed `tokens_in = len(request.prompt.split())`.
   Hoisting MOVED prompt bytes into `system`; it did not delete them. Counting
   one role would have shown the reorder cutting prompt tokens by roughly the
   size of the hoisted block while the real bill was unchanged -- a fabricated
   saving landing straight in CAPACITY.md's offline forecast. On the `smoke`
   fixture that is the user turn falling from 525 characters to 294 on a
   batched call and from 273 to 42 on a plain one (table above), against a
   `system + prompt` total that does not move at all. Both now count
   `system + prompt`.
3. `ProviderSpec.prefix_cache` was a label nothing read. It now rides on every
   `pool.probe()` row and prints in `swarmd providers`, because a run reporting
   `cached_tokens=0` has two opposite explanations: a zero from
   `google-aistudio` ("explicit") is expected, a zero from Groq ("auto") means
   the shared prefix is not being hit.

### Decision: a skill is promoted on distinct task SHAPES, not on successes

**Problem.** The old bar was two verified successes, which two agents passing
the same node of the same run satisfy. Those two draws share a task, a criterion
and a prompt: one observation counted twice. The library then offers advice
proven on exactly one question.

**Decision.** `generalise.py`, a pure module with no swarmd imports and no I/O.
`abstract()` replaces literals with typed placeholders (URL, PATH, DATE, MONEY,
PERCENT, QUOTED, NUMBER, TERM) in one ordered left-to-right pass;
`strip_source_terms()` removes the subject-matter nouns a step shares with its
own task, which shape-abstraction structurally cannot see;
`validate_instruction()` RAISES rather than repairs when an instruction still
shares a whole literal with its source task. Retrieval abstracts BOTH sides, so
a skill can no longer be retrieved because a later task happens to look like the
one it came from. A `Skill` now carries `evidence_tasks` -- a tuple of
`generalise.task_signature` values, never task text -- and reaches a human only
at `MIN_DISTINCT_TASKS` distinct signatures.

Name that precisely, because the code's own naming invites the wrong answer:
`run.py` computes `task_key = task_signature(task)` and passes it as
`evidence_task=`, `SkillLibrary.record_evidence` names the parameter
`task_fingerprint`, and `Skill`'s docstring calls the contents "abstract task
FINGERPRINTS" in the loose sense of "a hash, not the text". They are NOT
`generalise.abstract_fingerprint` values. Verified by running a `smoke` run
over `"compute the total cost of 3 pens at 1.25 dollars each"` with a real
`SkillLibrary` and reading the proposed skill back: `evidence_tasks ==
('bac37e579ae7c1e7',)`, which is `task_signature(task)`;
`abstract_fingerprint(task)` is `db1c9854b830f5a9` and appears nowhere on the
record.

**Follow-up questions this invites.**
- *What counts as a distinct shape?* `generalise.task_signature`: the task's
  SUBJECT MATTER (the HEAD NOUN of each noun phrase a determiner, preposition
  or literal introduced, stem-folded, minus method vocabulary; no literals, no
  grammar, order-independent) plus the sorted KINDS of literal it carries. It
  was first keyed on the abstracted SENTENCE, which inherits `memo.normalise`'s
  deliberate "a paraphrase MISSES" property -- correct for an exact-match cache,
  exactly wrong for a farming defence, where a miss MANUFACTURES the second
  piece of evidence. One request reworded with "please" and "for me" was pushing
  candidates to a human as if two tasks had agreed. Found by review, not by me.
- *Which way does the signature fail?* Toward collapsing. "count the pens" and
  "list the pens" share a signature and count as one shape; the cost is a
  promotion that does not happen. Splitting one task into two is the poisoning
  channel, so the error is chosen deliberately. It does NOT collapse a
  restatement that introduces genuinely new subject matter -- "...for the
  invoice" scores a second shape, because no lexicon can rule out a new content
  noun. That is the defensible boundary, not an airtight one.

  The residual sits on the SPLITTING side, and splitting is the dangerous
  direction here, which is worth spelling out because it is easy to state
  backwards. `MIN_DISTINCT_TASKS` counts DISTINCT signatures, so a
  rule that splits ONE task into TWO signatures hands a promotion its second
  piece of evidence for free. That is the farm. A rule that MERGES two
  genuinely different tasks into one signature does the opposite: it withholds
  evidence, and the cost is a promotion that does not happen. The deliberate
  design errs toward merging for exactly that reason.

  `_stem`'s bare-`s`-before-`es` plural tie-break folds the common `-se`
  subject class (`case`, `database`, `response`...) correctly at the cost of a
  few short bare-sibilant words (`bus`, `gas`) and unfixably ambiguous
  Latin/Greek loanwords (`lens`, `virus`, `atlas`...) whose singular already
  ends in a bare `s`. Those SPLIT: measured, `count the lens in the tray` and
  `count the lenses in the tray` are `1d5513adf70035da` and `d74a788bf7d23bee`
  -- two signatures for one question, where `count the pen`/`count the pens`
  correctly give one. So it can manufacture the second piece of evidence, and
  it is disclosed rather than fixed because telling a Latin bare-`s` singular
  from a genuine bare-`s` plural by suffix alone needs a lexicon this module
  refuses to keep. It does NOT reach the near tier: subjects enter
  `abstract_fingerprint` only as a COUNT, and one subject stays one subject
  however it is stemmed -- both spellings score `2ebf0629e20c2e86`.
- *Does the bar gate approval?* It gates the HUMAN QUEUE
  (`SkillGate.submit`) and, now, `SkillLibrary.approve()` itself: a candidate
  below it is still recorded, still unusable, still accruing evidence, and a
  caller that reaches `approve()` with evidence short of `MIN_DISTINCT_TASKS`
  is refused unless it passes `force=True`, which the skill's own record then
  carries as `approval_note` so the bypass is visible on the skill rather than
  only in whoever's log invoked it. `--auto-approve` still can approve a
  one-shape candidate -- explicitly, by passing `force=True`, which is the
  documented "bypasses review, but visibly" behaviour, not a gap in the check.
  The one place the check stays silent: a skill with NO recorded
  `evidence_tasks` at all -- hand-authored, never proposed through `run.py`'s
  distillation path -- skips it entirely, because a rule about distinct task
  shapes has nothing to say about a candidate that was never counted against
  it.

### Decision: retrieval checks shape agreement, not just word overlap

With the pen skill approved, `"Compute the average rainfall in millimetres for
12 cities"` and `"Compute the total distance of 5 marathons at 42.2 km each"`
both retrieved it -- on the strength of the word "compute" and the fact that a
number appears somewhere. Neither is a unit-price question, and a wrong skill
actively misleads a worker where no skill merely leaves it to think. When a
stored pattern names `MIN_SHAPE_SLOTS` (2) or more distinct literal kinds, the
incoming task must now supply ALL of them -- a SUBSET test, not a fraction. The
pen skill's pattern is `{NUMBER, MONEY}`; a task naming only one of those kinds
is refused outright rather than partially credited. The proper-noun kind
(`slot_term`) is excluded from the count on both sides, because counting it let
a capitalised word like "Boston" stand in for a missing literal kind. A
pattern's METHOD VOCABULARY has to agree too, unless the incoming task states
no method at all -- a bare noun phrase makes no method claim to contradict.
Inert below two slot kinds, so no existing retrieval test is touched.

**Where the boundary actually sits, probed rather than asserted.** With the pen
skill distilled and approved, its stored pattern is `Compute the total cost of
slot_number slot_term at slot_money each json_parses min_distinct_words`.
Running `library.retrieve` over a battery of tasks gives:

```
REFUSED  Compute the average rainfall in millimetres for 12 cities   (no MONEY)
REFUSED  Compute the total distance of 5 marathons at 42.2 km each   (no MONEY)
REFUSED  List 5 refunds over 20.00 dollars                (method disagrees)
REFUSED  Compute the total budget of 5 teams at 20.00 dollars each
                                    (states compute+total, but not "cost")
RETRIEVED Compute the total cost of 12 notebooks at 3.40 dollars each
RETRIEVED 7 pencils at 40c each                     (no method stated at all)
RETRIEVED 5 teams in the Boston office at 20.00 dollars
                                                    (no method stated at all)
```

The last row is the residual, and it is the same one `_shapes_agree`'s
docstring names: a task that states NO method vocabulary skips the method check
by design, because refusing it would delete `"7 pencils at 40c each"` -- the
headline transfer case -- along with it. So a task carrying every one of the
pattern's literal kinds and no verb can still retrieve a skill for a method it
never asked about. That is a disclosed design choice, not a measurement against
a corpus. The row above it is the cost paid for precision in the other
direction: `"the total budget"` is a genuinely related question and is refused,
because the pattern's method set is a SUBSET test and `cost` is missing from
the task.

ANATOMY: Idempotency-Key (request header, POST /api/runs, .../resume)
  8-200 characters of [A-Za-z0-9_.:-] -- the charset UUIDs, ULIDs, hashes and
  dotted job ids already use. The 8-character floor is not cosmetic: a client
  that "just picks something" collides across unrelated requests, and a
  collision here hands one caller another caller's run id. Absent means a new
  run every time, deliberately and unconditionally.

ANATOMY: IDEMPOTENCY_TTL_S (24 hours)
  How long a key is honoured. Long enough to cover every retry a client actually
  makes -- an HTTP retry budget is seconds, a CI re-run is minutes, an operator
  re-issuing a curl is hours -- and short enough that a key reused next week for
  a different question is treated as new. A record also ages out with the RUN it
  points at, whichever comes first; the sweep runs at startup beside
  `run_store.prune()`.

ANATOMY: MEMO_MAX_AGE_S (30 days)
  How long a frozen criterion may speak for a task. Not a correctness bound --
  the criterion is re-attacked on every hit, so an old memo is not a wrong one.
  It bounds STALENESS OF JUDGEMENT: check kinds get added and proposer prompts
  change, and a month-old definition of "done" deserves re-asking. Deliberately
  longer than the RunStore's 14-day sweep of finished runs, which is why the
  provenance status is recorded ON the memo rather than only looked up.

ANATOMY: SWARMD_PREFIX_ORDER=hoisted|legacy
  Which prompt layout to build. Read from the environment on every call rather
  than captured at import, for the same reason `use_skills` is a run flag: an
  ablation you cannot flip between two runs in one process is an ablation nobody
  measures. An unrecognised value is treated as `hoisted` and warned about,
  because silently honouring a typo as `legacy` would make a rollback look
  applied when it was not.

ANATOMY: MIN_DISTINCT_TASKS 2 (skill promotion)
  Distinct task SHAPES, not successes, before a candidate is offered to a human.
  Two is the smallest number that can distinguish "this worked" from "this works
  on more than the thing it came from". Raising it makes the library slower to
  grow and better evidenced; lowering it to 1 restores the farmable bar.
  Enforced in TWO places, not one: `SkillGate.submit` decides who reaches the
  reviewer, and `SkillLibrary.approve()` refuses a candidate whose recorded
  `evidence_tasks` is non-empty and short of the bar. `force=True` is the
  audited escape and writes `approval_note` onto the skill itself. Vacuous for
  a candidate with NO recorded evidence at all -- pinned by
  `test_a_candidate_with_no_tracked_evidence_is_not_gated_by_the_bar`, so it is
  a stated property rather than an oversight, and it is the hole a
  hand-authored skill walks through.

### Gate evidence

```
new tests, by the property they pin:
  test_idempotency     same key + same body -> 200, ORIGINAL run_id,
                       Idempotent-Replay: true, and NO SwarmRun constructed;
                       different body -> 422 that never leaks the first id;
                       malformed key -> 400; a record survives a fresh store
                       opened over the same directory
  test_memo            a second identical run skips BOTH synthesis stages and
                       books memo_hit rows carrying calls_avoided; a tampered
                       document is quarantined, not read; a provenance run that
                       did not complete is refused; eval + memo raises
  test_prefix_cache    every worker AND batch request carries a system message
                       that starts with WORKER_SYSTEM and is strictly longer
                       than it -- which no fallback can produce.
                       NEGATIVE CONTROL EXECUTED: under
                       SWARMD_PREFIX_ORDER=legacy this test FAILS with
                       "'worker' sent the bare base prompt: its node prefix was
                       dropped", so it is sensitive to the failure it names
  test_generalise      abstraction, the KEEP_AFTER exception ("round to 2
                       decimal places" survives), and one shared left-to-right
                       scan so abstract() and literals() cannot disagree about
                       what counts as a value
  test_skill_transfer  under task_signature -- subject head nouns plus the
                       sorted KINDS of literal, order-independent, most
                       plurals folded -- a restatement that adds politeness
                       words, swaps the digits, spells a number as a word,
                       changes case, or fronts the literals counts as ONE
                       shape. Verified by running task_signature over each:
                       "Please compute the total cost of 3 pens at 1.25
                       dollars each for me", "...9 pens at 4.75 dollars
                       each", "...three pens at 1.25 dollars each", the same
                       sentence uppercased or lowercased, and "AT 1.25
                       DOLLARS EACH, 3 PENS -- COMPUTE THE TOTAL COST" all
                       return bac37e579ae7c1e7, the base task's signature. The two measured near-misses
                       retrieve nothing; a real ApprovalManager wired into a
                       SwarmRun shows the durable queue empty below the bar.

                       TWO residuals live, disclosed not fixed -- (a) below,
                       and the narrower thing that replaced the one (b)
                       describes. The direction is easy to state backwards,
                       so state the rule first: the bar counts
                       DISTINCT signatures, so SPLITTING one task into two
                       signatures MANUFACTURES the second piece of evidence
                       (the farm), while MERGING two different tasks into one
                       signature only withholds evidence (a promotion that
                       does not happen).
                       (a) _stem's bare-s/-es tie-break. SPLITS, so it can
                           manufacture evidence. Measured: pens->pen and
                           cases->case fold, but bus->bus vs buses->buse,
                           gas/gases, lens->len vs lenses->lense,
                           virus->viru vs viruses->viruse, campus->campu.
                           So "count the lens in the tray" and "count the
                           lenses in the tray" score 1d5513adf70035da and
                           d74a788bf7d23bee -- one question, two signatures
                           -- where count the pen/pens correctly give one
                           (520803586b95d146 for both bare phrases,
                           recomputed just now). A Latin/Greek
                           singular already ending in a bare s is
                           indistinguishable from a genuine bare-s plural by
                           suffix alone, so fixing it needs a lexicon
                           generalise.py deliberately does not keep. This
                           residual is UNCHANGED by the fallback below, and
                           it is the one that manufactures evidence -- not a
                           harmless one. Invisible to the near tier, though:
                           both spellings keep one subject, so both
                           fingerprint 2ebf0629e20c2e86.
                       (b) the determiner-less FRONTED NOUN PHRASE residual is
                           CLOSED, and what replaced it is narrower. task_shape
                           now falls back to content words when nothing was
                           introduced, so "Pens: compute the total cost of 3
                           at 1.25 each" and "compute the total cost of 3 pens
                           at 1.25 each" both score 64f9e0ced633ac1f -- one
                           signature, where they used to be two -- and the
                           fronted PENCILS sentence scores c1596a4da1991a26,
                           the same as its ordinary phrasing, so the merge
                           across subjects is gone too.
                           The narrower residual that replaced it, pinned by
                           test_the_fallback_leaves_a_narrower_residual_and_
                           this_is_it: the fallback keeps every content word
                           and cannot tell an unrecognised verb from a noun,
                           so in a sentence with NO determiner anywhere, a
                           verb outside METHOD_LEXICON is read as subject
                           matter. Measured: task_signature("tally widgets")
                           is 343c262fa82dfce7 -- subjects ('tally','widget')
                           -- against 04d865fa68cef42b for "count widgets".
                           It needs BOTH conditions, where the old one fired
                           on any fronted phrase, and a determiner avoids it
                           entirely: "tally the widgets" and "count the
                           widgets" both score 04d865fa68cef42b. The honest
                           fix when such a verb turns up is to widen
                           METHOD_LEXICON, not the fallback, which is what
                           that test's docstring says failing means.
                       (c) NOT a residual any more: a spelled-out cardinal.
                           "three pens" used to mint a second signature
                           against "3 pens" -- one task written twice,
                           clearing the bar with no second task solved.
                           _digits_for_shape folds zero..twenty and
                           thirty..ninety to digits INSIDE task_shape, so
                           both now score bac37e579ae7c1e7. Folded there and
                           NOT inside abstract() generally, because
                           abstract() also renders distilled skill
                           instructions where a small number is method
                           guidance: "write one paragraph" must keep its
                           wording. Verified in both directions --
                           abstract("write one paragraph") is unchanged
                           while abstract("write 1 paragraph") gives "write
                           <NUMBER> paragraph", and re-running the suite with
                           abstract() wrapped to fold cardinals too fails
                           exactly one test out of 1,241,
                           test_run.py::test_distillation_without_artifacts_
                           describes_the_step_only, on 'When a step calls for
                           this: write <NUMBER> paragraph'.
                       And the boundary that is NOT a residual but a chosen
                       limit: a restatement introducing genuinely new subject
                       matter is a second shape. "...for the invoice" scores
                       64f93b39b25f93e2, because no lexicon can rule out a
                       new content noun.

NOT RUN, named rather than buried:
  the eval-profile NODE PASS RATE parity gate (hoisted vs legacy, fixed seeds).
    It needs live providers and a corpus. `hoisted` is the default on the
    byte-equivalence argument alone; `legacy` is the rollback.
  a live re-measurement of learning with the shape bar in place: still blocked
    on the daily quota, exactly as the previous entry left it.
```

Not done, and named -- with one item promoted off this list since it was first
drafted. The memo's near-match tier is now shipped: `MemoStore.by_fingerprint`
indexes on `generalise.abstract_fingerprint`, a key out of the same module the
skill gate draws on rather than a private coarse key of `memo.py`'s own --
which is the one thing that module was not allowed to grow.

It is NOT the skill gate's key. The skill gate keys evidence on
`generalise.task_signature`. The two hash
different things: `task_signature` takes the SORTED SET of literal kinds plus
the SORTED SET of subject nouns; `abstract_fingerprint` takes the ORDERED
sequence of literal kinds, the sorted set of method vocabulary, and a COUNT of
distinct subjects with the subject words themselves discarded. So pens and
pencils are TWO signatures and ONE fingerprint -- measured,
`compute the total cost of 3 pens at 1.25 each` and the pencils sentence give
`64f9e0ced633ac1f` / `c1596a4da1991a26` against a shared `db3cea5800e7b6d9`.
That difference is the whole reason the near tier fires exactly where the
evidence bar does not: two subjects are two independent pieces of evidence, and
one shape of work is one memo, rebindable from either onto the other.
`_near_memo_lookup`,
`_criterion_from_near_memo` and `_plan_from_near_memo` in `run.py` rebind a
same-shape entry's literals onto the new task and RE-ATTACK the rebound
criterion before trusting it, exactly as the exact tier does.
`leaked_subject_terms` closes the gap `rebind` alone leaves open: a source
task's subject noun surviving inside a check PARAMETER, which no adversarial
attack notices because it only tries degenerate candidates. Stated precisely,
because the unconditional version of this sentence is wrong: the guard first
computes the source's subject stems minus the target's, and if that set is
empty it reports NOTHING -- a criterion rebound from "3 pens" onto "1 pen"
keeps the word "pens" and is correctly allowed through, because pens and pen
are the same subject. Only when the source has a subject the target lacks does
it look, and it looks by STEM, so a pens-derived criterion rebound onto pencils
is caught whether the surviving parameter says "pens" or "pen". Verified by
calling `leaked_subject_terms` directly on both spellings against that pair:
`['pens']` and `['pen']` respectively, and `[]` for the same-subject pair, for
a method word like "cost", and for a word belonging to the target. A
rebind that fails any of that is REFUSED (`_near_refuse`), not deleted --
the failure is a property of the PAIRING of two tasks, not of the stored
entry, which is still exactly as good for its own task or the next one that
rebinds cleanly. Also unbuilt, and designed here rather than required by any document in
this repo -- `docs/SPEC.md` has no memo section at all: a fuller replay tier (stored worker results,
an environment closure over system/skills/grader/sandbox digests, hermeticity
and volatility gates) -- nothing here replays a candidate, so those gates would
guard a path that does not exist. `would_have_cost` on a `memo_hit` row is
always 0.0, because the avoided proposer call never named a provider or a model
and pricing it would be an invented number; `calls_avoided` carries the saving.
Gemini explicit context caching is deferred, not forgotten -- unreachable
through the OpenAI-compatible shim this pool talks to, and needing its own
create/TTL/delete handle lifecycle -- and is labelled `prefix_cache="explicit"`
in the registry and printed by `swarmd providers` so the gap reads as deferred.
`LLMRequest.metadata['prefix_group']` model affinity is not built. No document
in this repo asks for it -- grepped: `prefix_group` appears in this file and in
`interview_prep.md` and nowhere else, not in `SPEC.md`, not in the source. It
is my own idea for keeping a run's calls on one model so the provider-side
prefix survives, and it is a routing change with its own failure modes, so it
is named here as a design note rather than as an unmet requirement.
`CachedProvider` still replays a stored response carrying the ORIGINAL call's
`cached_tokens`, which corrupts nothing today (a hit writes a `cache_hit` row
and `prefix_cache` sums `llm_call` rows only) but would double-count for a
future reader aggregating off response objects rather than ledger rows.
`_memo_admission` deleting a memo the run store disowns is a rollback for the
MEMO only. Provenance rollback for the SKILL LIBRARY is not built: `_distill`
calls `SkillLibrary.propose()` for every passing node before the run's final
status is known, `Skill.provenance_run` records the id but nothing ever reads
it back against that run's later status, and there is no code path that
retracts or re-checks evidence a skill already banked once its originating
run turns out interrupted, aborted, or otherwise not `completed`.

The memo is wired into `swarmd swarm run` and the control plane only: not into
`swarm session` (it cycles repeated tasks to build a learning curve, and
removing synthesis from repeats 2..N would flatten the very curve it measures),
not into `eval`, and not into the CLI `runs resume` path, so a CLI-resumed run
cannot settle the memo its earlier process wrote -- that entry simply ages out.
And a further design for learning that I sketched and did not build. There is
no "learning spec" to cite: grepped, `Episode`, `ObservationStore`,
`MIN_DISTINCT_CRITERIA` and `MIN_DISTINCT_RUNS` appear only in this file and in
`interview_prep.md` -- no document in `docs/` defines any of them and no source
file mentions them. So these are my own unbuilt ideas, not unmet requirements:
`Episode` / `ObservationStore` / out-of-band corpus-wide promotion,
`MIN_DISTINCT_CRITERIA` and `MIN_DISTINCT_RUNS`, `Skill.transfers` and the
transfer-rate retrieval term, the never-transferred prune rule, `mark_retired`
and the no-resurrection loop, the "(unproven: worked X of Y)" prompt
annotation, and the families endpoint.
`merge_templates` is implemented and unit-tested but not yet wired into
instruction construction, because a single-task distillation has no second
template to merge against.

---

## 2026-08-31 - The meter was reading double

Four defects, all in the accounting path, all found by asking one question of
the code that no test had asked: *what does the journal contain after exactly
one successful call?*

**One call cost the day two requests.** The ration and the budget tracker share
a journal, and both wrote to it in full. A success produced a reservation row
(`+1 request`, `+1,250 estimated tokens`), a settlement row (`0 requests`,
`actual - estimate`), and then a third row from the pool's success path with
the whole cost again. `window_state`, `grant_state` and the session envelope
all sum that journal, so every rationed call was billed twice.

Every consequence pointed the same way — stopping early — which is why it
survived a month of use and a full test suite. A free tier was declared spent
at half its capacity. The session envelope handed out half its slice. And the
`groq 101,522 / 100,000 tokens BLOCKED` line this project has been quoting as
evidence of a tight day was a doubled reading of roughly 50,000 real tokens,
against a cap that was itself wrong by half. Two independent errors in the same
direction made one day look four times tighter than it was.

The fix is a condition rather than a deletion, and the condition matters: an
unrationed provider gets no reservation row at all, so for that provider the
pool's row *is* the charge. It now records zero cost when the call was
rationed, and keeps the row for what only it knows — `cached_tokens` and the
resolved model name.

**The token estimate was measured from itself.**
`observed_tokens_per_request` filtered the journal to rows carrying a request
count. That keeps the reservation row, which holds the ESTIMATE, and drops the
settlement row, which holds the CORRECTION. So the figure the ration reserves
against was an average of the number it had itself produced: it returned about
the midpoint of the 1,250-token default and the truth and could never converge.
This is why `docs/CAPACITY.md`'s live reading of "~1,026 tokens per call" is
withdrawn rather than adjusted — it was never a measurement.

**A grant taken per slot, settled per attempt.** The reservation was taken
once before the model loop, but the loop can send more than one request: a
model that raises `ProviderError` settles the grant and `continue`s to the next
model, which settles the SAME rid again. The error settlement returns the
request and the estimate; the success settlement then writes only
`actual - estimate` on top. Net for the served call: zero requests, and a
negative token count whenever the response came in under the estimate. A
provider that failed over from one model to another was CREDITED for the work
it went on to do.

**And reserved against the wrong model.** `models[0]`, always — while groq's
200,000-token cap is per MODEL. The whole slot was rationed out of one model's
share and the rest of the account sat untouched, which is the same failure this
project already fixed once at the provider table and did not think to look for
one layer down.

Both are fixed by moving the cost gates INSIDE the model loop, so one attempt
is one unit of accounting. The `try/finally` that releases an unsettled grant
moved with them, and it is deliberately a `finally` guarded by a flag rather
than a `release()` at each refusal site: a gate added later cannot forget it.

### What made these findable when the suite could not see them

Every one of these passed 1,244 tests. They were invisible because the tests
asserted ROUTING decisions -- which provider, in what order, backed off how
long -- and never the ledger those decisions leave behind. The probe that found
them was six lines: make one call, print every journal row. Each fix is now
pinned by a test that was verified to FAIL against the previous code, because a
regression test that passes both ways is a comment.

The kill-and-resume test caught the change honestly and loudly: its ration
constant of 70 encoded the doubled charge, so with calls costing half as much
the run finished without ever parking and the test said so instead of passing
vacuously. Halved to 35, with a comment saying what the number means.

### Two smaller ones, same sprint

`Pacer.park` keyed an anonymous waiter on `len(self.waiting)`, and two agents
parking in the same tick both read the same length before either inserted -- so
one key covered two waiters and the discard on wake removed a key the other
still needed. `pool.py` reads `agent_id` from request metadata and synthesis
calls carry none, so anonymous is the common case, not an edge one. Replaced
with a counter incremented under the lock.

And `task_shape`'s docstring described its fallback as firing when a sentence
has no determiner anywhere. It also fires when a determiner-introduced phrase
heads on a word that is itself in `METHOD_LEXICON` -- "count the count" -- so
the residual it documents was narrower than the residual it has. Corrected in
the docstring and in the test that pins it; no behaviour change, which is the
point: the comment was the thing that was wrong.

---

## 2026-09-01 - The dashboard could watch, but it could not act

The service and the dashboard were built together and tested apart, and four
things fell into the gap between them. All four have the same shape: the
control plane offered something and the browser could not reach it.

**The dashboard sent no operator token.** `SWARMD_API_TOKEN` gates every
mutating endpoint and the event stream. The dashboard's `fetch` helper set one
header, `Content-Type`. Reads are ungated, so the page loaded, filled with real
provider data and looked entirely healthy -- and then every button returned
401 and the websocket closed with 1008 and reconnected forever behind a
flickering "Connecting". Nothing in the UI could say why, because nothing in
the UI knew a token existed.

Fixed in one place: every request the app makes now goes through `lib/api.ts`,
which attaches `X-Swarmd-Token`, puts the same value in the stream's `?token=`
(a browser cannot set a header on a handshake), and rewrites 401 into the
sentence that says what to do. The new `GET /api/auth` lets the page ask
whether a token is wanted before anything is attempted, so a locked control
plane shows a field instead of a working-looking screen; a pasted token is
checked there rather than by spending a run to find out. The stream reconnects
when the token changes, and a 1008 close now reads "No token" and stops
retrying instead of pretending to be a network problem.

This is not user auth and does not become it. One operator, one credential, no
accounts -- ADR-013 is unchanged. What changed is that the operator can now
supply the credential from the thing they are looking at.

**A parked run could only be resumed from a terminal.** The pacer parks a run
that has spent the day's ration; the control plane has listed those runs at
`/api/runs/resumable` and resumed them at `/api/runs/{id}/resume` for as long
as it has existed. The dashboard called neither. Someone watching the screen
could see the run stop, could read the reason and the time capacity returns,
and then had to open a shell to do the one thing the situation calls for. The
Harness view now lists parked runs and resumes one in place, with the
idempotency key that makes a double click one resume rather than a 409.

That listing needed a change in the service too. A run this process is still
working on is on disk with status `running`, and so is a run abandoned by a pod
that died -- identical rows, and resuming the first is refused. The listing now
marks which runs are live here, so the button is offered only where it would
work.

**The dashboard never showed what a run produced.** It showed which agents ran,
what each cost, what was contained, which criterion was frozen and which plan
was chosen -- everything about HOW the run went, and nothing about what came
out of it. The artifacts were in the report the whole time. Reading the answer
meant opening the JSON. The Decisions view now renders them per node, with the
attempts and the skill used beside each.

**And the eval was counting capacity as failure.** A run stopped by something
other than the task -- every provider spent, the cost ceiling reached, the
process killed -- returns `solved=False`, which is the same value a run that
tried and failed returns. `summarise` counted them. A sweep long enough to
outlast the day's allowance therefore measured how much quota was left and
reported it as capability, and the damage was not symmetric: the loss lands on
whichever arm was running when the wall arrived, so the DELTA moved too.

Runs that never reached the task are now excluded from every figure, dropped
from the pairing, and counted out loud, because a rate over half a sweep
presented as a rate over all of it is just a different wrong number.
`failed_criterion` still counts as an attempt -- no criterion surviving attack
is a real thing this system failed to do.

The test fixture had been hiding it: `_outcome(solved=False)` stamped
`status="error"` with no nodes, which is the shape of a run that never started
rather than one that failed. It now builds what an unsolved run actually looks
like.

**And then the sweep itself turned out to be measuring nothing.** With the
exclusion fixed, a 100-cell ablation was started over HTTP and left to run. At
cell 18 it was reading treatment 6/9 against control 3/9 -- a positive result,
the opposite of the -0.400 this project has been carrying since 2026-08-29.

It was not a result. `SwarmRun` resolves `self.skills = skills if use_skills
else None`, so a run constructed without a `skills=` argument gets None in
BOTH arms. The CLI has passed a library since that exact defect was found and
written up on 2026-08-29; `_eval_runner` in the control plane never did. Every
eval ever started from the dashboard compared a configuration against itself
and reported "no measured improvement" -- the same words the real experiment
produces, for a reason that has nothing to do with skills.

The sweep was cancelled at cell 20 rather than spending the remaining 1,500
requests on a null experiment. The endpoint now refuses at submit time, with
the reason, when the treatment arm would have nothing to retrieve: no library
configured, or a library with nothing approved in it. Refused rather than
warned, because a warning in a job log is not attached to the number, and the
number is what gets quoted.

The lesson is the same one as the meter: the CLI and the service are two
clients of the same code, tests covered the CLI, and the fix landed on one
side. Both paths now construct the run the same way, and the test that pins it
starts a control plane with no library and asserts the refusal.

**Then the experiment itself turned out to be measuring memorisation.** With
the library wired in, the next step was to build one: `POST /api/sessions`,
ten tasks, auto-approved. The session draws its curriculum from
`suite(arms="both")` -- the same ten tasks `swarmd eval` then measures. A skill
distilled from `pub-extract-1` and retrieved while solving `pub-extract-1`
again is not learning; it is the answer, written down. It would have moved the
treatment arm for a reason that does not survive a task the library has never
seen, and the report would have called it an improvement.

`public` and `custom` are disjoint sets that already exist, so a session can
now be pointed at one and the eval at the other. The default is unchanged --
a session is also just how a library gets built for use -- but a session run
for a *measurement* has to name its training set, and this one does:
`{"arms": "public"}` trains, `{"arms": "custom"}` measures.

Three defects, all found while trying to produce one number, and all of the
same kind: the machinery worked and was pointed at the wrong thing. The
double-charged meter was a number that described something other than what it
claimed; so was an ablation with identical arms; so is a success rate measured
on the training set.

**A fourth, found the same way.** The first session died after one task with
`ConnectionRefusedError` out of `_auto_approve`: `DATABASE_URL` pointed at a
Postgres that was not running, and the approval store is not touched until the
first consolidation. So the pod reported ready, the job was accepted, one
task's provider quota was bought, and only then did the run discover it had
nowhere to put what it had learned. `/readyz` now checks the approval store,
and `POST /api/sessions` refuses before anything is spent.

**And with all four fixed, the measurement still did not happen -- for a
reason worth more than the measurement would have been.** The clean experiment
ran: ten tasks on `public`, live providers, 458 seconds, 22 skills proposed.
Zero approved. Zero even *queued*.

A skill reaches the human queue only once it is `promotable`: verified on two
distinct task shapes, which is the bar that makes "this transfers" a question
with an answer. All 22 carried evidence from exactly one shape. The five
training tasks have disjoint output shapes, so no approach was ever proposed
by two of them.

The first guess was fragmentation -- `make_skill_id` hashes the instruction
text, the instruction is written by a model, so the same approach worded twice
mints two records. That is true, and it is not the cause: grouping the 22 by
abstracted name and pattern gives 12 groups and still **zero** with two
shapes. A merge was written, tested, measured against the real library, and
reverted, because it fixed nothing that was actually broken here and changed
what "the same skill" means to do it.

The real finding is about the corpus. The transfer bar asks for an approach
that worked on two different kinds of task; a twelve-task suite with twelve
different output shapes cannot supply one. The "volume: 50-200 tasks" item has
been on this list for days as a statistical argument. It is not: it is a
structural precondition, and the tasks have to come in FAMILIES that share an
output shape, or the library can never promote regardless of how many there
are.

Two ways forward, and the choice is the operator's rather than mine: grow the
suite into families, or bypass the bar deliberately with
`approve(..., force=True)` -- which records the bypass on each record -- and
label the result as measuring RETRIEVAL rather than transfer.

**Acceptance criterion 2 did land.** Both holdout tasks ran end to end on live
providers with no code change: frozen criterion, plan, graded nodes, $0.00,
no simulated rows. `hold-schedule-1` passed 15 of 20 nodes and produced the
right answer (13 minutes); `hold-logistics-1` passed 0 of 30 and produced a
wrong one. Which is the honest reading of that criterion: the system takes an
unseen task end to end. It does not reliably solve one.

## 2026-09-01 - The learning loop turns

G-4 has been open since this project could measure anything, always reported
the same way: "no measured improvement". Every time, the reason turned out to
be mechanical. Today it was answered as a design question rather than chased as
a bug, and the reasoning is [ADR-014](adr/ADR-014.md).

**The diagnosis.** A skill becomes reviewable only once it is `promotable`:
verified on two DISTINCT task shapes. That bar is what makes "does this
transfer?" a question with an answer -- one task's evidence, drawn twice, says
nothing about a second task. Two independent preconditions have to hold before
anything can clear it, and neither held.

*The corpus had no shared structure.* Twelve tasks, twelve disjoint output
shapes: a median repair, a colour-ordering puzzle, a pagination strategy. No
approach distilled from one is ever proposed by another, so the bar is
unreachable at any sample size. A thousand unrelated tasks would promote
exactly as many as twelve. This is the thing the "50-200 tasks" item had been
quietly getting wrong: it is not a statistical argument about confidence
intervals, it is a structural precondition, and what the corpus needs is
FAMILIES.

*And identity fragmented on wording.* `make_skill_id` hashes the instruction,
the instruction is written by a model, and the same approach comes back phrased
differently every run -- so each phrasing minted a record starting again from
one shape.

**The part worth keeping.** The merge was tried first, alone, measured against
the real library, and reverted: on a corpus where no two tasks share an
approach, grouping records changes nothing. That measurement was right and the
conclusion drawn from it was too narrow. It showed merging is INSUFFICIENT, not
that it is unnecessary. With families in place it is load-bearing, and both
changes had to land together.

**What was built.** A `train` arm: fifteen tasks, five families of three, each
family sharing an output shape and aligned with a KIND of work the custom arm
also needs -- diagnosis-and-fix, checkability verdict, constrained enumeration,
rate-limited planning, name normalisation -- and with none of its content. It
is not evaluable, and that is enforced rather than observed: `SessionRequest`
accepts `train`, `EvalRequest` does not, so measuring over the tasks a library
was built from is unexpressible. A convention a person has to keep is exactly
what failed here a few hours earlier.

And an identity for evidence: the abstracted artifact shape plus the kinds of
check that graded it. The plan step is deliberately not in the key, and that is
the part that took a measurement to see -- plans are synthesised per task, so
steps never recur, so a key containing one can only ever match another proposal
from the SAME task. Which is precisely the evidence the bar refuses to count.
The cost is stated rather than hidden: two steps of one task that produce the
same shape under the same checks now merge, and the instruction kept is the one
proposed first. They were already competing for one retrieval slot.

**The result.** Fifteen tasks on `train`, live providers, 667 seconds, 33
records:

```
records stored           : 33
approaches after merging : 10
reaching 2 task shapes   : 2      <- was 0, always, for the life of the project
```

`approach: produce diagnosis, fix`, confirmed by the permissions task and the
timezone task. `approach: produce duration_minutes, strategy`, confirmed by two
different rate-limit tasks. Two families, each approach verified by a task the
other member never saw. Both approved on their own evidence -- `approval_note`
empty on both, so no `force` and no bypass. The first skills in this project's
history to clear the bar.

`swarmd skills merge` handles libraries written before this: it replays stored
records through `propose`, so the merge rule lives in one place, reports what
would collapse, and writes nothing without `--apply`.

**What this does not yet establish.** Whether retrieving those skills helps.
The ablation over the unseen `custom` arm is what answers that, and with two
approved skills aligned to two of the five custom tasks the effect it can show
is bounded. A null at this size means "not enough library to move five tasks",
not "skills do not help" -- and the fix for that is training volume, which is
finally worth spending quota on.
**And the ablation was started, then stopped at cell 5 of 30 -- deliberately.**

Before spending the rest of the day on it, one free check: does either approved
skill retrieve for any custom task? The answer, over both the bare prompt and a
task-plus-step query matching what `worker.py` actually asks:

```
cus-wrangle-1    0 hits
cus-paper-1      0 hits
cus-repo-1       0 hits        <- the task the diagnosis skill was FOR
cus-puzzle-1     1 hit         <- approach: produce diagnosis, fix
cus-api-1        0 hits
```

One hit in five, on the wrong task. The cause is not the skills; it is the size
of the index. `_idf` computes inverse document frequency across APPROVED
skills, and with two of them it produced two distinct values across 32 terms.
An IDF with no spread is not a weighting -- scoring collapses to "how many
terms overlap", which is how advice about diagnosing a permissions failure came
to be offered to a puzzle about the order of coloured houses.

So the experiment could not discriminate, and its result was knowable in
advance: a null, caused by the library being below the size at which retrieval
means anything. Running it would have spent roughly 500 requests -- most of
what the day had left -- to confirm that. Cancelled at cell 5.

**This is a threshold, and it is worth stating as one.** A skill library is a
retrieval index, and an index over two documents cannot rank. Somewhere above
that, IDF starts carrying information; where exactly is itself a measurement
nobody here has taken. What is certain is that two is below it.

The corpus went from five families to eight in response -- reconciliation,
sequencing, thresholds, authored to the same pattern as the five that worked
and marked in the file as not yet run. More families is the lever: each one
that promotes an approach adds a document to the index, and it is documents
that give IDF something to weigh.


## 2026-09-01 - Two ways to lose a human decision without noticing

Both found by running the thing that was supposed to preserve them.

**The merge silently un-approved the library.** `swarmd skills merge` replays
stored records through `propose`, which is right -- the merge rule then lives in
one place instead of being reimplemented by a migration. But `propose` mints
CANDIDATES. So the replay returned a library where two skills approved on their
own evidence were pending again, and the `uses`/`successes` counts that pruning
reads were back to zero. A migration that destroys the reviews it exists to
preserve.

Fixed by carrying the decision across, matched on `skill_id` -- the hash of the
surviving instruction -- so the approval follows the text a human actually
looked at. When two phrasings merge the loser's approval does not transfer: it
was granted for text no longer stored. Pinned by a test that approves a record,
records a use, merges, and asserts both survive.

**And the chain approved in bulk.** The unattended script ran: train, merge,
approve everything promotable, run the ablation. The RUNBOOK names that failure
in the ApprovalQueueStale entry -- *"approving in bulk to clear the queue
defeats the control"* -- and the script did it anyway, because it was written to
get to the measurement rather than to hold the gate.

It let through `approach: produce mapping, summary`, whose own generality score
was **0.25**: the instruction is the step restated, teaching an output shape and
nothing about how to reach it. That score exists as a reviewer signal for
exactly this case, and nothing between the bar and the library was reading it.
Now rejected, with the reason on the record.

The distinction the script blurred: `promotable` answers *"is there enough
evidence to ASK a human"*. It does not answer the question. The script now stops
at the candidates and prints them with their generality and their instruction;
approval and starting the ablation are separate deliberate acts.

**Where the measurement stands.** Three approved skills gave an IDF index of 38
terms with **three** distinct weights, and retrieval covered 1 of the 5 tasks
about to be evaluated -- still the diagnosis skill offered to the colour puzzle.
Rejecting the weak one puts it back to two. The ablation is still not worth
running: an index that cannot rank cannot produce a result that means anything,
and the honest spend is another training pass rather than a sweep.

A second pass over the same corpus is not wasted even though the task shapes
repeat: a family member that failed its criterion on the first pass and passes
on the second supplies the second shape its sibling's approach was waiting for.
Three of eight families promoted an approach on pass one.

## 2026-09-01 - The loop turned, and returned a negative on its own skills

Not from the ablation. From the mechanism.

**The economy pruned both approved skills for failing.** They were retrieved
during training and scored:

```
approach: produce diagnosis, fix              0 successes / 26 uses   (0%)
approach: produce duration_minutes, strategy  5 successes / 27 uses  (19%)
```

Both under the consolidator's 30% floor, both retired automatically with the
reason on the record. That is propose -> gate -> retrieve -> measure -> prune,
running end to end and reaching a verdict without anyone asking it to. It is
also the first empirical statement this project has ever made about whether its
own skills help: **retrieved 53 times, succeeded 5.**

**Then the review said why.** Four approaches later cleared the evidence bar.
Reading them, three were contaminated with their own source task:

- `produce reconciliation` (generality **1.00**, the top score) instructs every
  future run to emit keys `stock_count` and `ledger_count` -- the stock task's
  vocabulary, welded into identifiers where the abstraction could not see it.
- `produce verdict` (0.86) is advice about how UPTIME is measured -- synthetic
  probes, RUM, probe locations. That is `trn-checkable-1` subject matter, not a
  method for deciding whether any claim is checkable.
- `produce count, reasoning` (0.57) ends `<NUMBER>! = 6`. The factorial's
  ARGUMENT abstracted; its RESULT did not. One task's answer, in a method's
  grammar -- the exact poisoning `validate_instruction` exists to stop, and it
  cannot: `shared_literals` compares the instruction against the TASK, and 6
  never appeared in the task. It was computed.

One survived: `produce count`, three shapes, no literals, a real method (fix a
position, count the arrangements of the rest). **One clean skill in four.**

So the 0/26 is not mysterious. A worker handed advice that names another task's
keys, or another task's domain, or another task's answer, is worse off than one
handed nothing -- which is what the retrieval threshold's own docstring has
predicted all along.

**The identifier leak is fixed.** `strip_source_terms` now splits on the
boundaries a writer actually put there -- `_`, `-`, camelCase -- and strips a
token when every part came from the task and not all of them are method words.
So `stock_count` collapses to `<TERM>` while `sort_by_price` survives, because
an identifier made only of method vocabulary is describing the work rather than
the thing worked on. The other two leaks are recorded here with examples and
are not yet addressed: domain content presented as method, and a computed
result that never appeared in the task for `shared_literals` to compare against.

**And the identity was wrong in a way only a measurement showed.** The key
included the criterion's check kinds. But the criterion is authored fresh for
every run (ADR-009), so its check set differs between two runs of the same
work -- which reintroduced the exact fragmentation the key exists to remove,
one level up. Measured on the 38-record library rather than argued:

```
name + check kinds   38 approaches,  3 clearing the bar
name alone           34 approaches,  5 clearing the bar
```

Keyed on the artifact shape alone now. The test that asserted the old rule was
replaced rather than deleted, and says why it was wrong.

**Two operational lessons, both learned by losing something.**

`swarmd skills merge` replays records through `propose`, and `propose` mints
CANDIDATES -- so the first version silently un-approved the library, dropped
every retired record, and reset the use counts pruning reads. It erased the
0/26 verdict that had cost 26 retrievals to earn. The merge now carries
decisions at the approach level: a rejection is never resurrected, an approval
transfers only when the surviving instruction is the one that was approved, and
an approval that cannot be carried is reported rather than dropped quietly.

And: **do not hand-edit a library a session has open.** A restore from backup
was clobbered minutes later by a training session that had loaded the file at
startup and saved at the end. Last writer wins, and the last writer was holding
a stale copy.

**Where G-4 stands.** Not "does self-learning work" -- the loop demonstrably
runs and produces a verdict. The blocker is now **distillation quality**, with
a rate attached: three of four promoted approaches carried their source task.
Fixing that is a specific piece of work with examples, which is a better place
to be than "no measured improvement".

## 2026-09-01 - Design fault or model fault: tracing each leak to its line

The library that reached retrieval scored 5 successes in 53 uses, and three of
the next four candidates carried their own source task. The tempting reading is
"the model writes bad advice", and it is worth resisting: there is no prompt
that makes a model reliably produce advice general enough for tasks it has not
seen, and believing there is turns a fixable pipeline into a vendor problem.

So each class was traced to the line that let it through. [ADR-015](adr/ADR-015.md).

**`stock_count` -- an identifier naming the source task.** `strip_source_terms`
compared WORDS against the task's vocabulary. `stock_count` is not in it;
"stock" and "count" are. The abstraction tokenised prose and never considered
that a writer packs two words into one identifier. **Design.** Fixed: split on
the boundaries a writer actually put there -- `_`, `-`, camelCase -- and strip
when every part came from the task and not all parts are method vocabulary. So
`stock_count` collapses and `sort_by_price` survives.

**`<NUMBER>! = 6` -- a computed answer.** This one is two characters. The NUMBER
pattern ended `(?![\w.])`, rejecting a following period so `1.25` would not be
split in half -- and rejecting the period that ends a sentence with it. A number
in final position was invisible: `"the answer is 42."` yielded no literals at
all. The end of a sentence is where an answer gets stated. **Design.** Fixed:
reject a following period only when it is a decimal point.

`shared_literals` could never have caught this one either, and that is worth
saying plainly: it compares the instruction against the TASK, and a computed
answer never appeared in the task. It was derived.

**Advice about synthetic probes and real-user monitoring.** Distillation
abstracts the PLAN STEP, and the step is prose written for one task by a planner
that knows the domain. Abstraction removes literals and words the task used; it
cannot remove domain knowledge the planner introduced, because "domain" is not
a structural property. `probes` and `monitoring` look exactly like `parse` and
`validate` to any rule that does not already know the subject. **Design, and
the deepest of the three.**

**Zero of three were model faults.** In each case the model did what it was
asked and the pipeline failed to enforce the property it needed.

### The signal that was measured and then not shipped

For the third class a detector was built: the share of an instruction's content
words appearing in neither the task nor the method lexicon -- the symmetric
partner to `strip_source_terms`, which handles words FROM the task, catching
words the planner INVENTED. Measured against the four candidates already judged
by hand:

```
REJECT (leaked keys)     ratio 0.41
REJECT (domain as method) ratio 0.52
REJECT (computed answer)  ratio 0.35
APPROVE (clean)           ratio 0.33
```

0.35 against 0.33 is not a threshold, it is a coincidence with two decimal
places. Shipping it would have meant tuning a float on four samples to
reproduce a judgement already made by hand, which is how a metric becomes a
rationalisation. Not shipped, and recorded here so the next person does not
rebuild it.

### The deeper fault, and what compensates

Distillation is the one place in this system that **trusts instead of
verifying.** Everything else is criterion-first: a claim is not believed until
something independent checks it. A skill claims *this approach transfers*, and
until it is retrieved nothing tests that. It enters future worker prompts on
the strength of having been distilled.

Which is why the compensating control matters more than another filter. The
mechanism that DID catch these skills is the one that measures them in use --
retrieval scoring and pruning -- and it worked, retiring both automatically with
the reason on the record. It was only slow: `prune` runs at consolidation, every
N tasks, while a skill can be retrieved by every node of every task in between.
That is how one reached **26 uses at 0%**.

So retrieval now applies the same rule against the same constants: a skill at or
past `PRUNE_MIN_USES` with a success rate below `PRUNE_MIN_SUCCESS_RATE` is not
offered, even before consolidation formalises the retirement. Damage capped at
the evidence threshold instead of at the consolidation interval. Consolidation
still does the retiring; this only stops the bleeding.

### A fourth channel, larger than the three above

Auditing the library for the two closed classes turned up one the analysis had
missed. `_distil_instruction` wrote the artifact KEY NAMES into the advice --
`Produce a JSON object with these fields: most_users (bool),
preference_threshold (int)` -- and those keys were chosen by one worker for one
criterion. **17 of 31 live records carried them, against 8 for the identifier
leak.** The skill's NAME is derived from the same keys, and `skills_block` was
printing it into the prompt beside the instruction, so the channel was open
twice.

What settles it is that they are redundant. The worker is shown its own frozen
criterion's exact requirements -- added because withholding them was the
largest single cause of live runs failing -- so it already knows which keys it
must produce. A retrieved skill naming a different set can only agree by
accident, and disagreeing produces correct data under keys nothing is looking
for: the exact failure the criterion block exists to prevent.

Removed from both places. The instruction records the KINDS of value the work
produced and says to take key names from the reader's own criterion; the name
stays as the library's identity and is no longer shown to workers.

The first attempt replaced the names with a field COUNT and lasted about an
hour. `validate_instruction` compares whole literals against the source task,
so a skill saying "3 fields" distilled from a task about "3 pens" was refused
as a leak -- and every skill from that run was lost. The old comment in
`_distil_instruction` warns about exactly this for the success count; I walked
into it from the other side. The shape is spelled in words now.

### What is still exposed

Domain-as-method still reaches the library. It now costs at most five
retrievals, and the human gate sees it first -- which is how the uptime-probes
candidate was caught. But the gate's own signals are weak: `generality` scored
**1.00** on the worst candidate in the set, the one naming another task's keys,
because it measures method-vocabulary density and contamination does not reduce
that. A reviewer reading the instruction catches these. A reviewer reading the
score does not, and anything automating approval on it would have admitted all
three.

There is a design that eliminates the class outright and is deliberately not
taken: build the instruction from structured parts only -- artifact shape,
check kinds, method verbs -- and never embed the step's prose. No prose, no
leaks, by construction. The prose is also what tells a worker HOW, so trading
all of it for cleanliness is a bet to settle by measurement rather than
argument, once the library is large enough for an ablation to discriminate.

**And for anyone reading the 0/26:** it is not evidence that skill retrieval
does not work. It is evidence that three specific pipeline defects produced
advice that could not work, two of which are now closed.

## 2026-09-01 - The acceptance-scale chaos run, on real providers

The SPEC Phase 11 deliverable that had only ever run against the simulated
provider, and the thing QA has been refusing to sign since the profiles were
resized: every live run to date had been `smoke`.

```
swarmd swarm run "<logistics scheduling>" --profile standard --chaos --no-skills

run=run-61cd7e40d6  status=completed  34.5s
criterion=9368bc96464e34d0 (4 checks, attempts=1)
plan=655bdafb711165f3 (5 nodes, width=2)
nodes_passed=25/25  contained=0
integrity_hash=ee7e549f587d3ae6
cost=$0.000000 of $0.05 ceiling  calls=17
prefix_cache=896/6736 prompt tokens served from the provider's cache (13.3%)
agents=34 alive=25 bankrupt=0 contained=0
```

Nine of thirty-four agents were killed mid-run. Each replacement logged
`resumed` then `skipped_generate` -- it read the checkpoint and did not re-buy
the generation -- and every node still passed with the integrity hash intact.
That is the recovery guarantee holding on real traffic rather than on a
deterministic mock, which is the only version of it worth anything.

`--no-skills` deliberately: this is a resilience gate, and skill retrieval
would make the run's shape depend on library contents that change between runs.

**First live prefix-cache reading: 13.3%**, on openrouter. Worth naming
carefully, because two different caches get confused in this repo's own notes:
this is the PROVIDER's prompt-prefix cache, measured on the run-stable system
block, not the semantic run memo. The memo's hit rate on genuinely novel tasks
is still unmeasured and still expected to be near zero by construction.

`deep` has still never met a real provider, and neither has the 500-agent
claim. The gap narrowed; it did not close.

## 2026-09-01 - A criterion nothing could satisfy, and the bar that found it

The acceptance bar was restated: not 500 agents, but **5-10 agents running
perfectly on a task nobody scoped**. Measured rather than asserted, on tasks
belonging to no suite in this repo:

```
bakery waste flags      --agents 5              14/14 nodes   30.0s
library overdue fines   --agents 8               8/8  nodes   16.4s
server uptime shortfall --agents 10 --chaos     10/10 nodes   30.2s
logistics scheduling    --profile standard      25/25 nodes   34.5s  (9 killed, resumed)
```

All $0.00, all with integrity hashes, criteria authored and frozen per run.
Then the holdout warehouse task, which had failed 0/30 nodes on 2026-09-01
morning, failed again -- **0/12, same criterion hash**. Reproducible total
failure is the useful kind.

**The criterion was self-contradictory.**

```
numeric_range  total_boxes  min=8 max=8
numeric_range  total_boxes  min=9 max=9
```

Eight is correct. Nothing is both, so every worker failed every attempt, on
every run, forever. Two proposers disagreed about the answer and the consensus
merge unioned their checks rather than noticing they could not both hold. The
same criterion also demanded `boxes_per_order` AND `order_boxes` as required
keys -- two proposers' names for one thing, both mandatory.

**`attack` cannot find this, by construction.** It tries degenerate candidates
-- empty, constant, task-echo -- and a criterion nothing can satisfy rejects
those exactly as it rejects correct work. It survives every attack while making
the task unsolvable. `is_weak` catches a criterion that accepts everything;
there was no counterpart for one that accepts nothing.

`Criterion.contradictions()` is that counterpart. It intersects every
`numeric_range` asserted on a key and reports an empty intersection, plus any
single range with min above max. Only PROVABLE conflicts: two proposers naming
different keys may well be over-constrained, but that is a judgement about
intent, while disjoint numeric ranges on one key is arithmetic. `attack`
appends them unconditionally rather than only when the sampled attacks found
nothing, because this is a proof rather than a sample.

**The memo replayed it, and the memo caught it.** A frozen criterion is
memoised, so re-running the task served the impossible target again --
`attempts=0` on both failing runs. That could have made the fix useless. It did
not, because `_criterion_from_memo` re-attacks every stored criterion against
THIS run's task using the code running now, which its own docstring says is the
point: "the check list it runs is the code running NOW rather than the code
that froze it".

So the fix reached a criterion frozen before the fix existed. Live proof:

```
before  criterion=43b7c37957bb8bde (7 checks, attempts=0)   0/12 nodes
after   criterion=24a950ae092e9b82 (4 checks)              14/14 nodes
        - numeric_range total_boxes min=8 max=8     <- one check, and correct
```

The memo was rejected on re-attack, synthesis re-authored, and a task that had
never once succeeded now passes every node.

**Where the bar stands: five unknown tasks at 5-10 agents, every node of
every one solved.**

That last clause needed a reporting fix to state truthfully. `nodes_passed`
counted AGENT OUTCOMES under a node's name -- `results` holds one entry per
agent, and a node is run by a pool -- so the scheduling task read "7/8 nodes"
when all four of its nodes were solved and one pool of two had a single agent
fail. Every run this system has ever reported was understated the same way.
The holdout `hold-schedule-1` that this file recorded as 15/20 was 4 of 4
nodes.

Fixed: `nodes_passed` counts distinct plan nodes with at least one uncontained
passing agent, `agents_passed` counts what the old field counted, and both are
printed. A pool losing a member is a population search working, and the report
should not read like a failure.

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
- [x] Batched generation: one call per node, verified by counted calls
- [x] Semantic cache wired to the provider, exact-keyed, eval runs refuse it
- [x] Checkpoint recovery in the swarm, tested by counting calls not comparing output
- [x] Rogue seeding with per-detector attribution; SPEC Phase 8 gate runs in CI
- [x] Agent count selectable per run from the service, dashboard and CLI
- [x] Live providers wired and measured: 4/4 live, registry corrected against
      reality, NVIDIA added as a grant-backed tier, Cerebras removed (402)
- [x] Budgets tracked across minute/hour/5h session/day/week/month, summed from
      a durable per-credential journal, surfaced in CLI, API and dashboard
- [x] Real runs SOLVE: 8/8 nodes repeatably, 20% at task level in the eval
- [x] Profiles sized to the measured budget; any agent count honoured exactly,
      with a preflight that prices it before the run starts
- [x] Distillation records an approach rather than the answer it produced
- [x] Task FAMILIES sharing an output shape, without which no skill ever
      reaches two distinct shapes and the library can never promote. Done
      2026-09-01: a `train` arm of 15 in 5 families, plus an identity that
      lets two phrasings of one approach be one record. 2 skills approved on
      their own evidence, where every previous session produced 0
- [ ] Volume, now that it can pay off: 5 families of 3 promoted 2 approaches.
      More families and more members per family is what turns 2 into a
      library big enough to move a five-task eval
- [x] The ablation actually compares two different things (it did not before)
- [x] pass@k and node pass rate reported and traceable
- [x] Re-measure learning: the node-anchor fix was never the blocker. The
      library could not promote at all, for two reasons neither of which
      was quota (ADR-014); the ablation over the unseen `custom` arm runs
      on a library with approved skills for the first time
- [ ] The learning curve: 50-200 tasks with the control arm, then and only then
      generate BENCHMARKS.md and make an improvement claim
- [x] Product posture: operator token, edge allowlist, rate limits, JSON logs
      with redaction, SECURITY.md, ADR-013
- [x] Narrow the egress NetworkPolicy to provider CIDRs -- done in
      deploy/k8s/base/rbac-and-config.yaml, with the failure mode it introduces
      written next to it: a provider changing ranges looks like an outage
- [x] Rollback exercised on a real cluster (k3s): v1 Ready, v2 out, undo back
      to v1, each step gated on rollout status. Surfaced the apply/undo
      annotation trap, now in the runbook
- [x] The image builds and the deployment starts: three defects found by doing
      it -- missing README build input, missing serve/postgres extras, and a
      default command binding container-loopback. Guarded by tests
- [x] Debt cleared: no unmarked mock on any user-facing path. make_router
      raises instead of downgrading, FallbackRouter no longer recommends a
      mock tail, and 'mock' now aliases the tainted simulated provider
- [x] Traceability view + the ledger commands RUNBOOK.md names
- [x] Skill approval through the HITL queue: SkillGate is the only path in
      distillation, sessions, the CLI and the approve endpoint
- [x] Supervisor inside the swarm loop: proposes from the failure taxonomy,
      the consolidator gates, ineffective patches are reverted
- [x] ADRs 007, 008 and 010 written -- they were cited 32 times and did not exist
- [x] Idempotent run submission and resume: same key, same body, one run,
      durable across a restart. Per-pod only, and it says so
- [x] A repeat task reuses its criterion and plan instead of re-buying them --
      exact key, re-attacked on every hit, never an answer replay
- [x] Worker prompts split run-stable / node-stable / volatile so the shared
      prefix is cacheable, with cached tokens read from the provider or
      recorded as absent, never estimated
- [x] Skill promotion counts distinct task SHAPES, and retrieval checks that a
      pattern's literal kinds are actually present in the incoming task
- [ ] Run the hoisted-vs-legacy node-pass-rate parity gate on live providers;
      until then `hoisted` rests on byte equivalence, not on quality
- [x] A near-match memo tier over the abstract fingerprint, sharing
      generalise.py's notion of a literal rather than growing its own; rebound
      criteria are re-attacked and checked for a leaked source-task subject
      word before they may grade anything
- [ ] Unify the kernel Runtime and the swarm executor: they share the Checkpoint
      contract but not the loop
