# swarmd

> A runtime for generic agents thrown at tasks nobody scoped for them. The
> swarm agrees on how success will be measured *before* it starts, generates
> its own plan, executes it across a worker pool under a hard cost ceiling, and
> reports whether it improved against its own ablation.

## The problem

Agent systems get demonstrated on tasks their authors already knew how to
solve. The pipeline is hand-drawn, the success criterion is hand-written, and
"the agents learned" is asserted against no control. Throw the same system at
something nobody scoped for it and it has no way to decide what *done* means,
no way to decompose the work, no memory that the last hundred tasks happened,
and no way to tell a productive agent from one burning budget in a loop.

Meanwhile the scale demos measure headcount. Nobody publishes cost per solved
task, and nobody runs the ablation that would show whether the swarm improved
or the tasks got easier.

## The loop

```
unknown task in
  │
CRITERIA     N agents independently author a machine-checkable criterion
             a red-team tries to satisfy it with garbage
             garbage passes → rejected, re-authored
             → FROZEN, content-addressed, immutable for the run
  │
PLAN         competing DAG decompositions proposed
             structurally validated → handed to the existing executor
  │
RETRIEVE     skill library queried (cold start: empty, and the run shows it)
             a skill is retrievable only after two DIFFERENT task shapes
  │
EXECUTE      one generic worker type, chaos killing them throughout
             each spending from a budget, paid on VERIFIED success only
  │
GATE         the frozen criterion decides · bounded repair · dead-letter
  │
DISTILL      verified successes → candidate skills → A HUMAN APPROVES
  │
CONSOLIDATE  prompts rewritten with rollback, dead skills pruned
  │
CURRICULUM   next task proposed at the measured ability frontier
```

Running throughout, not as a stage: a **red-team organ** tailing the live action
log with authority to contain agents.

## Five things that are actually unusual here

**The criterion is frozen before any solving happens.** Not written by a
developer, not judged by a model at the end. N agents author it, a red-team
tries to pass it with empty, constant, and prompt-echoing output, and it is
rejected if garbage gets through. Some tasks fail at stage zero without a single
solve attempt, and that rate is a reported metric. ([ADR-009](docs/adr/ADR-009.md))

**No improvement claim without a control arm.** `swarmd eval` refuses to emit an
improvement figure without a paired run — identical tasks, identical seeds,
skills disabled. When the confidence intervals overlap it prints *"no measured
improvement"*, in those words. ([ADR-007](docs/adr/ADR-006.md))

**Every reported number is a sum over an append-only ledger.** No component
keeps a running total. Agents are selected on reported success and paid on
verified success, so anything an agent can write, selection pressure eventually
teaches it to write dishonestly — removing the capability is cheaper than
policing it.

**Provider quota is a cluster resource.** Limits are per *account*, not per
process, so three pods each politely limiting to 45 RPM present 135 to the
account. Quota moves to Redis, evaluated as one atomic script against the Redis
clock. ([ADR-011](docs/adr/ADR-011.md))

**Synthetic data cannot pretend to be real.** The simulated provider marks every
ledger row it produces; taint propagates row → report → dashboard banner, and
`refuse_simulated()` raises before anything publishes a number from it.
([ADR-012](docs/adr/ADR-012.md))

## Quickstart

```bash
uv sync --all-extras

# No API key required. Runs the entire loop on a synthetic provider whose
# output is marked simulated everywhere it surfaces.
SWARMD_SIMULATED_PROVIDER=true uv run swarmd swarm run \
  "extract the numeric claims from a short report and verify each one" \
  --profile smoke --chaos
```

```
* criterion_frozen 0eff8402816d7ce1
* plan_selected    ab7f6feca1038789
* node_finished    read
. a0001: calling_model
. a0001: criterion_passed
* agent_killed
* agent_requeued

run=run-8d321c794d  status=completed  0.5s
criterion=0eff8402816d7ce1 (2 checks, attempts=1)
plan=ab7f6feca1038789 (4 nodes, width=2)
nodes_passed=4/4  contained=0
integrity_hash=ba687b1e8e66c34a
cost=$0.000000 of $0.05 ceiling  calls=8  cache_hits=0  [SIMULATED]
redteam: contained=0 flagged=0 llm_calls=0
```

### Watch it live

```bash
SWARMD_SIMULATED_PROVIDER=true uv run swarmd serve --port 8000 &
cd frontend && npm install && npm run dev     # http://localhost:3001
```

The dashboard renders the websocket stream or an empty state. There is no
fixture path anywhere in it, and CI fails the build if one appears.

Everything the CLI does, it does: start a run, watch the agents work, read the
artifacts it produced, run an eval or a learning session, probe providers and
read the day's remaining budget, approve a skill, resume a run parked on a
spent ration.

Two things to know when running it locally:

* **If `SWARMD_API_TOKEN` is set, the dashboard asks for it.** Reads are
  ungated, so the page fills with real data either way; the token is what lets
  you act. Paste it into the field in the top bar -- it is kept in that
  browser and sent as `X-Swarmd-Token`. This is the operator credential, not a
  login: swarmd has one principal and no accounts (ADR-013).
* **If the control plane is not on port 8000, tell the dashboard.**
  `SWARMD_API=http://127.0.0.1:8123 npm run dev`. Next.js bakes the rewrite in
  at build time, which is why the destination is an environment variable here
  and absent in deployment, where the Ingress serves both from one origin.
  Windows in particular reserves blocks of ports for Hyper-V, and 8000 is
  often inside one -- `netsh int ipv4 show excludedportrange protocol=tcp`
  lists them. The symptom is uvicorn logging `[Errno 13] ... [winerror 10013]`
  at bind and exiting.

### With real providers

Copy `.env.example` to `.env` and add at least one key. Groq (14,400 req/day)
and Google AI Studio (1,500 req/day) are free and need no card; Cerebras and
OpenRouter roughly double the daily headroom.

```bash
uv run swarmd providers probe   # discovers what capacity actually exists
uv run swarmd swarm run "<task>" --profile standard --chaos
uv run swarmd eval --arms both --repeats 5 --benchmarks docs/BENCHMARKS.md
```

**A note on free tiers:** they train on submitted prompts. Mistral's tier
requires explicitly consenting to that, which is why it sits behind
`--allow-data-training` and is off by default.

## Capacity is the design constraint

The bottleneck is not CPU or money — it is someone else's rate limit. Pooled
free tiers give roughly **45 requests/minute**, so a 15-minute run has ~675
call slots. Four levers get a 500-agent run inside that:

| Lever | Effect |
|---|---|
| The criterion is executable code, so verification costs nothing | −5,000 calls |
| Batched generation: one call returns K variants | 8× |
| Semantic cache (population search generates near-identical prompts) | ~2.5× |
| Red-team detectors are pure code, zero model calls | the safety tax |

| Profile | Calls | Wall clock | Use |
|---|---|---|---|
| `smoke` | ~60 | ~2 min | CI, every PR |
| `standard` | ~600 | **12–18 min** | the watchable run |
| `deep` | ~1,800 | ~40 min | enough curve points to mean something |
| `eval` | ~12,000 | ~4.5 hr | the sweep — a batch job, not interactive |

Full derivation, including the assumptions most likely to be wrong, in
[docs/CAPACITY.md](docs/CAPACITY.md).

## Operations

Kubernetes manifests with dev/prod overlays, Terraform for EKS/RDS/Secrets
Manager, Prometheus metrics on a private registry, two provisioned Grafana
dashboards, alert rules where every alert has a runbook entry, and SLOs that
promise what the system actually claims — correctness under chaos at 100% with
no error budget, and cost per run.

```bash
docker compose up -d          # Jaeger · Prometheus · Grafana · Postgres · Redis
kubectl apply -k deploy/k8s/overlays/dev
```

Chaos runs in production. Turning it off would make production the one
environment where the recovery guarantee is never tested.

**Access posture.** Single-tenant and operator-run: no user accounts, no roles,
because there is one principal. A shared operator token gates every mutating
endpoint and the event stream, and the service refuses to start bound off-host
without one. The dashboard sits behind an Ingress source allowlist and the
control plane is never given a public route. That is a decision with
compensating controls, not an omission — [ADR-013](docs/adr/ADR-013.md) and
[SECURITY.md](SECURITY.md).

**The uncomfortable number:** infrastructure costs ~$280/month against ~$0 of
LLM spend — about 5,600× more than inference. If the goal were minimising cost
this belongs on Fargate. It is on EKS because the goal is operating it, and
saying so is better than pretending the architecture is cost-optimal.

## Docs

| | |
|---|---|
| [PRD](docs/PRD.md) | goals, non-goals, acceptance criteria |
| [SPEC](docs/SPEC.md) | phases with hard gates |
| [PLAN](docs/PLAN.md) | build order and the documentation protocol |
| [CAPACITY](docs/CAPACITY.md) | why the run profiles are what they are |
| [SLO](docs/SLO.md) | what we promise, and what we deliberately do not |
| [RUNBOOK](docs/RUNBOOK.md) | one entry per alert |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | AWS architecture, rejected alternatives, Azure mapping |
| [SECURITY](SECURITY.md) | threat model, sandbox limits, data retention, known gaps |
| [STATUS](docs/STATUS.md) | **what is done and what is not, per requirement** |
| [PRR](docs/PRR.md) | production readiness review, honestly filled in |
| [ADRs](docs/adr/) | the one-way doors, including two reversals |
| [flow.md](docs/flow.md) | decision log with alternatives and follow-up questions |
| [interview_prep.md](docs/interview_prep.md) | the questions this invites, answered |

## Status

The loop runs end to end with no API key. 807 tests, ruff and mypy clean,
kernel chaos gate passing at kill-rate 0.9 with matching integrity hashes, and
the red-team gate passing with five deliberate rogues injected into a real run.

Checkpoint recovery holds in both halves: the kernel at kill-rate 0.9 with
byte-identical output, and the swarm flagship, where a killed agent's
replacement resumes from its checkpoint. That second claim is tested by
counting provider calls rather than by comparing output, because identical
output only proves the work was deterministic — not that it was recovered
instead of repeated.

Measured against real providers: runs end to end at $0.00 on free tiers, both
held-out tasks taken from prompt to graded artifact with no code change, and
training sessions that fill a skill library.

**Not claimed: that the system improves.** For most of this project's life that
was pending quota. It was not. A skill becomes reviewable only once it has
worked on two *distinct* task shapes — the bar that makes "does this transfer?"
answerable — and a suite of twelve tasks with twelve disjoint output shapes can
never supply the second one, at any sample size. Nothing was ever queued, so
nothing was approved, so the treatment arm of every ablation retrieved from an
empty library and reported *"no measured improvement"* for a reason that had
nothing to do with skills. That is fixed — there is a training arm of task
families, and two phrasings of one approach are now one record — and the first
skills to clear the bar on their own evidence exist. Whether retrieving them
helps is a question this repo can now ask, and has not yet answered.
([ADR-014](docs/adr/ADR-014.md))

A `standard`-profile chaos run has now gone through on live providers: 25 of 25
nodes, 34 agents with 9 killed mid-run and resumed from their checkpoints
without re-buying the work, integrity hash intact, $0.00. `deep` and the
500-agent claim still have not.

Two numbers remain unmeasured and worth naming, because both could come back
worse than hoped: the run memo's hit rate on genuinely novel tasks (exact
keying means novel work hits near zero -- distinct from the provider's prompt
prefix cache, which read 13.3% on that run), and whether a real model asked for
eight distinct approaches returns eight rather than three and five rewordings.
[STATUS.md](docs/STATUS.md) tracks it as the one remaining gap.

Second domain: [`examples/leadops/`](examples/leadops/) is a sales pipeline on
the same runtime, retained and green. The `src/` tree did not change by a line
when the flagship pivoted, which is the evidence for the kernel-purity claim.
