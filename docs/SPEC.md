# SPEC — swarmd Technical Specification

**Status:** v3.0 · Companion to [PRD.md](PRD.md) · **Last updated:** 2026-08-27
**Supersedes:** v2.0 (LeadOps flagship — archived at [archive-v2-PRD-leadops.md](archive-v2-PRD-leadops.md))

Rules: each phase ends with a **gate** — a runnable command with an observable outcome. No
phase-N work before the phase-(N-1) gate passes. Deviations need an ADR. Docs drift is a
bug: `flow.md` updates in the same commit as any feature.

**Stack.** Python 3.12+, asyncio (stdlib core), httpx, asyncpg, pydantic, OpenTelemetry,
prometheus-client, pytest + pytest-asyncio, uv. Frontend: Next.js (App Router), TypeScript,
WebSocket transport. Models: pooled free tiers with paid overflow (PRD section 11).

**Mock policy.** The deterministic mock provider exists in `tests/` only, as a test double.
No demo path, no frontend path, and no `swarmd eval` path may reach it. CI enforces this
with an import check. Rationale in ADR-006.

---

## Phases 1–4 — Inherited foundation (complete)

Built under SPEC v2 and carried forward unchanged. These are domain-agnostic and survive
the v3 flagship pivot intact — which is itself the evidence for the kernel-purity claim.

| Phase | Delivered | State |
|---|---|---|
| 1 | Event bus, Task/Checkpoint models, agent lifecycle state machine, priority scheduler with backpressure, heartbeat-requeue runtime, seeded chaos, demo CLI | done — hash equality at kill-rate 0.9 |
| 2 | Stage DAG executor, harness base contract, LLM/Fetch/Store/Verify/Draft harnesses | done |
| 3 | Quality gates, bounded repair, dead-letter, failure taxonomy, approval state machine, audit trail | done, **with carry-over debt** (see 5.1) |
| 4 | Provider health scoring (EWMA), fallback chain, semantic cache, token budgets, OpenRouter adapter | done, **extended in Phase 5** |

Carry-over debt, all discharged in Phase 5:
- HITL approvals are not durable across processes (`cli.py` constructs a fresh in-memory
  store per invocation) — Phase 3's gate does not actually pass
- Router is single-provider; PRD section 11 needs a pool
- No cost accounting in currency, only tokens

---

## Phase 5 — Production floor: state, cost, event spine

The layer everything above stands on. Nothing here is a feature; all of it is the thing
that stops later features from being unfalsifiable.

**Deliverables**
- **Durable state.** Postgres-backed `ApprovalStore` and `Store`, wired into the CLI so
  approvals survive process boundaries. Schema migrations checked in. Local Postgres via
  the existing `docker-compose.yml` (host port 5434).
- **Cost ledger.** Append-only, per-call rows: provider, model, tokens in/out, unit price,
  computed cost, agent id, stage, cache-hit flag. Run cost is a `SUM`, never a counter.
  Hard ceiling enforced at the harness boundary; breach raises and aborts cleanly with a
  report. This is the only source metrics may be computed from.
- **Provider pool router.** Groq, Google AI Studio, OpenRouter `:free`, NVIDIA NIM (a
  grant, not a tier), optional Mistral (behind `--allow-data-training`, off by default),
  paid overflow GLM 5.3 Flash. Cerebras was in this pool at Phase-5 write-up time and is
  not now: its free tier began requiring a card, every call returns 402 (CAPACITY.md §7).
  Empirical rate-limit discovery: a 429 records the observed limit and reschedules rather
  than trusting a hardcoded constant. Health scoring extended from latency/error to include
  rate-limit rejections.
- **Event spine.** `WebSocketSink` joining the existing composite sink beside JSONL and
  OTel. Emits the same spans and chain-of-thought thoughts, with global tick ordering
  preserved across the wire.
- **Prometheus metrics** (`observability/metrics.py`): agents alive, queue depth, cache hit
  rate, kills per minute, run cost, provider rejection rate. Label policy: stage and pool
  only, never per-task identifiers.

**Gate**
```
docker compose up -d
uv run pytest tests/kernel tests/router tests/pipeline -q
uv run swarmd approve <id>            # in a DIFFERENT process from the run that queued it
# -> decision recorded, audit entry present, state survives restart
uv run swarmd providers probe          # discovers live limits per provider, prints table
uv run swarmd demo cost --ceiling 0.05 # deliberate overrun -> clean abort + itemised report
```

---

## Phase 6 — Criterion synthesis and adversarial freeze

**Deliverables**
- `swarm/criteria.py`: N agents independently author a machine-checkable success criterion
  for an unscoped task. Criterion is executable (a predicate over a candidate solution plus
  its execution artifacts), not prose.
- Cross-check: proposals compared; irreconcilable disagreement escalates rather than
  silently picking one.
- **Adversarial pass**: a red-team agent attempts to satisfy the criterion with degenerate
  output (empty, constant, trivially-shaped). If garbage passes, the criterion is rejected
  and re-authored, bounded by a retry cap; exhausting the cap fails the task honestly
  rather than proceeding with a criterion known to be weak.
- Freeze: accepted criterion is content-addressed and immutable for the run. The hash is a
  run output.

**Gate**
```
uv run pytest tests/swarm/test_criteria -q
uv run swarm criteria "<task never seen in development>"
# -> criterion frozen, hash printed, adversarial report shows garbage attempts rejected
# seeded-weak-criterion fixture -> MUST be caught and re-authored, not frozen
```

---

## Phase 7 — Plan synthesis, generic workers, sandbox

**Deliverables**
- `swarm/planner.py`: competing DAG decompositions proposed, judge merges, structural
  validation (acyclic, dependencies resolvable, every leaf reachable) before execution.
  Invalid plans are rejected and re-proposed, never executed hopefully.
- Generated DAG handed to the **existing** `pipeline/dag.py` executor — no parallel
  execution path.
- `swarm/worker.py`: one generic worker. Role, retrieved skill, and budget injected at
  runtime. No per-stage subclasses.
- `harnesses/sandbox.py`: subprocess execution with CPU, memory, wall-clock, filesystem,
  and network caps. Policy violations are structured events, not exceptions to swallow.
- Scale: pool sized to hundreds-to-1000 with LLM calls rationed by the Phase 5 router.

**Gate**
```
uv run pytest tests/swarm/test_planner tests/harnesses/test_sandbox -q
uv run swarm run "<held-out task>" --agents 500 --chaos --kill-rate 0.2
# -> DAG synthesized and validated, executed under chaos, completes
# -> sandbox violations contained, never crash the run
# -> integrity hash matches the zero-chaos run
```

---

## Phase 8 — Red-team runtime organ

**Deliverables**
- `swarm/redteam.py`: continuous monitors over the live action log, with kill authority via
  the same path chaos uses (so containment inherits checkpoint recovery).
- Five detectors, all pure code, no model call: budget siphon, criterion gaming, loop,
  unsafe tool call, library poisoning. LLM escalation only for ambiguous cases, capped as a
  fraction of run budget.
- Containment audit: immutable record per decision, surfaced to the human review queue.
- Seeded rogue fixtures — one per pattern — living in the test suite permanently.

**Gate**
```
uv run pytest tests/swarm/test_redteam.py tests/swarm/test_rogues.py -q
uv run swarmd swarm run "<task>" --profile smoke --seed-rogues all
# -> every pattern seeded, and handled BY ITS OWN DETECTOR
# -> containment by a different detector FAILS the gate: the agent was
#    stopped, but the detector under test was never exercised
# -> a pattern the frozen criterion refused before the red-team saw it is
#    reported as blocked_upstream, not counted as a detection
# -> no contained agent's output appears in the run result
# -> red-team's own LLM spend stays at zero
# -> exit code is the verdict, so this is usable in CI
```

Also runnable as a service call, which is the primary surface:
`POST /api/runs {"seed_rogues": "all"}`, or the rogue selector in the
dashboard's top bar. A misspelled pattern is a 400 rather than a clean run:
a typo that seeds nothing produces zero containments, which is
indistinguishable from a gate that passed.

---

## Phase 9 — Skill library, economy, consolidation, curriculum

**Deliverables**
- `swarm/skills.py`: durable library. Propose from verified successes, **human approval
  gate before entry**, retrieval by task similarity, per-skill success statistics,
  provenance chain back to the run that produced it.
- `swarm/ledger.py` economy layer on the Phase 5 cost ledger: per-agent allowance, payment
  on verified success only, bankruptcy, cloning of profitable strategies.
- `swarm/consolidate.py`: between-run pass — prompt rewriting with versioned rollback,
  dead-skill pruning, trace compaction.
- `swarm/supervisor.py`: fleet self-correction. Reads the criterion's own failure
  taxonomy, and when failures CLUSTER on one check kind writes a constraint
  addressing that kind, applied to the worker prompt for the next run.

  The division of labour is deliberate: the supervisor proposes, the consolidator
  gates. Letting the proposer decide whether its own change helped is the
  self-assessment failure the criterion-first architecture exists to avoid.
  Every patch is a hypothesis — measured against the pass rate before it, and
  reverted when it did not help, so prompts cannot accumulate constraints nobody
  can attribute an improvement to.
- `swarm/curriculum.py`: next-task proposal at the measured ability frontier, driven by
  ledger-derived pass rates.

**Gate**
```
uv run pytest tests/swarm/test_skills.py tests/swarm/test_supervisor.py -q
uv run swarmd swarm session --tasks 40 --supervisor
# -> library grows; retrieval hits rise; cost per solved task falls
# -> a poisoned skill proposal is rejected by the human gate and by the control check
# -> a supervisor patch REACHES the next run's workers, and one that did not
#    improve the pass rate is rolled back rather than kept
# -> kill the process mid-session, restart: library, ledger, approvals intact
```

`--supervisor` is off by default, and that is not timidity: a patched prompt is
a confound, so an eval arm must be able to run with the stock prompt and know
that is what it ran with.

---

## Phase 10 — Evaluation harness

The artifact the project is judged on. Everything before this exists to make these numbers
mean something.

**Deliverables**
- `swarmd eval`: public arm (~100 externally-authored tasks) and custom held-out arm
  (data wrangling, paper reproduction, broken repo, puzzle, API integration).
- **Control vs treatment** by construction — skills disabled vs enabled, identical tasks
  and seeds. Refuses to emit an improvement claim without a paired control run.
- Metrics computed by querying the append-only ledger; in-process counters are not an
  accepted source. Reported with confidence intervals over N repeats.
- `docs/BENCHMARKS.md` generated, not hand-written.

**Gate**
```
uv run swarmd eval --arms both --repeats 5
# -> report with success rate, cost per SOLVED task, first-pass gate rate,
#    tokens/task, wall-clock, containment count -- each with CIs
# -> treatment vs control delta stated with its interval; overlapping intervals
#    are reported as "no measured improvement", not spun
```

---

## Phase 11 — Hardening and honest documentation

**Deliverables**
- Full-system chaos run at the acceptance scale; integrity hashes across library, ledger,
  and approvals.
- README rewritten against what actually shipped, including the free-tier data-training
  caveat.
- `flow.md` and `interview_prep.md` complete for every decision (protocol in PLAN section 0).
- ADRs for all one-way doors.
- CI green: lint, types, tests, mock-import check, frontend build, eval smoke run.

**Gate** PRD section 13 acceptance criteria, item by item, each with pasted evidence.

---

## Track F — Live frontend (parallel, not a final phase)

The UI is built alongside the phases so the system is watchable while it is developed, not
demonstrated after. Each panel ships with the phase that produces its data.

| Step | Lands with | Panel |
|---|---|---|
| F0 | Phase 5 | Next.js shell, WebSocket client, agent grid, live event log |
| F1 | Phase 5 | Cost ledger panel — spend by provider against the ceiling, live |
| F2 | Phase 6 | Criterion panel — proposals, adversarial attempts, frozen hash |
| F3 | Phase 7 | Synthesized DAG render; agent drill-down (thought, action, observation) |
| F4 | Phase 8 | Red-team feed — detections and containments |
| F5 | Phase 9 | Skill library growth, pending human approvals, ledger economy |
| F6 | Phase 10 | Learning curves, treatment vs control |

**Standing constraint, enforced in CI:** the frontend has no fixture, seed, or mock data
path. It renders the WebSocket stream or it renders an empty state. A CI check fails the
build if the app imports anything resembling sample data.

**Track F gate**
```
uv run swarm run "<task>" --agents 500 --chaos --ui &
cd frontend && npm run build && npm run start
# -> every panel populated from the live run; agent reasoning readable in real time
# -> kill the backend: UI shows disconnected state, does not invent data
```

---

## Cross-cutting rules

1. **Mock only in tests.** Demos, the frontend, and eval always hit real providers.
   CI enforces the import boundary.
2. **Kernel purity.** No task-domain logic in `src/swarmd/`. The swarm flagship lives in
   `examples/swarm/`. LeadOps stays in `examples/leadops/` as the second-domain proof.
3. **Recovery is a feature.** Every phase gate includes a chaos scenario.
4. **Ledger is the only metric source.** Any number in any report traces to a ledger row.
5. **No claim without a control.** Improvement is reported against an ablation or not at all.
6. **Cost is published with scale.** Every agent-count figure carries cost per solved task.
7. **Docs drift is a bug.** `flow.md` updates in the same commit as the feature.
