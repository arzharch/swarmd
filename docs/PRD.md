# PRD — swarmd

**Status:** v3.0 · **Owner:** Arsh Zakee Chowhan · **Last updated:** 2026-08-27
**Supersedes:** [archive-v2-PRD-leadops.md](archive-v2-PRD-leadops.md) (LeadOps flagship)

## 1. Problem statement

Agent systems are demonstrated on tasks their authors already knew how to solve. The
pipeline is hand-drawn, the success criterion is hand-written, and "the agents learned" is
asserted against no control. Throw the same system at a task nobody scoped for it and it
has no way to decide what *done* means, no way to decompose the work, no memory that the
last hundred tasks ever happened, and no way to tell a productive agent from one burning
budget in a loop.

Meanwhile the scale demos — a thousand coding agents on a repo — measure headcount, not
capability. Nobody publishes the cost per solved task, and nobody runs the ablation that
would show whether the swarm improved or the tasks got easier.

## 2. Vision

`swarmd` is a runtime for **generic agents thrown at unknown tasks**, which get better at
them over time and can prove it.

An unscoped task arrives. The swarm first agrees on *how success would be measured*, then
proposes competing decompositions, executes the winner across a large pool of
undifferentiated workers under a hard cost ceiling, verifies against the frozen criterion,
distills what worked into a reusable skill library, and consolidates between runs. A
red-team organ watches the action log the entire time and kills agents that go rogue.
Every claim of improvement is reported against its own ablation.

## 3. Design stance (reverses v2 §3)

v2 deliberately capped concurrency at 10–50 agents and disclaimed scale. v3 reverses this:
**hundreds to 1000+ concurrent agents, because population size is load-bearing** —
population search, market selection, and multi-proposal criterion synthesis all degrade to
theatre at N=20.

This reversal is bounded by physics, not ambition — and the physics has since been
MEASURED rather than estimated, which moved the numbers.

The estimate here was ~86,000 TPM across five providers. What the keys actually buy
(docs/CAPACITY.md section 7, measured 2026-08-28):

- **Cerebras is gone.** Its free tier now requires a card; every call returns 402.
- **Groq's binding limit is tokens, not requests**: 200,000/model/day, which at the ~1,000
  tokens this system sends per call is **~200 requests a day**, not the 1,000 its request
  quota advertises.
- Plannable capacity across everything configured: **~2,200 requests/day**
  (docs/CAPACITY.md §7, which §1 declares authoritative over its own supply
  table; live-checked 2026-08-30, `swarmd providers budget` currently prints
  `plannable 2,195 requests/day` — the total moves a little day to day because
  groq's share is a token cap divided by observed tokens per call, not a fixed
  request cap). The ~1,146 this line used to carry was the pre-recount figure,
  superseded once Groq's 200,000-tokens-per-*model* cap and OpenRouter's funded
  1,000/day were corrected (docs/CAPACITY.md §7).

A thousand agents each calling a model per step is not merely expensive, it is a
day's entire budget for one run. That is not a reason to abandon population size; it is
the reason rationing is the runtime's job.

So the stance is precise: **1000 agents, of which few are talking to a model at any
instant** — and the operator chooses the number, because only they know what the task is
worth. `--agents 1000` is honoured exactly; `preflight` prices it against the remaining
daily budget before the run starts, and says plainly when it will not fit. Profiles are
sized to what a day actually holds (`standard` = 24 agents, ~90 calls, a dozen a day)
rather than to an aspiration.

Batched generation is what makes a wide population affordable at all: one call returns K
candidates, so generation costs one request per plan node regardless of pool width.
Repairs remain one call each, which is why cost still grows with population and why the
preflight exists. Skill retrieval, sandboxed execution, verification, ledger accounting, and
red-team log scanning are all free. The LLM is a rationed resource and the runtime's job is
rationing it — cache, batch, route, and refuse. Scale claims are always paired with cost
per solved task.

## 4. Target users

1. **Engineers building agent systems** who need the operational layer — recovery, gates,
   HITL, cost ceilings — rather than another way to draw a graph
2. **Researchers** wanting an honest harness for self-improvement claims (control arms,
   append-only ledgers, ablation by default)
3. **Teams facing a cost wall** who need agent work to fit a budget rather than discover
   the bill afterwards

## 5. Goals

| # | Goal | Success metric |
|---|---|---|
| G1 | Unknown-task competence | System accepts a task it was never designed for, produces a frozen success criterion and an executable plan, with no code change |
| G2 | Verifier-first correctness | Success criterion authored and adversarially red-teamed *before* solving begins; gaming attempts on a seeded weak criterion are caught |
| G3 | Runtime plan synthesis | The stage DAG is generated per task and executed by the existing executor; cycle-free, dependency-correct |
| G4 | Measurable self-learning | Treatment arm (skills on) beats control arm (skills off) on the same tasks and seeds, reported with confidence intervals. **MEASURED NEGATIVE**: with a real 11-skill library the treatment arm scored 0/5 against control's 2/5 (node pass rate 56.7% vs 65.6%). Cause found -- skills were anchored to plan node names that are regenerated each run, so retrieval injected advice about steps the reading plan does not have. Fix applied; re-measurement pending quota. Note that until this week the two arms were byte-identical (the eval never passed a library), so no earlier G4 result meant anything |
| G5 | Rogue containment | Seeded rogue behaviours (budget siphon, criterion gaming, loop, unsafe call, library poisoning) are detected and contained; contained agents cannot affect run output |
| G6 | Hard cost ceiling | Full flagship run completes at **≤ $0.05**; breach aborts cleanly with a report, never silently truncates |
| G7 | Recovery under chaos | Kill agents at any rate; skill library, ledger, and approvals survive; results are unchanged (integrity hash matches). Completed work is never redone: the kernel proven at kill-rate 0.9, the swarm flagship verified by counting provider calls across a kill, bounded at 5 resumes per node so relentless chaos terminates |
| G8 | Live observability | Every agent's reasoning, action, and observation visible in real time in the web UI, sourced from the real event stream |

## 6. Non-goals

- Not an agent framework or DSL — no visual builder, no YAML pipelines
- Not autonomous action in the world — external side effects terminate in human approval
- Not a model trainer — "learning" means skill library plus prompt consolidation, never
  weight updates, and is labelled that way in every document
- Not multi-node distributed execution in v1 (single process; interfaces leave room)
- No unqualified scale claims — every agent-count number is published with cost per solved
  task alongside it

## 7. Flagship application: the generalist swarm

`examples/swarm/` — one worker type, no per-stage specialists. Behaviour comes from
retrieved skill plus assigned role plus budget, all runtime data.

```
unknown task in
  |
CRITERIA     N agents independently author a success criterion
             red-team attempts to pass it with garbage
             garbage passes -> criterion rejected, re-authored
             -> FROZEN (immutable for the rest of the run)
  |
PLAN         agents propose competing DAG decompositions
             judge merges -> validated (acyclic, deps resolvable)
             -> handed to the existing DAG executor
  |
RETRIEVE     skill library queried for matching prior solutions
             (cold start: empty, and the run proves it)
  |
EXECUTE      large generic worker pool, chaos killing them throughout
             each agent spends from a budget ledger
             paid on VERIFIED success only, not on output
             bankrupt agents die, profitable strategies clone
  |
GATE         frozen criterion decides - bounded repair - dead-letter
  |
DISTILL      winning sub-plans -> candidate skills
             HUMAN APPROVES what enters the library
  |
CONSOLIDATE  prompts rewritten, dead skills pruned, traces compacted
  |
CURRICULUM   next task proposed at the measured ability frontier
```

Running throughout, not as a stage: **the red-team organ** (section 8).

### 7.1 Why each piece is here

| Piece | Without it |
|---|---|
| Verifier-first | The model grades its own homework; every success number is unfalsifiable |
| Red-teamed criterion | A weak criterion is worse than none — it manufactures confident false passes |
| DAG synthesis | The task must be one you already scoped; "unknown task" is marketing |
| Skill library | Task #100 costs exactly what task #1 cost; nothing was learned |
| Budget ledger | Selection pressure has no currency; bad strategies never die |
| Human gate on distillation | The library poisons itself and every later task inherits it |
| Consolidation | The library grows monotonically into noise |
| Curriculum | Tasks must be hand-fed forever |

## 8. Red-team as a runtime organ

Not a test suite. Continuous monitors tailing the live action log, with authority to act.

| Rogue pattern | Signal | Containment |
|---|---|---|
| Budget siphon | spend per verified success past threshold | quarantine, reclaim ledger balance |
| Criterion gaming | passes the frozen criterion with degenerate output | fail item, flag criterion for re-authoring |
| Loop | repeated near-identical action signatures | kill, requeue from checkpoint |
| Unsafe tool call | sandbox policy violation | kill, escalate to human |
| Library poisoning | proposed skill degrades the control-arm score | reject skill, revert, audit entry |

Containment is *runtime authority*: the monitor calls the same kill path chaos uses, so
containment inherits checkpoint recovery for free. A contained agent's work never reaches
run output, and the containment decision is an immutable audit record.

Deliberate design constraint: **monitors are cheap.** Signature matching, ratio checks, and
policy checks are pure code with no model call. Only ambiguous cases escalate to an LLM
judge, so red-teaming does not consume the rationed budget it exists to protect.

## 9. Live frontend

Next.js, fed exclusively by the real event stream. No fixtures, no synthetic data, no
placeholder states anywhere a viewer can reach.

- **Agent grid** — every agent, live state, current role, budget remaining
- **Agent drill-down** — the reasoning stream: thought, action, observation, in order
- **Synthesized DAG** — the plan this run generated, nodes lighting as stages execute
- **Skill library** — growth over time, retrieval hits, pending human approvals
- **Ledger** — spend by provider and by stage against the $0.05 ceiling, live
- **Red-team feed** — detections and containments as they happen
- **Chaos feed** — kills, requeues, recoveries
- **Learning curves** — treatment vs control, updating between tasks

Transport: the kernel already emits lifecycle events on a bus and traces chain-of-thought
with global tick ordering. A WebSocket sink joins the existing composite sink alongside
JSONL and OTel. The UI is a consumer of the same stream Jaeger gets — it cannot show
anything that did not actually happen.

## 10. Evaluation

`swarmd eval` is the artifact the project is judged on.

- **Public arm** (~100 externally-authored tasks): answers "is this self-graded?"
- **Custom arm** (held-out, cross-domain: data wrangling, paper reproduction, broken repo,
  puzzle, API integration): answers "does it handle what it wasn't built for?"
- **Both arms run control vs treatment** — skills disabled vs enabled, identical tasks and
  seeds. An improvement claim means the treatment arm beat its own ablation.
- Reported: success rate, cost per *solved* task, first-pass gate rate, tokens per task,
  wall-clock, containment count — each with confidence intervals over N repeats.

Metrics are computed from the append-only run ledger, never from in-process counters an
agent could touch. This is the structural answer to "self-learning is over-claimed": the
number is unfakeable by construction, and it is reported against a control or not at all.

## 11. Cost model

Hard ceiling: **$0.05 per full flagship run**, enforced by the existing token-budget code.
Breach behaviour is a clean abort with a report.

Provider pool, health-scored by observed rate-limit rejections and latency:

| Tier | Members | Role |
|---|---|---|
| Free, quota | Groq, Google AI Studio, OpenRouter `:free` | bulk of all calls |
| Free, grant | NVIDIA NIM (~1,000 credits, expires 30 days after issue, never refills) | burst only — spent once, then gone |
| Free, opt-in | Mistral Experiment tier | **requires consenting to data training — explicit flag, off by default** |
| Paid overflow | GLM 5.3 Flash ($0.075/M in, $0.25/M out, 1.31M context) | roughly 180 calls within the ceiling |

Cerebras is not in this pool: its free tier now requires a card on file and
every call returns 402 (docs/CAPACITY.md §7, checked 2026-08-28).

Published limits are treated as hints — sources disagree on OpenRouter's daily cap — so the
router discovers real limits empirically and adapts. Free tiers train on submitted prompts;
this is documented in the README rather than buried.

## 12. Functional requirements

- **FR-1 Criterion synthesis** — N independent proposals, cross-check, adversarial red-team
  pass, freeze; the frozen criterion is immutable and content-addressed
- **FR-2 Plan synthesis** — competing DAG proposals, judge merge, structural validation,
  execution by the existing DAG executor
- **FR-3 Generic worker pool** — one agent implementation; role, skill, and budget injected
  at runtime; scales to 1000+ with most steps LLM-free
- **FR-4 Skill library** — durable store; propose, human-approve, retrieve, score, prune;
  every skill carries provenance and success statistics
- **FR-5 Budget ledger** — append-only, per-agent allowance, payment on verified success,
  bankruptcy, run-level hard ceiling
- **FR-6 Red-team organ** — continuous cheap monitors with kill authority; LLM escalation
  only for ambiguity; immutable containment audit
- **FR-7 Provider pool router** — multi-provider free-tier fan-out, empirical limit
  discovery, health scoring, paid overflow, hard abort at the ceiling
- **FR-8 Live UI** — Next.js over WebSocket, real stream only
- **FR-9 Eval harness** — public and custom arms, control vs treatment, reported in CI
- **FR-10 Recovery** — checkpoints at step boundaries; library, ledger, approvals, and
  frozen criteria all survive process restart

## 13. Acceptance criteria (v0.1)

Status as of 2026-08-28, against live providers. `docs/STATUS.md` carries the
evidence for each; this table carries the verdict.

| # | Criterion | Status |
|---|---|---|
| 1 | All SPEC phase gates pass | **PASS** except the live acceptance run in Phase 11 |
| 2 | A held-out task runs end to end with no code change: criterion frozen, DAG synthesized, executed, verified | **PASS** — 6/6 nodes on live providers, repeatably, on tasks not used in development |
| 3 | Chaos at kill-rate 0.2 across every stage; integrity hashes match | **PASS** — verified at 0.9 in CI |
| 4 | All five seeded rogue behaviours detected and contained; none reach run output | **PASS** — and four of the five now also fire on *unseeded* live runs |
| 5 | Full run cost at or under $0.05, itemised by provider | **PASS** — measured $0.000000; every provider used is free-tier |
| 6 | Eval report shows treatment vs control with CIs on both arms | **PASS** — 20% vs 20%, CI[0.00, 0.60], "no measured improvement" |
| 7 | Frontend replays a live run, zero mock data paths | **PASS** — CI-enforced |
| 8 | Process killed mid-run and restarted: state intact | **PASS** — and the eval itself now resumes, which it did not |

**The one that is not yet met is G4, measurable self-learning.** Both arms score
20%. That is not a failure of the eval -- it is the eval working: the skill
library is empty, so the treatment arm has nothing to retrieve and there is no
mechanism by which it could beat control. Distillation requires two verified
successes on the same node before it will propose a skill, and at a 20% task
success rate that has not happened yet.

The honest reading: **the system solves tasks; it has not yet been shown to
learn.** Those are different claims and only the first is currently supported.

### What a v1.0 sign-off requires

1. Volume: n=5 gives CI[0.00, 0.60]. 50-200 tasks before the success rate is a
   property rather than a measurement.
2. A learning curve with its control arm, which needs the library to be
   non-empty, which needs a higher success rate first.
3. `standard` and `deep` exercised against real providers. Every live run so
   far has been `smoke`.

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Swarm-authored criteria are wrong or gameable | Adversarial red-team must fail to pass garbage before freeze; seeded weak-criterion test in CI |
| "Self-learning" reads as over-claim | Control arm mandatory; ledger-derived metrics; the word "learning" is scoped to skills and prompts, never weights, in every document |
| Rate limits collapse the run | Multi-provider pool, empirical discovery, cache-first, batching; degraded mode completes with fewer agents rather than failing |
| Free tiers change or vanish | Providers behind one interface; paid overflow always available; limits discovered, not hardcoded |
| Cost overrun | Hard ceiling with clean abort; cost is a first-class run output, shown live |
| 1000-agent claim reads as generic | Always published as agent count *and* cost per solved task; the rationing engineering is the point |
| Frontend drifts into mock data | UI consumes the same sink Jaeger does; a CI check fails the build on fixture imports in the app |
| Scope creep | Non-goals enforced; PRD update required before new surface |
