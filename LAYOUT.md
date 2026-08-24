# Planned module layout (Phase 1+). Create each file when its phase starts.

src/swarmd/
├── __init__.py
├── cli.py               # demo kernel / approve|reject|list / bench subcommands
├── task.py              # Task, TaskResult, Checkpoint models
├── agent.py             # AgentHandle, lifecycle states, checkpoint contract
├── scheduler.py         # priority + bounded queues, backpressure, concurrency caps
├── runtime.py           # worker pool, heartbeat expiry, requeue-with-checkpoint
├── chaos.py             # kill injection, latency injection, provider outage sim
├── events.py            # event bus (lifecycle events for tests/observability)
├── pipeline/
│   ├── dag.py           # stage DAG: dependencies, concurrency control
│   ├── stage.py         # Stage: pool size, retry policy, verifier wiring
│   └── gates.py         # verifier protocol, repair loop, dead-letter
├── harnesses/
│   ├── base.py          # Harness = toolset + system prompt + loop policy
│   ├── llm.py           # LLMHarness (provider interface)
│   ├── fetch.py         # robots-aware allowlisted HTTP
│   ├── store.py         # Postgres persistence (asyncpg)
│   ├── verify.py        # validators + resample checks
│   └── draft.py         # template/persona rendering
├── router/
│   ├── providers.py     # Provider interface + deterministic mock + OpenRouter adapter
│   ├── health.py        # latency/error scoring, fallback chains
│   └── cache.py         # semantic cache: similarity threshold, TTL, LRU
├── hitl/
│   └── approvals.py     # durable AWAITING_APPROVAL state, CLI actions, audit trail
└── observability/
    ├── tracing.py       # OTel spans per transition/LLM call
    └── metrics.py       # Prometheus collectors

examples/leadops/        # flagship app — business logic lives HERE, not in src/
├── pipeline.py          # INGEST→ENRICH→DEDUPE→SCORE→DRAFT→QA→REVIEW definition
├── agents/              # enrich/dedupe/score/draft/qa/supervisor agent impls
├── sources/             # open-data adapters + committed fixtures (offline-first)
└── integrity.py         # lead-integrity checker (hash vs clean run)

tests/
├── kernel/              # ordering, backpressure, kill-and-resume determinism
├── pipeline/            # gate behavior, repair loops, HITL durability
├── router/              # failover, cache hit rates
└── fixtures/            # open-data lead fixtures, mock provider transcripts
