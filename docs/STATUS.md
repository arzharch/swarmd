# Status

**Generated:** 2026-08-28 · **Commit:** `b3587c3` · **Tests:** 807 passed, 1 skipped
**Gates:** ruff clean · mypy clean (54 files) · frontend typechecks and builds ·
kernel chaos integrity at kill-rate 0.9 · red-team gate passes with five seeded rogues

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

### G-4 · No live-provider validation
**Blocks:** acceptance criteria 2 and 5; every empirical number in the project.

Everything has run against the simulated provider. Three things are
consequently unmeasured, and each has a specific way it could be wrong:

- **Cache hit rate on real workloads.** Exact keying means genuinely novel
  tasks hit near zero. The 100% measured on a repeated run is the ceiling, not
  the expectation.
- **Whether K variants from one call are genuinely distinct.** The simulated
  provider returns eight different candidates because it was written to. A real
  model asked for eight distinct approaches may return three good ones and five
  rewordings, which costs diversity without recovering the call count.
- **The learning curve.** None exists. Until one does with its control arm,
  nothing here claims the system improves.

**Fix:** add `GROQ_API_KEY` and `GOOGLE_API_KEY`, then a `standard` run and a
`deep` session. Blocked on credentials only.

### Smaller, and not blocking
- No on-call rotation (single maintainer — stated, not solvable).
- Rollback documented but never exercised against a real cluster.
- The kernel `Runtime` and the swarm executor share the `Checkpoint` contract
  but not the loop. A deliberate duplication with a real cost; unifying them is
  the next structural refactor, not a correctness gap.

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
