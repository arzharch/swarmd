# SPEC — swarmd Technical Specification

**Status:** v2.0 · Companion to [PRD.md](PRD.md) · **Last updated:** 2026-08-24

Rules: each phase ends with a **gate** (runnable command + observable outcome). No
phase-N work before the phase-(N-1) gate passes. Deviations need an ADR. Docs drift =
bug: flow.md updates in the same commit as any feature.

Stack: Python 3.12+, asyncio (stdlib core), httpx, asyncpg, OpenTelemetry,
prometheus-client, pytest + pytest-asyncio, uv. Mock LLM provider default; OpenRouter
adapter for real runs.

---

## Phase 1 — Kernel & agent lifecycle (weeks 1–3)

**Deliverables**
- `Task` (payload, priority, deadline, retries, checkpoint slot), `AgentHandle`
  (SPAWNED/RUNNING/PARKED/DONE/FAILED/KILLED)
- `Scheduler`: priority + bounded queues (backpressure), concurrency caps
- Checkpoint contract: agents persist state at step boundaries; resume skips completed
  steps deterministically; heartbeat expiry requeues claimed work with checkpoint intact
- Chaos hook v1: probabilistic kill injection
- Event bus: lifecycle events for tests/observability

**Gate ✅**
```
uv run pytest tests/kernel -q          # ordering, backpressure, kill-and-resume determinism
uv run swarmd demo kernel --kill-rate 0.3   # output hash == zero-kill run, printed
```

---

## Phase 2 — Pipeline & harnesses (weeks 4–6)

**Deliverables**
- `Pipeline`: stage DAG — inputs/outputs, pool size per stage, retry policy, dependency
  ordering; independent stages run concurrently
- Harness base + implementations:
  - `LLMHarness`: provider interface + mock provider (deterministic); router skeleton
    (fallback chain, per-run budget)
  - `FetchHarness`: robots-aware, allowlisted, rate-limited HTTP
  - `StoreHarness`: Postgres persistence via asyncpg (upsert/query)
  - `VerifyHarness`: schema validators + resample-checks
  - `DraftHarness`: template/persona rendering
- Stage verifier wiring: failures → bounded repair loop → requeue → dead-letter

**Gate ✅**
```
uv run pytest tests/pipeline -q
# two-stage demo pipeline (mock enrich → verify) with injected failures:
#   failures repair/requeue, never leak downstream; report shows pass rates
```

---

## Phase 3 — Quality gates & HITL durability (week 7–8)

**Deliverables**
- Verifier protocol formalized (per-stage, composable checks)
- `AWAITING_APPROVAL` durable pipeline state: survives full process restart;
  approve/reject/edit via CLI (`swarmd approve|reject|list`)
- Audit trail of all human decisions
- Run quality report: pass rates, failure taxonomy

**Gate ✅**
```
# start pipeline → reaches REVIEW QUEUE → kill process → restart →
# state intact; approve via CLI → pipeline completes; decision audited
```

---

## Phase 4 — Model routing & cost control (weeks 9–10)

**Deliverables**
- Provider pool: health scoring from latency/error signals; fallback chains (<2s failover)
- Semantic cache: embedding similarity threshold, TTL, LRU; hit-rate metrics
- Token budgets per run/stage; budget breach aborts cleanly with report
- OpenRouter adapter (free-tier models) behind the same provider interface

**Gate ✅**
```
uv run pytest tests/router -q
# repeated benchmark workload → ≥60% cache hit rate reported;
# forced provider failure fails over transparently
```

---

## Phase 5 — LeadOps flagship (weeks 11–13) ⭐

**Deliverables**
- `examples/leadops/`: INGEST → ENRICH → DEDUPE → SCORE → DRAFT → QA → REVIEW QUEUE
  over committed open-data fixtures (offline-first; real fetch adapters optional)
- Parallel pools: ENRICH ×N, DRAFT ×M outreach agents concurrently (per-domain limits)
- Supervisor deep-agent: samples QA failures, patches stage prompts, hot-reloads workers
- Chaos integration across every stage; lead-integrity checker (no lost/duplicated leads)

**Gate ✅**
```
uv run leadops run examples/leadops/pipeline.py --chaos --kill-rate 0.2
# completes; integrity hash matches clean run; QA report + supervisor log produced
```

---

## Phase 6 — Observability & benchmarks (weeks 14–15)

**Deliverables**
- OTel spans: stage transitions, checkpoints, LLM calls, approvals; Jaeger via docker-compose
- Prometheus metrics + committed Grafana dashboards (agents alive, queue depth, cache hit %,
  kills/min, approval wait time)
- `swarmd bench`: parallel-vs-serial speedup, recovery time post-kill, memory curve,
  integrity checks; results written to BENCHMARKS.md format

**Gate ✅**
```
docker compose up -d && uv run leadops run ... --chaos --otel
# trace chain visible in Jaeger end-to-end; dashboards live during run;
# bench shows ≥4× draft-stage speedup vs serial; PRD §9 criteria all green
```

---

## Phase 7 — Packaging & release (week 16)

**Deliverables**
- PyPI package (`swarmd`) + console scripts; clean-machine quickstart verified
- README with architecture diagram + benchmark table; recorded demo video
- Offline-first guarantee: everything works with mock provider, zero API keys

**Gate ✅** PRD acceptance criteria §9 all checked; v0.1 tagged and published.

---

## Cross-cutting rules

1. **Offline-first.** Mock provider + fixtures make every test and demo free and deterministic.
2. **Kernel purity.** No business logic in `src/swarmd/`; LeadOps lives in `examples/`.
3. **Recovery is a feature, not a test detail.** Every phase's gate includes a chaos scenario.
4. **Docs drift = bug.** flow.md updated in the same commit as any feature.
