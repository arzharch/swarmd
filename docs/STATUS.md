# Status

**Generated:** 2026-09-01 · **Commit:** `ceb4a71` · **Tests:** 1,250 passed, 9 skipped
**Gates:** ruff clean · mypy clean (61 files) · frontend typechecks and builds ·
kernel chaos integrity at kill-rate 0.9 · red-team gate passes with five seeded rogues ·
image builds and serves · manifests validate against real k8s schemas · rollback exercised on a cluster

The single authoritative list of what is built and what is not, checked against
[PRD.md](PRD.md), [SPEC.md](SPEC.md) and the acceptance criteria rather than
written from memory. Every "done" has evidence; every gap says what is missing
and what it blocks.

One gap remains. It needed a training corpus and a change to what counts as
one skill, both of which now exist; what is left is the measurement itself.

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
| Control plane | 8000 | `SWARMD_API` retargets the dev proxy. Windows reserves port blocks for Hyper-V; if bind fails with `[winerror 10013]`, pick a port outside `netsh int ipv4 show excludedportrange protocol=tcp` and point `SWARMD_API` at it |
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
| FR-8 | Live UI | **DONE** | Next.js, six views, websocket only, no fixture path (CI-enforced). Agent count and rogue seeding are controls, not config files. Every operation the CLI performs is reachable from it: start a run, read the artifacts it produced, run an eval or a session, probe providers, approve a skill, resume a run parked on a spent ration |
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
| 11 | Hardening and honest documentation | **PARTIAL** — docs complete (PRR, SECURITY, SLO, RUNBOOK, CAPACITY, 15 ADRs); the full-system chaos run at acceptance scale landed on live providers 2026-09-01 (25/25 nodes, 9 agents killed and resumed, $0.00). What remains is the learning measurement itself (G-4) |

---

## 4. Acceptance criteria (PRD §13)

| # | Criterion | State |
|---|---|---|
| 1 | All SPEC phase gates pass | **PARTIAL** — only Phase 11's live run outstanding |
| 2 | A held-out task runs end to end with no code change | **PASS mechanically, 2026-09-01** — both holdout tasks ran on live providers with no code change: frozen criterion, plan, graded nodes, $0.00, zero simulated ledger rows. Neither passed every node (`hold-schedule-1` solved 4 of 4 nodes with the right answer, 15 of 20 agents; `hold-logistics-1` solved 0 of 6, blocked by a criterion requiring `total_boxes` to be both 8 and 9 -- refused at freeze since 2026-09-01, and the task now solves 7 of 7 nodes) |
| 3 | Chaos at 0.2 across every stage; integrity hashes match | **PASS** — verified at 0.9 in CI, and in the swarm loop at 0.3 |
| 4 | All five seeded rogues detected and contained | **PASS** — four caught by their own detector; one blocked by the frozen criterion before any detector saw it, reported as its own outcome rather than counted as a catch |
| 5 | Full run at or under $0.05, itemised by provider | **PASS structurally** — ceiling enforced and itemised; every live run on 2026-09-01 cost $0.00 on free tiers, so it remains unverified against PAID traffic |
| 6 | Eval shows treatment vs control with CIs on both arms | **PASS** — verified over HTTP: 10 runs, both arms, "no measured improvement" |
| 7 | Frontend replays a live run, zero mock data paths | **PASS** — three CI guards enforce the no-fixture rule. The dashboard also *acts* now: it sent no operator token until 2026-09-01, so on a gated control plane it rendered live data and answered 401 to every button |
| 8 | Kill mid-run, restart, state intact | **PASS** — approvals, ledger, skills and criteria survive; the run resumes from the killed agent's checkpoint |

---

## 4a. Acceptance evidence, item by item

SPEC Phase 11's gate is "PRD section 13 acceptance criteria, item by item, each
with pasted evidence". This is that. Each entry names what produced it, so a
reader can re-run the thing rather than trust the row.

**1 — All SPEC phase gates pass.** PARTIAL. Phases 1–10 pass; this section is
Phase 11's own gate, and it is complete except where the rows below say
otherwise.

**2a — Unknown tasks at the sizes this product is for, 2026-09-01.** Five
tasks belonging to no suite in this repo, live providers, `--no-skills`:

```
task                     agents          nodes solved   agents passed
bakery waste flags       --agents 5           7/7            14/14
library overdue fines    --agents 8           4/4             8/8
server uptime shortfall  --agents 10 --chaos  5/5            10/10
warehouse box counts     --agents 8           7/7            14/14
parallel scheduling      --agents 8           4/4             7/8
```

Every node of every task was solved. The agent column is lower in two of them
because a node is run by a POOL and a pool losing a member is a population
search working -- which the report used to hide by counting agent outcomes
under the name `nodes_passed`. Both numbers are now reported separately.

The warehouse task is the interesting one: it failed 0/30 and then 0/12 with
the same memoised criterion, which turned out to be self-contradictory --
`total_boxes` required to be exactly 8 AND exactly 9. `Criterion.contradictions()`
now refuses that at freeze, and because the memo path re-attacks stored criteria
with current code, the fix reached a criterion frozen before it existed.

**2 — A held-out task runs end to end with no code change.** PASS mechanically,
2026-09-01, live providers, submitted through `POST /api/runs`:

```
run-3f4757e392  hold-logistics-1  completed  0/30 nodes   78.9s
                criterion 43b7c37957bb8bde   simulated=False   $0.00
run-21e0a680fe  hold-schedule-1   completed  15/20 nodes 103.5s
                criterion 7642a158f5945d7e   simulated=False   $0.00
                artifact: {"total_time": 13, "schedule": [...]}   <- correct
```

Both took an unseen task from prompt to graded artifact against a criterion the
system authored and froze itself. Neither passed every node, so this is not
"solves a held-out task"; `hold-logistics-1` produced a wrong answer on every
node.

**3 — Chaos at 0.2 across every stage; integrity hashes match.** PASS. CI job
`chaos integrity (SLO-2, no error budget)`, step `kernel determinism under
chaos`: `swarmd demo kernel --kill-rate 0.9 --tasks 40`, and step `swarm run
completes under chaos`. Verified at 0.9 rather than 0.2 because the gate has no
error budget.

**And at acceptance scale on live providers, 2026-09-01** — the SPEC Phase 11
deliverable, which until now had only ever run against the simulated provider:

```
swarmd swarm run "<logistics scheduling task>" --profile standard --chaos --no-skills

run=run-61cd7e40d6  status=completed  34.5s
criterion=9368bc96464e34d0 (4 checks, attempts=1)
plan=655bdafb711165f3 (5 nodes, width=2)
nodes_passed=25/25  contained=0
integrity_hash=ee7e549f587d3ae6
cost=$0.000000 of $0.05 ceiling  calls=17
prefix_cache=896/6736 prompt tokens served from the provider's cache (13.3%)
agents=34 alive=25 bankrupt=0 contained=0
```

Nine of thirty-four agents were killed mid-run and resumed from their
checkpoints (`resumed`, then `skipped_generate` — the replacement did not
re-buy the work), every node passed, and the integrity hash came out intact.
The 13.3% is the PROVIDER's prompt-prefix cache, not the run memo; it is the
first live reading of either.

**4 — All five seeded rogues detected and contained.** PASS. CI step `red-team
gate (SPEC Phase 8)` runs `--seed-rogues all`. Four are caught by their own
detector; `criterion_gaming` is blocked by the frozen criterion before any
detector sees it, and is reported as its own outcome rather than counted as a
catch.

**5 — Full run at or under $0.05, itemised by provider.** PASS structurally.
The ceiling is enforced at the harness boundary and every run is itemised in
the ledger. **Never exercised against paid traffic:** `SWARMD_ALLOW_PAID` is
false and every live run to date cost $0.00 on free tiers, so the abort path
has been tested against a synthetic ceiling and not against real spend. Closing
this means deliberately spending money, which is an operator decision rather
than an oversight.

**6 — Eval shows treatment vs control with CIs on both arms.** PASS. Verified
over HTTP; the report carries `success_ci` on both arms, a paired delta with
its own interval, pass@k where the sample supports it, and the words "no
measured improvement" when the intervals overlap. Two refusals now guard it:
the sweep will not start when the arms would be identical, and runs that never
reached the task are excluded from every figure rather than counted as
failures.

**7 — Frontend replays a live run, zero mock data paths.** PASS. Three CI
guards in `tests/test_deploy_guards.py` (`test_the_frontend_imports_no_sample_data`,
`test_the_frontend_has_no_hardcoded_backend_host`, and the manifest checks).
Additionally, as of 2026-09-01 the dashboard can act as well as watch: it sends
the operator token, resumes a run parked on a spent ration, and renders the
artifacts a run produced.

**8 — Kill mid-run, restart, state intact.** PASS. `tests/test_kill_resume_process.py`
kills a real process mid-run and resumes it, asserting progress is preserved by
**counting provider calls** across the kill rather than by comparing output —
identical output would only prove the work was deterministic, not that it was
recovered instead of repeated.

---

## 5. The remaining gap

### G-4 · The system solves tasks. The learning loop turns; whether it helps is now measurable
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

**And it was still not an ablation over HTTP, until 2026-09-01.** The fix
above landed in the CLI. `_eval_runner` in the control plane — the path the
dashboard and `POST /api/evals` use — kept constructing `SwarmRun` with no
`skills=`, so every eval started from a browser compared a configuration
against itself and reported "no measured improvement" in the same words the
real experiment produces. Found by starting a 100-cell sweep and reading the
code while it ran; cancelled at cell 20 rather than spending the remaining
~1,500 requests on a null experiment. The endpoint now refuses at submit time,
with the reason, when the treatment arm would have nothing to retrieve — no
library configured, or a library with nothing approved in it. Refused rather
than warned: a warning in a job log is not attached to the number, and the
number is what gets quoted.

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

#### And on 2026-09-01, the real blocker was measured -- then removed

The re-measurement was attempted properly: train on `public`, measure on
`custom` (disjoint sets), library built by a full ten-task session on live
providers. It produced no number, and the reason was not quota.

**The library could not promote anything.** A skill reaches the review queue
only once it is `promotable` -- verified on `MIN_DISTINCT_TASKS = 2` distinct
task shapes, the bar that makes "does this transfer?" answerable at all. The
session's result:

| | |
|---|---|
| skills proposed | 22 |
| distinct task shapes seen in training | 5 |
| skills with evidence from 2+ shapes | **0** |
| promotable | **0** |
| approved | **0** |

Nothing was ever queued, so nothing was approved, so the treatment arm had
nothing to retrieve -- and an eval in that state compares a configuration
against itself.

**Two independent causes, and neither alone explained it.** Both are now fixed;
[ADR-014](adr/ADR-014.md) is the reasoning in full.

*The corpus had no shared structure.* Promotion asks for one approach that
worked on two DIFFERENT task shapes. The suite's twelve tasks have twelve
disjoint output shapes, so no approach was ever proposed twice and the bar was
unreachable at any sample size. A thousand mutually-unrelated tasks would have
promoted exactly as many as twelve: none. This is what the "50-200 tasks" item
had been getting wrong for days -- it is not a statistical argument, it is a
structural precondition, and what the corpus needs is FAMILIES of tasks calling
for the same kind of output. There is now a `train` arm of fifteen tasks in
five families of three, and it is not evaluable: `SessionRequest.arms` accepts
it, `EvalRequest.arms` does not, so measuring over the tasks a library was
built from is unexpressible rather than merely discouraged.

*And identity fragmented on wording.* `make_skill_id` hashes the instruction
text, which is written by a model, so the same approach distilled from two runs
minted a second record starting again from one shape. The first attempt to fix
this was measured against the old corpus, showed nothing, and was reverted --
correctly, because on a corpus where no two tasks share an approach, merging is
provably insufficient. It is not unnecessary: with families in place it is
load-bearing. Identity for evidence is now the abstracted artifact shape plus
the kinds of check that graded it. The plan step is deliberately NOT in the
key: plans are synthesised per task, so a key containing one can only ever
match another proposal from the same task -- exactly the evidence the bar
refuses to count.

**The loop turns.** Fifteen tasks on the `train` arm, live providers, 667
seconds:

| | before | after |
|---|---|---|
| records stored | 33 | 33 |
| distinct approaches | 33 | **10** |
| reaching 2 task shapes | 0 | **2** |
| approved | 0 | **2** |

The two are `approach: produce diagnosis, fix` (evidence from the permissions
and the timezone task) and `approach: produce duration_minutes, strategy`
(evidence from two different rate-limit tasks) -- two different families, each
confirmed by a task the other member never saw. Both were approved on their own
evidence: `approval_note` is empty on both, meaning no `force`, no bypass. This
is the first time in this project's history that a skill has cleared the bar.

`swarmd skills merge` exists for libraries written before this: it replays
stored records through `propose` -- the merge rule stays in one place -- reports
what would collapse, and writes nothing without `--apply`.

**What this does and does not establish.** It establishes that the mechanism
can produce a reviewable, approved, retrievable skill from evidence rather than
from a bypass. It does not yet say whether retrieving them helps: the ablation
over the unseen `custom` arm is what answers that, and with only two approved
skills aligned to two of the five custom tasks, the effect it can show is
bounded. A null result at this size would mean "not enough library to move
five tasks", not "skills do not help" -- and the honest fix for that is more
training volume, which is now finally worth spending.

**The ablation was started and stopped at cell 5 of 30, on purpose.** A free
check first: neither approved skill retrieved for the task it was built for.
Over both the bare prompt and the task-plus-step query `worker.py` actually
issues, the five custom tasks drew 0, 0, 0, 1 and 0 hits -- and the single hit
was `approach: produce diagnosis, fix` offered to the colour-ordering puzzle.
The cause was index size: `_idf` weights terms across APPROVED skills, and two
of them yield two distinct weights over 32 terms, so ranking collapses to raw
term overlap.

#### And then the loop answered the question without the ablation

**The economy pruned both approved skills for failing.** They were retrieved
during training and scored:

| approach | successes / uses | |
|---|---|---|
| `produce diagnosis, fix` | 0 / 26 | 0% |
| `produce duration_minutes, strategy` | 5 / 27 | 19% |

Both under the consolidator's 30% floor, both retired automatically with the
reason written to the record. Propose, gate, retrieve, measure, prune --
running end to end and reaching a verdict unprompted. It is the first
empirical statement this project has made about whether its own skills help:
**retrieved 53 times, succeeded 5.**

**The review then said why.** Four approaches later cleared the evidence bar,
and three carried their own source task:

- `produce reconciliation`, generality **1.00** -- the top score -- instructs
  every future run to emit `stock_count` and `ledger_count`, the stock task's
  vocabulary welded into identifiers where abstraction could not see it.
- `produce verdict`, 0.86, is advice about how UPTIME is measured. That is one
  task's subject matter, not a method for judging whether a claim is checkable.
- `produce count, reasoning`, 0.57, ends `<NUMBER>! = 6`. The factorial's
  argument abstracted; its result did not. `validate_instruction` cannot catch
  this: `shared_literals` compares against the TASK, and 6 never appeared in
  the task -- it was computed.

One survived: `produce count`, three shapes, no literals, an actual method.
**One clean skill in four.**

So the 0/26 is not mysterious. A worker handed advice naming another task's
keys, domain, or answer is worse off than one handed nothing, which is exactly
what the retrieval threshold's own docstring predicts.

**Three of four classes are now closed** ([ADR-015](adr/ADR-015.md)) -- the
fourth was found while auditing for the first three, and was the largest of
them: `_distil_instruction` wrote the artifact KEY NAMES into the advice, and
the skill's name (derived from those keys) was printed into the worker prompt
beside it. 17 of 31 live records carried them, against 8 for the identifier
leak. They are redundant as well as harmful -- the worker is shown its own
criterion's exact required keys, so a skill naming a different task's keys can
only agree by accident. Removed from both places; the instruction now records
the KINDS of value and says to take key names from the reader's own criterion.

Each of the others was traced to the line that let it through rather than
blamed on the model. The identifier leak: `strip_source_terms` splits on `_`, `-` and
camelCase, so `stock_count` collapses while `sort_by_price` survives. The
computed answer: the NUMBER pattern rejected any following period so that
`1.25` would not be split, and swallowed sentence-final digits with it --
`"the answer is 42."` yielded no literals at all. It now rejects a period only
when it is a decimal point.

**The third is not closable deterministically, and that was measured rather
than assumed.** Domain knowledge the planner introduced -- `probes`,
`monitoring` -- is structurally identical to `parse` and `validate` to any rule
that does not already know the subject. A detector was built and scored against
the four judged candidates: 0.41, 0.52, 0.35 for the rejects and 0.33 for the
approval. That is not a threshold. What compensates instead: retrieval now
withholds a skill that has already earned a pruning verdict, so a bad one costs
at most `PRUNE_MIN_USES` retrievals rather than the 26 it cost here.

**Also corrected, by measurement:** the approach identity included the
criterion's check kinds, but the criterion is authored fresh per run (ADR-009)
so its check set drifts between two runs of the same work -- reintroducing the
fragmentation the key exists to remove. On the 38-record library: name plus
check kinds gave 38 approaches and 3 clearing the bar; the name alone gave 34
and 5. Keyed on artifact shape alone now.

#### What G-4 is, as of this measurement

Not "does self-learning work" -- the loop demonstrably runs and returns a
verdict. Not corpus size, and not quota. It is **distillation quality**, with a
rate attached: three of four promoted approaches carried their source task, and
the skills that did reach retrieval scored 5 successes in 53 uses. That is a
specific piece of work with worked examples attached, which is a considerably
better place to stand than "no measured improvement".

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

**Two of those four blocks were self-inflicted, and that was found the next
day.** Groq's daily token limit is 200,000 *per model*, not 100,000 across the
account — the original figure came from measuring one model and applying it to
all of them, so a run stopped while two other models were untouched. And
OpenRouter's cap is 1,000/day because this account is funded; the table carried
the unfunded 50/day. Both figures are corrected in `router/budget.py` with
their sources and check dates, and `docs/CAPACITY.md` §7 records the
correction. The lesson is not "the providers were stingy", it is that a limit
written down without provenance is indistinguishable from one that is wrong.

**And a third block was self-inflicted in a way neither of those explains.**
The `101,522 / 100,000` reading above came from a meter that counted every call
twice: the ration's reservation and the pool's own usage row both charged the
same journal in full, so one call cost the day two requests and twice its
tokens. The real spend behind that line was around 50,000 tokens against a cap
that is really 200,000 per model — the run stopped with roughly three quarters
of the day untouched. Fixed 2026-08-31 with the two faults beside it (a grant
settled twice across a model failover, and a token estimate measured from
itself); the derivation and the tests are in
[CAPACITY.md](CAPACITY.md) §7, "The meter was reading double".

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
calls against a plannable budget of ~2,200 requests/day (docs/CAPACITY.md §7,
which §1 declares authoritative; `swarmd providers budget` currently prints
`plannable 2,195` -- the ~1,146 this line once carried was the pre-recount
figure) -- more than a quarter of total capacity for one run. It is now 24
agents and ~90 calls, so a dozen fit a day.
An operator can still ask for 500 or 1000; the count is honoured exactly, and
`preflight` prices the run against the remaining budget before it starts.

### Pacing: the run no longer dies when the day runs out

Added after the sign-off above, and it changes what "blocked" means.

A daily allowance is now cut into four 6-hour sittings per credential, per
dimension, so one afternoon cannot spend a day (`router/ration.py`). When a
sitting's slice is spent the run **pauses** rather than failing
(`router/pacer.py`): one pause shared by every agent in the air, announced with
the provider, the dimension that bound, and the instant capacity returns.

The pause is durable. The criterion, plan, batch drafts, economy balances,
containment set and finished nodes are written to `.swarmd/runs/<id>.json`
before the wait begins (`swarm/runstore.py`), and `SwarmRun.resume` rebuilds
from that without re-buying any of it.

Verified rather than asserted, in two ways:

- **A real process kill.** A child process is started, allowed to park on an
  exhausted ration, killed with `taskkill /F`, and a *second* process resumes
  from disk. Provider calls are counted from the usage journal, which both
  processes append to and neither can rewrite.
- **The integrity hash.** `tests/swarm/test_runstore_resume.py` asserts an
  interrupted-and-resumed run reports the *same* integrity hash as an
  uninterrupted one — not merely that it finishes.

Two silent defects were found by the contract tests written for this and would
not have been found by watching it work:

- Every heartbeat raised `TypeError` inside the ticker, whose `finally` woke
  the run, so a pause meant to last hours ended immediately and the run spun on
  the refusal it had just parked on. It *looked* like pausing worked.
- The pacer called its event sink as `emit(kind, **payload)`, which no sink in
  the codebase accepts, and the guard swallowed the error — so no pause event
  ever reached the dashboard, the logs or the run's own stream.

Surfaces: `--no-wait` and `swarmd runs list` / `swarmd runs resume`;
`no_wait` on `POST /api/runs`, `GET /api/runs/resumable`,
`POST /api/runs/{id}/resume`, `GET /api/pace`; and a dashboard banner that
counts down locally so it does not freeze between minute heartbeats.

`preflight` now projects a timeline — `fits_this_session`,
`fits_today_with_pauses`, `spans_days`, `exceeds_horizon` — with the first
pause and projected finish, because a yes/no verdict stopped being the right
shape once running out means waiting instead of failing.

### Not hitting the limit in the first place

Pacing decides how fast a known allowance is spent. Two changes decide whether
the allowance is known at all, and both closed gaps that were costing capacity
silently.

**The provider was telling us and we were not listening.** Every response
carries `x-ratelimit-*` saying what is left and when it refills, and all of it
was discarded until a 429 taught the same lesson one rejected request later —
a rejection several providers charge to the daily allowance. A success
reporting zero remaining for two hours now blocks the provider directly.

Three defects surfaced wiring it, none of which were visible from outside:

- `_retry_after` parsed reset headers with `float(raw.rstrip("s"))`, which
  throws on Groq's `"2m59.56s"`. The provider that states its reset most
  precisely was the one whose word was thrown away for a guessed backoff.
- A 429 whose wait was measured in hours was answered by halving the
  per-minute bucket — that is, by retrying a spent day slightly more slowly,
  earning another rejection per retry.
- `blocked()` never read the `exhausted` rows that `observe_day_limit` writes.
  The ration honoured the provider's own word; the pool's budget gate did not,
  and kept offering a provider that had already said no.

**Tokens are now paced per minute, not just requests.** Groq allows 30 requests
and 8,000 tokens a minute against calls averaging a little over 1,000 tokens,
so a request-only limiter sends 30 legal requests carrying 30,000 tokens into
an 8,000-token minute. Both dimensions are taken together or not at all:
granting requests while refusing tokens leaked the request allowance every time
tokens were the binding dimension.

**Still not measured:** every pacing test above uses a fake clock or a
deliberately tiny ration. The pause has not yet been exercised against a real
provider's real daily reset, which takes a day to observe by construction, and
the header path has not yet seen a real provider report itself empty.

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
