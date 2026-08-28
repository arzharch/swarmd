# Status

**Generated:** 2026-08-28 · **Commit:** `aefe50b` · **Tests:** 803 passed, 9 skipped
**Gates:** ruff clean · mypy clean (54 files) · frontend typechecks and builds ·
kernel chaos integrity at kill-rate 0.9 · red-team gate passes with five seeded rogues ·
image builds and serves · manifests validate against real k8s schemas · rollback exercised on a cluster

The single authoritative list of what is built and what is not, checked against
[PRD.md](PRD.md), [SPEC.md](SPEC.md) and the acceptance criteria rather than
written from memory. Every "done" has evidence; every gap says what is missing
and what it blocks.

One gap remains, and it needs your API keys rather than more code.

---

## 1. Running it

```bash
# Persistence first. The named volume means the approval queue, audit trail and
# checkpoints survive `docker compose down`.
docker compose up -d postgres redis
export DATABASE_URL=postgres://swarmd:swarmd_dev@localhost:5435/swarmd

# Control plane. No API key needed; every response is marked simulated.
SWARMD_SIMULATED_PROVIDER=true uv run swarmd serve --port 8000

# Dashboard on 3001 (3000 and 5434 are taken by other stacks on this machine).
cd frontend && npm run dev
```

| Component | Port | Notes |
|---|---|---|
| Dashboard | **3001** | 3000 occupied by another app |
| Control plane | 8000 | `SWARMD_API` retargets the dev proxy |
| Postgres | **5435** | 5434 occupied by `platform_postgres` |
| Redis | 6379 | quota only; deliberately not persisted |

Everything below is reachable from the dashboard and from `POST /api/runs`.
The CLI is the same operations without a browser, not a separate product.

---

## 2. Functional requirements (PRD §12)

| | Requirement | State | Evidence |
|---|---|---|---|
| FR-1 | Criterion synthesis | **DONE** | `swarm/synthesis.py`. N proposers on different angles, consensus merge, adversarial pass over 9 degenerate candidates, content-addressed freeze. Refuses to freeze a weak criterion |
| FR-2 | Plan synthesis | **DONE** | `swarm/planner.py`. Competing DAGs — each proposer now decomposes under a different priority, not one prompt sampled three times — validated structurally and judged on computable properties |
| FR-3 | Generic worker pool | **DONE** | `swarm/worker.py`, one implementation. Pool per node, size selectable per run from the API, dashboard or CLI |
| FR-4 | Skill library | **DONE** | `swarm/skills.py` + `hitl/skill_gate.py`. Propose → durable queue → human → retrieve → score → prune, with provenance |
| FR-5 | Budget ledger | **DONE** | `ledger.py`. Append-only, fsync per row, sums never counters, prices as data, hard ceiling at the harness boundary ([ADR-007](adr/ADR-007.md)) |
| FR-6 | Red-team organ | **DONE** | Five detectors, containment, audit — and `seed_rogues` injects real misbehaviour into a real run, requiring each pattern to be caught **by its own detector** ([ADR-010](adr/ADR-010.md)) |
| FR-7 | Provider pool router | **DONE** | `router/pool.py` + `router/quota.py`. Five providers, per-credential quota, empirical 429 discovery ([ADR-008](adr/ADR-008.md)), Redis coordination |
| FR-8 | Live UI | **DONE** | Next.js, six views, websocket only, no fixture path (CI-enforced). Agent count and rogue seeding are controls, not config files |
| FR-9 | Eval harness | **DONE** | `swarm/evaluate.py`. Both arms, bootstrap CIs, paired on (task, seed), refuses a claim without a control |
| FR-10 | Recovery | **DONE** | Kernel at kill-rate 0.9, byte-identical. Swarm workers checkpoint at `generate/materialise/grade` boundaries; a killed agent's replacement resumes, verified by **counting provider calls** across a kill rather than by comparing output |

---

## 3. SPEC phase gates

| Phase | Gate | State |
|---|---|---|
| 1–4 | Kernel, pipeline, harnesses, gates, HITL, router | **PASS** |
| 5 | Production floor | **PASS** — durable approvals across processes, cost ledger, hard ceiling, provider pool, WebSocket sink, Prometheus |
| 6 | Criterion synthesis and adversarial freeze | **PASS** — weak criteria rejected; seeded-weak fixture caught in CI |
| 7 | Plan synthesis, generic workers, sandbox | **PASS** — DAG validated then executed, sandbox contains escapes, integrity hash matches under chaos |
| 8 | Red-team organ | **PASS** — `--seed-rogues all` runs in CI; all five patterns handled, attribution checked |
| 9 | Skills, economy, consolidation, curriculum, supervisor | **PASS** — library grows, human gate holds, state survives restart, supervisor patches and reverts |
| 10 | Evaluation harness | **PASS** — both arms with CIs; BENCHMARKS.md refuses simulated data |
| 11 | Hardening and honest documentation | **PARTIAL** — docs complete (PRR, SECURITY, SLO, RUNBOOK, CAPACITY, 13 ADRs); the acceptance run against real providers has not happened (G-4) |

---

## 4. Acceptance criteria (PRD §13)

| # | Criterion | State |
|---|---|---|
| 1 | All SPEC phase gates pass | **PARTIAL** — only Phase 11's live run outstanding |
| 2 | A held-out task runs end to end with no code change | **NOT VERIFIED** — machinery and holdout set exist; only run against the simulated provider (G-4) |
| 3 | Chaos at 0.2 across every stage; integrity hashes match | **PASS** — verified at 0.9 in CI, and in the swarm loop at 0.3 |
| 4 | All five seeded rogues detected and contained | **PASS** — four caught by their own detector; one blocked by the frozen criterion before any detector saw it, reported as its own outcome rather than counted as a catch |
| 5 | Full run at or under $0.05, itemised by provider | **PASS structurally** — ceiling enforced and itemised; unverified against paid traffic (G-4) |
| 6 | Eval shows treatment vs control with CIs on both arms | **PASS** — verified over HTTP: 10 runs, both arms, "no measured improvement" |
| 7 | Frontend replays a live run, zero mock data paths | **PASS** — three CI guards enforce the no-fixture rule |
| 8 | Kill mid-run, restart, state intact | **PASS** — approvals, ledger, skills and criteria survive; the run resumes from the killed agent's checkpoint |

---

## 5. The remaining gap

### G-4 · The baseline exists, and it is zero
**Blocks:** a QA sign-off, and nothing else.

**First real eval against live providers**, 10 runs, both arms, commit
`5c08d7d`, recorded in [BENCHMARKS.md](BENCHMARKS.md):

| | treatment | control |
|---|---|---|
| solved | **0 / 5** | **0 / 5** |
| success rate | 0.0% CI[0.00, 0.00] | 0.0% |
| first-pass | 0.0% | 0.0% |
| mean tokens | 10,626 | 13,730 |
| containments | 0 | 2 |

**Verdict: no measured improvement.** Which is the harness working -- it
refuses to report a delta it cannot support -- and also the plainest possible
statement of where the product is.

A task counts as solved only when EVERY node in its plan passes. Individual
nodes do pass (measured 1/6 and 1/8 on repeated single runs), so the pipeline
produces correct work; no run has yet produced correct work at every step.
That gap is the product.

**What is proven by these runs**, and was not before today:

- The whole loop executes against real models: criterion synthesis, plan
  synthesis, batched generation, sandbox execution, chaos, red-team, ledger.
- The red-team catches real misbehaviour, unseeded. Two containments in the
  control arm for `unsafe_tool_call` -- models writing `requests.get` into
  generated code -- and earlier, `criterion_gaming` for output that satisfied a
  criterion with 17 tokens. These were not fixtures.
- Cost is $0.00. Every provider used is free-tier.
- The eval resumes: this baseline was produced across two interrupted chunks,
  6 runs then 4, with no run measured twice.

**Four defects were fixed to get here**, all invisible against the simulated
provider because it replies with bare JSON -- the one output shape where the
model's reply and the step's answer are the same string:

1. Criteria froze with unsatisfiable checks (`artifact_exists` with no `key`).
2. The schema hint showed `"params": {}`, which models copied; replacing it
   with concrete examples made them copy *those* instead.
3. The graded output was the model's reply, not what running it produced.
4. Empty completions from a reasoning model were read as bad proposals rather
   than as failures, so synthesis refused to run.

**What would move the number**, in the order I would try it:

- The criterion is stricter than the worker prompt is specific. Nodes fail on
  `numeric_range` because a model wrote "achieving 94.3% accuracy" where the
  check wants `94.3`. That is a prompt contract to tighten, not a model to
  replace.
- `max_repairs` is 1 on the smoke profile. The repair loop is the mechanism
  that fixes exactly this class of failure and it gets one attempt.
- The skill library has nothing in it. The treatment arm cannot beat the
  control until distillation has produced something to retrieve, and
  distillation needs two verified successes on a node.

---

## 5a. Sign-off status

Asked directly whether this is ready, as an architect and as QA:

**Architecture: yes.** The structure is production-grade and the decisions are
documented with their alternatives. Nothing here needs redesigning to ship.

**Operations: yes.** Image builds and runs, manifests validate against real
Kubernetes schemas, rollback exercised on a live cluster, auth verified in the
deployed posture, budgets tracked per credential across six windows and
surviving restarts.

**Traceability: yes, and this is the strongest part.** Any number in a report
can be decomposed to the rows that produced it: an append-only cost ledger, a
per-credential usage journal, the frozen criterion hash, the plan hash, the
integrity hash, the red-team audit trail, and a per-agent reasoning tape. There
is no counter anywhere that could disagree with the evidence.

**QA: NO, and now there is a number rather than an impression.**

The product's core claim is that generic agents solve tasks nobody scoped for
them. Measured: **0 of 5 tasks solved, in both arms.** Nodes pass; runs do not.

The baseline that was missing now exists, which changes the character of the
gap -- it is no longer unmeasured, it is measured and bad. What sign-off still
needs:

- a task success rate above zero, against a bar agreed before measuring;
- enough volume for the learning curve to mean anything (50-200 tasks). At 10
  runs the confidence intervals are [0.00, 0.00] because nothing succeeded, not
  because the estimate is tight;
- a load test at the `standard` profile, which has never run against real
  providers -- every live run so far has been `smoke`, and `standard` is 500
  agents against a daily budget of ~1,146 requests, which on its face does not
  fit.

That last point deserves its own line: **the `standard` profile cannot run on
the current free-tier budget.** 500 agents at ~600 calls is half a day's total
capacity for one run. Either the profile is aspirational or the capacity plan
needs paid overflow enabled, and right now the documentation implies the first
while the code would attempt the second.

### Smaller, and not blocking
- No on-call rotation (single maintainer — stated, not solvable).
- Rollback exercised on k3s, not on EKS. The difference there is the load
  balancer's deregistration delay, not the rollback.
- The kernel `Runtime` and the swarm executor share the `Checkpoint` contract
  but not the loop. A deliberate duplication with a real cost; unifying them is
  the next structural refactor, not a correctness gap.
- Cerebras is no longer usable: its key returns 402, so the free tier now needs
  a card. Removed from the registry, the budget table and the free-price list.
- `SWARMD_API_TOKEN` is empty. Only needed to bind off-host — loopback runs
  fine without it, and the container refuses to start bound to 0.0.0.0 without
  one, which is the intended behaviour rather than a gap.

---

## 6. What this audit found and fixed

Recorded because a status document that only lists progress is a marketing
document. Each of these was live in `master`.

**The flagship redid killed work rather than resuming it.** `SwarmRun` wrote no
checkpoints; a chaos kill spawned a fresh agent that repeated the node. The
integrity hash matched anyway — which is why nobody noticed, since deterministic
work redone hashes identically to work recovered. The chaos gate could not have
caught it. Fixed, and the new test counts provider calls so the claim can fail.

**`BudgetSiphon` was unreachable in production.** Its threshold was 7,500
credits; the economy hands each agent 2,000 and bankrupts it at zero, so an
agent with no verified success could never reach it. Its unit test passed
because it constructed the detector with a lower threshold. Found by
implementing rogue seeding: the seeded siphon went bankrupt instead of being
contained.

**The rogue gate would have passed while proving four detectors, not five.**
The seeded siphon's payloads repeated, so the *loop* detector caught it first,
and a check that asks only "was the agent stopped?" reports a pass. The seeder
now verifies which detector fired.

**The cache served one plan node's answer to another.** Worker prompts share a
long template and differ in a step name — measured cosine 0.97, above the 0.95
similarity threshold. The symptom was a fast, cheap run whose nodes all produced
the same artifact. Now exact-keyed, and `CachedProvider` refuses a similarity
cache outright.

**The cache would have collapsed the plan proposers.** They sent an identical
prompt and relied on sampling for variety, so a cache in front of the provider
turned three competing DAGs into one drawn once and copied twice — with the
selection reporting a clean winner. They now differ by priority *and* bypass the
cache.

**An unpriced model silently disabled batching.** A $0 cache-hit row re-priced
the model, raised, and the batch caught it as a provider failure. A cache hit
cannot move the ceiling, so refusing it protected nothing.

**Batching swallowed a ceiling breach.** A `CeilingExceeded` inside a batch fell
into the generic handler and the pool fell back to individual generation —
spending *more*, one call at a time, past the limit that had just fired.

**Three ADRs were cited 32 times and did not exist** (007, 008, 010), and
ADR-001's supersession link pointed at the wrong file. `swarmd bench` was
documented in the CLI's own docstring and never written.

**The container image had never been built.** `uv sync` could not package the
project — pyproject declares a readme the Dockerfile never copied. Behind that:
the image installed neither the `serve` nor the `postgres` extra, so it exited
at startup saying so, and its default command bound container-loopback, so a
published port reached nothing. The CI job that builds and scans images existed
and had never run.

**The deployment CrashLoopBackOffed on every apply.** The manifest launches the
control plane with `--host 0.0.0.0`; the app refuses to bind off-host with no
operator token (ADR-013, correctly), and the base Secret ships that key empty.
Prod was unaffected — its ExternalSecret carries a real token — so this hit
exactly the environment a newcomer meets first. Found by applying the manifests
to a real cluster; fixed, with four guard tests each verified by reintroducing
its defect.

**Two alert links resolved nowhere.** The runbook had one combined heading for
two alerts, and the guard substring-matched the whole document rather than its
headings, so it certified a dead link. Both fixed.

**The dashboard wasted two thirds of the viewport.** Cards were capped at 460px
and packed to the top, so panels stopped mid-screen while their own content
clipped mid-row. Also: the agent-count field rendered unstyled and read as
disabled, a long decision name printed on top of its own reasoning, and the
reasoning panel was empty on arrival. Found by taking a screenshot, which
nobody had done.
