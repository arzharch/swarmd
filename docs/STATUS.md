# Status

**Generated:** 2026-08-28 · **Commit:** `cf37dd3` · **Tests:** 708 passed, 1 skipped
**Gates:** ruff clean · mypy clean (50 files) · frontend builds · chaos integrity holds at kill-rate 0.9

The single authoritative list of what is built and what is not, checked against
[PRD.md](PRD.md), [SPEC.md](SPEC.md) and the acceptance criteria rather than
written from memory. Every "done" below has evidence; every gap says what is
missing and what it blocks.

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

---

## 2. Functional requirements (PRD §12)

| | Requirement | State | Evidence / gap |
|---|---|---|---|
| FR-1 | Criterion synthesis | **DONE** | `swarm/synthesis.py`. N proposers, consensus merge, adversarial pass over 9 degenerate candidates, content-addressed freeze. Refuses to freeze a weak criterion; 47 tests |
| FR-2 | Plan synthesis | **DONE** | `swarm/planner.py`. Competing DAGs, structural validation (acyclic, resolvable, reachable, bounded), judged on computable properties, executed by the Phase-2 executor. 34 tests |
| FR-3 | Generic worker pool | **DONE** | `swarm/worker.py`, one implementation; pool per node sized from the profile. Capped at 16/node — see gap G-1 |
| FR-4 | Skill library | **DONE** | `swarm/skills.py` + `hitl/skill_gate.py`. Propose → durable queue → human → retrieve → score → prune, with provenance. 59 tests |
| FR-5 | Budget ledger | **DONE** | `ledger.py`. Append-only, fsync per row, sums never counters, prices as data, unknown models refused, hard ceiling at the harness boundary. 23 tests |
| FR-6 | Red-team organ | **PARTIAL** | Five detectors, containment, audit — all built and tested (37 tests). **`--seed-rogues` does not exist**, so the SPEC Phase-8 gate command cannot be run. See G-2 |
| FR-7 | Provider pool router | **DONE** | `router/pool.py` + `router/quota.py`. Five providers, per-credential quota, empirical 429 discovery, tier ordering, Redis coordination. 63 tests |
| FR-8 | Live UI | **DONE** | Next.js, six views, websocket only, no fixture path (CI-enforced). Runs, decisions, cost, traceability, evals & sessions, harness |
| FR-9 | Eval harness | **DONE** | `swarm/evaluate.py`. Both arms, bootstrap CIs, paired on (task, seed), refuses a claim without a control, generates BENCHMARKS.md. 29 tests |
| FR-10 | Recovery | **DONE** | Kernel at kill-rate 0.9, byte-identical. Swarm workers checkpoint at `generate/materialise/grade` step boundaries; a killed agent's replacement resumes, verified by counting provider calls across a kill rather than by comparing output. Bounded at 5 resumes/node |

---

## 3. SPEC phase gates

| Phase | Gate | State |
|---|---|---|
| 1–4 | Kernel, pipeline, harnesses, gates, HITL, router | **PASS** — inherited, untouched by the v3 pivot |
| 5 | Production floor | **PASS** — durable approvals across processes (Postgres + SQLite), cost ledger, hard ceiling, provider pool, WebSocket sink, Prometheus |
| 6 | Criterion synthesis and adversarial freeze | **PASS** — weak criteria rejected; seeded-weak fixture caught in CI |
| 7 | Plan synthesis, generic workers, sandbox | **PASS** — DAG validated then executed, sandbox contains escapes, integrity hash matches under chaos |
| 8 | Red-team organ | **PARTIAL** — all five patterns detected and contained in tests; the gate's `--seed-rogues all` command is not implemented (G-2) |
| 9 | Skills, economy, consolidation, curriculum | **PASS** — `swarmd swarm session` and `POST /api/sessions`; library grows, human gate holds, state survives restart |
| 10 | Evaluation harness | **PASS** — `swarmd eval` and `POST /api/evals`, both arms with CIs, BENCHMARKS.md refuses simulated data |
| 11 | Hardening and honest documentation | **PARTIAL** — docs complete (PRR, SECURITY, SLO, RUNBOOK, CAPACITY, 13 ADRs); the acceptance run against real providers has not happened |

---

## 4. Acceptance criteria (PRD §13)

| # | Criterion | State |
|---|---|---|
| 1 | All SPEC phase gates pass | **NO** — Phase 8 and 11 partial (above) |
| 2 | A held-out task runs end to end with no code change | **NOT VERIFIED** — the machinery exists and the holdout set is written, but it has only run against the simulated provider |
| 3 | Chaos at 0.2 across every stage; integrity hashes match | **PASS** — verified at 0.9 in CI, and in the swarm loop at 0.3 |
| 4 | All five seeded rogues detected and contained | **PARTIAL** — proven in tests; not runnable as the gate specifies (G-2) |
| 5 | Full run at or under $0.05, itemised by provider | **PASS structurally** — ceiling enforced and itemised; unverified against paid traffic since nothing has cost anything yet |
| 6 | Eval shows treatment vs control with CIs on both arms | **PASS** — verified over HTTP: 10 runs, both arms, "no measured improvement" |
| 7 | Frontend replays a live run, zero mock data paths | **PASS** — verified live; three CI guards enforce the no-fixture rule |
| 8 | Kill mid-run, restart, state intact | **PASS** — approvals, ledger, skills and criteria survive; the run resumes from the killed agent's checkpoint rather than repeating the node |

---

## 5. Gaps, in priority order

### G-1 · The capacity plan's two multipliers are not implemented
**Blocks:** the 500-agent `standard` profile; the whole wall-clock argument.

[CAPACITY.md](CAPACITY.md) claims a 500-agent run fits 15 minutes via four
levers. Two are real (executable criteria save ~50% of calls; red-team detectors
are free). Two do not exist:

- **Batched generation (8×)** — no call returns K variants; every agent is one call.
- **Semantic cache (2.5×)** — `charge_cache_hit` exists in the ledger and
  **nothing calls it**. Cache hit rate is structurally 0.

Consequence: the pool is capped at 16 agents/node, so `--agents 500` is a
number the profile carries and the executor cannot honour. The cap is
deliberate and commented, but the capacity plan currently overstates what runs.

**Fix:** implement batching in the worker; wire the Phase-4 semantic cache into
`LLMHarness`. Then raise the cap. Est. moderate.

### G-2 · `--seed-rogues` does not exist
**Blocks:** SPEC Phase 8 gate; acceptance criterion 4.

The runbook and SPEC both name `swarm run --seed-rogues all`. The five detectors
are fully tested against seeded fixtures in `tests/swarm/test_redteam.py`, but
there is no way to inject a rogue into a real run. Same class of bug as the
missing `swarmd ledger` commands, which were fixed earlier this session.

**Fix:** a `--seed-rogues` flag that injects the five behaviours into the worker
pool. Est. small.

### G-3 · ~~The swarm flagship does not use the kernel's checkpoint recovery~~ CLOSED

Was the most significant finding of this audit, and the one place the project
said something untrue. Recorded rather than deleted, because the interesting
part is how it hid.

**What was wrong:** `SwarmRun` wrote no checkpoints. On a chaos kill it spawned
a fresh agent that redid the node from scratch. The integrity hash still matched
the clean run — which is exactly why nobody noticed. Deterministic work redone
produces the same bytes as work recovered, so the existing chaos gate could
never have caught this. `redteam.py` claimed containment "inherits checkpoint
recovery"; PRD G7 claimed completed work is never redone.

**Fix:** `GenericWorker.execute` now takes a `Checkpoint` — the kernel's type,
with the kernel's skip-completed-steps semantics, not a second implementation —
and checkpoints at attempt-scoped steps (`generate:1`, `materialise:1`,
`grade:1`). Attempt scoping matters: a repair round is genuinely new work, so a
kill during attempt two resumes at attempt two with attempt one intact. A
checkpoint holding a passed grade short-circuits the whole attempt.

`run_agent` carries the checkpoint across kills, bounded at `max_recoveries=5`
so a run where chaos always wins terminates as a failed node rather than
spinning. Payloads are JSON round-trippable, since a checkpoint that cannot
reach a durable store only works in memory — the one case it is not needed for.

**How the claim is now falsifiable:** the test counts provider calls, not
output bytes. `test_a_kill_between_generating_and_grading_reuses_the_generation`
asserts `worker_calls == 1` after a resume, so a regression to redoing the work
fails the suite instead of passing it silently.

### G-4 · No live-provider validation
**Blocks:** acceptance criteria 2 and 5; every number in the project.

Everything has run against the simulated provider. The capacity plan's central
assumption — a ~60% cache hit rate — is untested (and currently unimplementable,
see G-1). No learning curve exists.

**Fix:** add `GROQ_API_KEY` and `GOOGLE_API_KEY`, then a `standard` run and a
`deep` session. Blocked on credentials only.

### G-5 · Supervisor is not wired into the swarm
`examples/leadops/supervisor.py` exists and is tested; the swarm loop uses
`Consolidator` for prompt versioning but no supervisor samples dead-letters to
propose patches. PRD §7 lists it under the flagship.

### G-6 · Smaller items
- No on-call rotation (single maintainer — stated, not solvable)
- Rollback documented but never exercised against a real cluster
- ADR-001..005 live in one bundled file rather than per-ADR files
- `swarmd bench` is referenced in `cli.py`'s docstring and does not exist

---

## 6. Path to publication

**Before it can be shown as finished:**

1. G-2 (`--seed-rogues`) — small, closes a SPEC gate and an acceptance criterion.
3. G-1 (batching + cache) — makes the capacity plan accurate.
4. G-4 (live run + curve) — needs your keys.

**Before production**, additionally, from [PRR.md](PRR.md):
load test at real scale, rollback exercised, and the on-call gap acknowledged
rather than closed.

Publication-ready today: the architecture, the documentation, the test suite,
the operational surface. Not ready: the empirical claims, because they have no
data behind them yet — which the README already says outright rather than
implying otherwise.

---

## 7. Corrections this audit produced

Recorded because a status document that only lists progress is a marketing
document.

- **`asyncpg` was never a declared dependency.** The Postgres store had never
  once executed. Fixed, with nine integration tests and a CI service.
- **Postgres had no volume.** `docker compose down` would have discarded the
  audit trail. Fixed.
- **Port 5434 was already taken** by another stack, contradicting the comment
  claiming it avoided collisions. Moved to 5435.
- **The checkpoint-recovery claim in `redteam.py`, the PRD and the README was
  not true of the swarm path.** The chaos gate could not have caught it: redone
  deterministic work hashes identically to recovered work. Fixed, and the new
  test counts provider calls so the claim can fail.
