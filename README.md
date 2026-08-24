# swarmd

> A multi-agent orchestration runtime: staged pipelines of harnessed agents with
> checkpoint/resume recovery, quality gates, durable human-in-the-loop approvals,
> and full tracing — proven by a production-shaped LeadOps engine.

## Why

Frameworks make it easy to *draw* an agent graph. Operating one reliably is the hard part:
agents die mid-stage, bad output flows downstream without quality gates, human approvals
vanish on restart, and "recovery" means re-running everything. `swarmd` owns that layer —
scheduling, checkpointing, verification, approval states, and tracing — demonstrated by a
real application, not a toy.

## The flagship: LeadOps engine

A sales/leads operations pipeline over open data:

```
INGEST → ENRICH → DEDUPE → SCORE → DRAFT → QA → REVIEW QUEUE (human)
```

- Parallel agent pools per stage (10–50 concurrent agents), multiple outreach agents drafting simultaneously
- Every stage checkpointed — kill any agent mid-run and the pipeline resumes from last-good state
- Quality gates between stages; failing items repair/requeue instead of flowing downstream
- Outreach **never** auto-sends: review queue is a durable pipeline state that survives restarts
- A Supervisor deep-agent samples QA failures and hot-patches stage prompts fleet-wide

## The kernel (what you embed)

| Layer | Provides |
|---|---|
| **Pipeline** | stage DAG, dependency + concurrency control, per-stage pools & retry policies |
| **Kernel** | async scheduler, agent lifecycle, step-boundary checkpoints, heartbeat requeue |
| **Harnesses** | Fetch · LLM (router w/ cache+budgets) · Store · Verify · Draft |
| **Chaos harness** | random kills, latency injection, provider outages — first-class |
| **Observability** | OTel spans per transition/LLM call, Prometheus metrics, Grafana dashboards |

## Quickstart

```bash
uv sync
uv run leadops run examples/leadops/pipeline.py --chaos --kill-rate 0.2
# → runs the full pipeline on committed open-data fixtures (offline mock provider)
```

Observability stack:

```bash
docker compose up -d   # Jaeger · Prometheus · Grafana (dashboards in-repo)
```

## Docs

- [`docs/PLAN.md`](docs/PLAN.md) — execution roadmap, build order, documentation protocol
- [`docs/PRD.md`](docs/PRD.md) — goals, non-goals, acceptance criteria
- [`docs/SPEC.md`](docs/SPEC.md) — phased spec with hard gates
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design + trade-offs
- [`docs/adr/`](docs/adr/) — decision records
- [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) — methodology + results (fills as phases land)
- [`docs/flow.md`](docs/flow.md) — living progress log (decision + anatomy blocks)
- [`docs/interview_prep.md`](docs/interview_prep.md) — growing interview Q&A

## Status

Pre-code. Specs complete; implementation starts at Phase 1 of SPEC.md.
