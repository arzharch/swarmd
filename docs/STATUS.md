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

### G-4 · The system solves tasks. Self-learning is measured, and NEGATIVE.
**Blocks:** an unconditional QA sign-off.

**Task solving works.** 8/8 nodes repeatably on live providers, 20% at task
level over the suite, $0.00. That was 0% this morning; four mechanisms were
rejecting correct work and are fixed.

**Self-learning does not, and now there is evidence rather than an absence of
it.** The measurement that was impossible for most of this project's life has
now run twice.

#### First: the ablation was not an ablation

`swarmd eval` constructed `SwarmRun(use_skills=use_skills)` and never passed
`skills=`. Since `self.skills = skills if use_skills else None` evaluates to
`None` when `skills` is `None`, **both arms ran with no library**. The
treatment and control arms were byte-identical code paths.

Every "no measured improvement" this project has reported was therefore a null
result generator, not a measurement. An A/B test whose arms are the same code
cannot produce anything else, at any sample size. Fixed: the library is passed
to both arms, and `use_skills` gates retrieval, so the only variable between
arms is whether a skill may be read.

#### Then: with a real library, skills made it WORSE

11 skills distilled from a training session, human-gate approved, retrieved by
the treatment arm:

| | treatment | control |
|---|---|---|
| solved | 0 / 5 | **2 / 5** |
| node pass rate | 56.7% | **65.6%** |
| pass@1 | 0% | 40% |

Verdict from the harness: **no measured improvement, delta -0.400.** It
declines to call that a regression at n=5 because the intervals overlap, which
is the correct reading and not a hedge.

**Diagnosis.** Distillation was writing skills anchored to plan node names --
`"For steps like 'extract_dates': ..."`. Plan node names are generated fresh
for every run, so retrieval was injecting confident instructions about a step
the reading plan does not have. A worker told how to do `extract_dates` while
executing `tokenize` is worse off than one told nothing, which is exactly what
the retrieval threshold's own docstring predicts.

**Fix applied, not yet measured:** skills now describe the kind of work and the
output shape it produced, with no node name. The library was rebuilt and reads
`"When a step calls for this: Return a list of all date strings found in the
paragraph. Produce a JSON object with these fields: ..."`.

**The re-measurement did not run**, because the day's provider budget was spent
on the two that did:

```
groq         101,522 / 100,000 tokens   BLOCKED (resets in 1 day)
openrouter        51 / 50 requests      BLOCKED
nvidia-nim         0 / 1,000 credits    GRANT EXHAUSTED, does not refill
google           496 / 1,000 requests   429 under load
```

That is the budget system reporting exactly what it was built to report,
including the finite grant reaching zero. It is also the honest reason G4
carries a measured negative and an unmeasured fix rather than a result.

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

**QA: still conditional, and I am not signing it off today.**

The product's core claim is that generic agents solve tasks nobody scoped for
them. That now happens: 20% of tasks end to end, 6/6 nodes on repeated single
runs, at $0.00. The claim is demonstrated rather than asserted, which it was
not this morning.

What I will not sign, in order of how much it matters:

- **The learning claim is measured negative.** Skills made the treatment arm
  worse: 0/5 against 2/5. A diagnosis exists and a fix is applied, but the
  re-measurement has not run. Signing off on self-learning today would mean
  signing off on a hypothesis.
- **Volume.** n=5 gives CI[0.00, 0.60]. That interval admits almost any true
  value, so 20% is a measurement, not a property.
- **Scale.** Every live run has been `smoke`. `standard` and `deep` are sized
  to the measured budget and have never been run against real providers.

What changed today is that all three are now *measurable*. The ablation
compares two different things, pass@k and node pass rate are reported, and the
eval resumes rather than losing a sweep to an interruption. The blockers are
measurements that need quota, not features that need building.

The profiles were resized as part of this: `standard` was 500 agents and ~600
calls against a measured budget of ~1,146 requests/day -- half a day of total
capacity for one run. It is now 24 agents and ~90 calls, so a dozen fit a day.
An operator can still ask for 500 or 1000; the count is honoured exactly, and
`preflight` prices the run against the remaining budget before it starts.

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
