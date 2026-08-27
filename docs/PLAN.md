# PLAN — Execution Roadmap & Working Protocol

**Status:** v2.0 · **Updated:** 2026-08-27 · Companion to [SPEC.md](SPEC.md) (phases/gates)
and [PRD.md](PRD.md) (goals)

SPEC says *what* each phase delivers and *how it is gated*. This file says *in what order
we build it, what decisions each step forces, and what must be written down when*. If SPEC
and PLAN disagree, SPEC wins on scope; PLAN wins on order.

---

## 0. Working protocol (every commit, every phase)

### 0.1 The three-write rule

A feature is not done until three things are written in the same commit:

| Write | Where | Content |
|---|---|---|
| Code + tests | `src/swarmd/`, `examples/`, `frontend/`, `tests/` | Green tests, ruff + mypy clean |
| Progress log | `docs/flow.md` | What was done, **why**, alternatives considered, why they were rejected |
| Interview answers | `docs/interview_prep.md` | Every question this feature invites, answered — **plus the follow-up questions those answers invite, also answered** |

Gate evidence (command output, hashes, metrics) is pasted under the matching
`## Gate evidence` heading in `flow.md`.

### 0.2 Decision blocks

Anything non-obvious gets a decision block in `flow.md`:

```
DECISION: <one-line choice>
ALTERNATIVES: <option B> · <option C>
WHY THIS: <the reason that survives follow-up questions>
TRADE-OFF ACCEPTED: <what we gave up, consciously>
FOLLOW-UPS: <the questions this answer invites> -> <answers>
```

`FOLLOW-UPS` is new in v2 and is not optional. A decision that only survives the first
question is not documented, it is advertised. Architectural (hard to reverse) decisions
graduate to a numbered ADR in `docs/adr/`. Rule of thumb: reversible means a decision block;
one-way door means an ADR.

### 0.3 Command anatomy

Every command, flag, config knob, or tunable constant gets an anatomy block on first
appearance in `flow.md`. Standard is **understanding-level, not reference-level**: say what
the knob does, why *this* value, and what changing it causes.

```
ANATOMY: uv run swarm run "<task>" --agents 500 --kill-rate 0.2 --ceiling 0.05
  --agents 500    worker pool size. Why 500: large enough that population selection
                  and the market have real variance, small enough that the pooled
                  free-tier ceiling (~34 LLM calls/min) still lets a run finish in
                  under an hour. At 50 the economy is a meeting; at 5000 agents
                  starve waiting for tokens and the run never converges.
  --kill-rate 0.2 probability a running agent is killed per scheduling tick. Why 0.2:
                  hits every recovery path within a single run while leaving enough
                  survivors to prove partial progress is preserved.
  --ceiling 0.05  hard USD limit. Breach aborts cleanly with an itemised report rather
                  than silently truncating work, because a truncated run produces
                  numbers that look like results.
```

### 0.4 Branching and cadence

- One branch per step, merged when its checklist is green.
- `flow.md` updated in the merge commit.
- Commits are per-feature: not one squashed commit per phase, not one per file.
- End of each week: re-read `interview_prep.md` top to bottom; anything unanswerable
  becomes the next week's first task.

---

## 1. What already exists (Phases 1–4)

Kernel, pipeline, harnesses, gates, HITL state machine, router with cache and budgets.
Domain-agnostic and untouched by the v3 pivot. Three known debts, all discharged in
Phase 5: approvals are not durable across processes, the router is single-provider, and
there is no cost accounting in currency.

LeadOps (`examples/leadops/`) stays as the second-domain proof. It receives no new feature
work; its tests must stay green as evidence that the runtime is not shaped around the new
flagship.

---

## 2. Phase 5 — Production floor

Nothing here is a demo feature. All of it is what stops later features from being
unfalsifiable.

| # | Step | Files | Decisions this forces | Tests |
|---|---|---|---|---|
| 5.1 | Postgres approval + record store | `hitl/approvals.py`, `harnesses/store.py`, `migrations/` | schema ownership (app vs migration tool); connection lifecycle in a CLI process; how audit immutability is enforced (append-only table vs constraint) | approve in a *different process* from the run that queued it |
| 5.2 | Cost ledger | `ledger.py` | append-only vs mutable rollup; price table as data vs code; what a cache hit costs (zero, but it must still be a row) | run cost equals SUM of rows; no counter anywhere |
| 5.3 | Hard ceiling | `harnesses/llm.py` | where the check lives (harness boundary, so nothing bypasses it); abort semantics vs truncation | deliberate overrun aborts cleanly with report |
| 5.4 | Provider pool | `router/pool.py`, `router/providers.py` | per-provider auth and quirks behind one interface; empirical limit discovery from 429s; how a provider is benched and re-probed | forced 429 reschedules rather than failing the call |
| 5.5 | WebSocket sink | `observability/ws_sink.py` | backpressure when the UI is slow (drop vs buffer vs block — the run must never block on a browser); tick ordering across the wire | slow consumer never stalls the run |
| 5.6 | Prometheus metrics | `observability/metrics.py` | metric naming and cardinality policy | no per-task labels |

**Gate:** SPEC Phase 5. **Docs due:** anatomy for `--ceiling`, `--allow-data-training`,
provider probe output; ADR for ledger-as-only-metric-source.

**Track F0/F1 lands here:** Next.js shell, WebSocket client, agent grid, live event log,
cost panel.

---

## 3. Phase 6 — Criterion synthesis

| # | Step | Decisions | Tests |
|---|---|---|---|
| 6.1 | Criterion representation | executable predicate vs prose vs test file — must be machine-checkable or the whole loop is theatre | criterion runs against a candidate |
| 6.2 | N-proposal synthesis | how many proposers; how disagreement is resolved (vote vs judge vs escalate) | disagreement escalates, never silently picks |
| 6.3 | Adversarial pass | what counts as degenerate output; retry cap before honest failure | seeded weak criterion is caught |
| 6.4 | Freeze | content addressing; immutability enforcement | frozen hash stable across restart |

**Docs due:** "what if the criterion is wrong?" answered with its follow-ups.

---

## 4. Phase 7 — Plan synthesis, workers, sandbox

| # | Step | Decisions | Tests |
|---|---|---|---|
| 7.1 | DAG proposal + merge | judge vs vote vs union; what makes a plan invalid | invalid plans rejected, not hopefully executed |
| 7.2 | Structural validation | acyclicity, dependency resolvability, leaf reachability — reuse existing cycle detection | cycle injected is caught |
| 7.3 | Generic worker | how role/skill/budget inject without subclassing | one implementation covers every stage |
| 7.4 | Sandbox harness | subprocess isolation depth on Windows and Linux; resource cap mechanism; violations as events | violation contained, run survives |
| 7.5 | Scale run | pool sizing against the token ceiling; queue starvation | 500 agents complete under chaos |

---

## 5. Phase 8 — Red-team organ

| # | Step | Decisions | Tests |
|---|---|---|---|
| 8.1 | Monitor framework | pull vs push over the action log; monitor cost budget | monitors add no LLM spend |
| 8.2 | Five detectors | thresholds per pattern, each justified in an anatomy block | each seeded rogue is caught |
| 8.3 | Containment authority | reuse the chaos kill path so recovery is inherited | contained work never reaches output |
| 8.4 | Escalation | when a monitor may spend on an LLM judge; hard cap | escalation stays under cap |

---

## 6. Phase 9 — Skills, economy, consolidation, curriculum

| # | Step | Decisions | Tests |
|---|---|---|---|
| 9.1 | Skill representation and store | what a skill *is* (prompt, code, sub-plan); retrieval key | retrieval hits rise across a session |
| 9.2 | Human approval gate | what a reviewer sees; default-deny | poisoned proposal blocked |
| 9.3 | Economy | allowance sizing, payment on verified success, bankruptcy threshold, cloning rule | bad strategies die, good ones spread |
| 9.4 | Consolidation | prompt rewrite with rollback (extends supervisor); prune criteria | consolidation never lowers control-arm score |
| 9.5 | Curriculum | frontier definition from ledger pass rates | difficulty tracks measured ability |

---

## 7. Phase 10 — Evaluation

| # | Step | Decisions |
|---|---|---|
| 10.1 | Task loaders | which public suite; how held-out custom tasks stay genuinely held out |
| 10.2 | Arm runner | enforcing paired control runs; seed discipline |
| 10.3 | Statistics | which interval, how many repeats, how overlapping intervals are reported |
| 10.4 | BENCHMARKS.md generation | generated from ledger, never hand-edited |

---

## 8. Phase 11 — Hardening

Full-scale chaos run, README rewrite against what shipped, ADR backfill, complete
`interview_prep.md`, CI green including the mock-import check and frontend build.

---

## 9. Deferred: cloud

Explicitly out of scope until Phase 11 is green. When it starts, the story is already
instrumented: the ledger holds cost per solved task, the pool router holds provider
economics, and the metrics hold throughput. Moving to managed infrastructure becomes a
measured before-and-after rather than a claim.

---

## 10. Standing risk watchlist

| Risk | Watch signal | Pre-agreed response |
|---|---|---|
| Criterion synthesis produces unusable predicates | adversarial pass never converges | fall back to human-authored criterion for that task and report the fallback rate honestly |
| Rate limits collapse runs | provider rejection rate climbing in metrics | degrade agent count, never silently drop work |
| Frontend drifts to mock data | CI import check fails | build stays red until the fixture is removed |
| Learning claims outrun evidence | treatment/control intervals overlap | report "no measured improvement" and keep building |
| Doc debt | `flow.md` older than the newest feature commit | merge blocked until caught up |
| Scope creep | work not traceable to a PRD FR | goes to backlog, not the pipeline |
