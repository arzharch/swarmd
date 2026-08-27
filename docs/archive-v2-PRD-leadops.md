# PRD — swarmd

**Status:** v2.0 · **Owner:** Arsh Zakee Chowhan · **Last updated:** 2026-08-24

## 1. Problem statement

Multi-agent systems fail at orchestration, not at generation: pipelines stall when one
agent dies mid-stage, quality gates are absent so bad output flows downstream, humans
are bolted on as afterthoughts instead of first-class pipeline states, and recovery
means "run everything again." Frameworks make it easy to *draw* an agent graph; nothing
makes it easy to *operate* one reliably through failure, quality, and approval loops.

## 2. Vision

`swarmd` is a multi-agent orchestration runtime: define a pipeline of stages, each backed
by a pool of harnessed agents running in parallel; the kernel schedules them, checkpoints
every step, recovers dead agents from last-good state, enforces quality gates between
stages, pauses for human approval where configured, and traces every decision. Proven by
a production-shaped flagship application: **LeadOps** — a sales/leads operations engine.

## 3. Design stance: multi-agent, not mega-swarm

Deliberately **10–50 concurrent agents** (pools per stage, sized dynamically), not 1000+.
Rationale: the hard, demonstrable problems are coordination, quality, interactivity, and
recovery — not headcount. Concurrency is still real and measured (parallel agents within
and across stages), but orchestration quality is the product.

## 4. Target users

1. **Engineers building agentic pipelines** who need reliability, quality gates, and HITL
   beyond a notebook script
2. **Teams deploying outreach/enrichment/backlog pipelines** where a bad send costs money
3. **Framework authors** wanting a runtime layer beneath their abstractions

## 5. Goals

| # | Goal | Success metric |
|---|---|---|
| G1 | Orchestration correctness | Stage DAG executes with dependency + concurrency control; no stage starts before inputs ready |
| G2 | Recovery from failed state | Kill any agent mid-stage → pipeline resumes from last checkpoint; completed work never redone; zero lost leads in flagship run |
| G3 | Quality gates | Every stage has a verifier; failing items route to repair/requeue, not downstream; QA pass-rate reported per run |
| G4 | Human-in-the-loop | Approval is a pipeline state (`AWAITING_APPROVAL`), resumable across restarts; approve/reject via API/CLI |
| G5 | Parallel throughput | Multiple outreach/draft agents run concurrently with per-source rate limits; measured speedup vs serial documented |
| G6 | Total traceability | One trace spans lead → stage → agent → LLM call; every state transition queryable |

## 6. Non-goals (v1)

- Not an agent framework/DSL — pipelines are Python; agents are coroutines with harnesses
- Not auto-sending emails — outreach always terminates in a human review queue
- Not multi-node distributed execution (single process; interfaces leave room)
- No 1000-agent scale claims — concurrency target is tens, honestly benchmarked

## 7. Flagship application: LeadOps engine

A sales operations pipeline over open data (public directories, GitHub org data, open
startup lists — no client data):

```
INGEST → ENRICH → DEDUPE → SCORE → DRAFT → QA → REVIEW QUEUE (human)
```

- **ENRICH** (pool ×N): fetch public signals (site, GitHub, news), normalize company/person
  fields, extract tech-stack signals from messy text
- **DEDUPE**: embedding candidates + LLM confirmation merges duplicates across 10k+ leads
- **SCORE**: ICP-fit rubric (structured output, reasons) + verifier re-check
- **DRAFT** (multiple outreach agents concurrently): personalized first-touch emails,
  per-domain rate limits, template+persona config
- **QA**: schema validation, hallucination spot-checks, tone/compliance pass
- **REVIEW QUEUE**: human approves/rejects sends; decision recorded in audit trail

Deep-agent layer: a **Supervisor** samples QA failures, diagnoses systematic prompt
issues, patches stage prompts, hot-reloads running workers — fleet self-correction.

## 8. Functional requirements

### FR-1 Pipeline & stages
- Stages declare inputs/outputs, pool size, retry policy, verifier, approval requirement
- DAG execution with dependency ordering; independent stages run concurrently

### FR-2 Harnesses
- `FetchHarness` (allowlist, robots-aware, rate-limited HTTP), `LLMHarness` (router:
  fallbacks, semantic cache, budgets), `StoreHarness` (Postgres persistence),
  `VerifyHarness` (validators + resampling), `DraftHarness` (templates/personas)
- Harness = toolset + system prompt + loop policy; business logic lives here, kernel stays pure

### FR-3 Agent lifecycle & recovery
- States: SPAWNED → RUNNING → PARKED → DONE/FAILED/KILLED
- Checkpoint at every step boundary; heartbeat expiry requeues claimed work with
  checkpoint intact; resumed agents skip completed steps deterministically

### FR-4 Quality gates
- Verifier per stage; failures route to bounded repair loop → requeue → dead-letter
- Per-run quality report: pass rates, failure taxonomy, supervisor interventions

### FR-5 Human-in-the-loop
- `AWAITING_APPROVAL` is a durable pipeline state surviving restarts
- Approve/reject/edit via CLI/API; all decisions audited

### FR-6 Observability
- OTel spans per state transition, LLM call, tool call; Prometheus metrics;
  committed Grafana dashboards

### FR-7 Benchmark suite
- `swarmd bench`: stage throughput, parallel-vs-serial speedup, recovery time post-kill,
  memory curve, end-to-end lead integrity (zero lost/duplicated leads under chaos)

## 9. Acceptance criteria (v0.1)

1. All SPEC phase gates pass
2. Flagship demo: 5k open-data leads through full pipeline; chaos kills agents in every
   stage; final DB contains exactly the expected lead set (integrity hash match); QA
   report + supervisor intervention log produced
3. Kill-and-resume proven per stage; approval flow survives full process restart
4. Parallel draft stage shows ≥4× speedup vs serial on benchmark workload
5. README quickstart runs offline-first (mock provider default); published to PyPI

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| "Multi-agent" reads as generic | Differentiator is operability: recovery, quality gates, HITL durability — each individually demoable |
| Open-data sources rot / block | Source adapters isolated; fixtures committed for offline runs; robots-aware fetching |
| LLM cost creep in flagship | Semantic cache + budgets in router; mock provider for CI; free-tier models supported |
| Scope creep toward framework | Non-goals enforced; PRD update required before new surface |
