# Capacity Plan

**Status:** v1.1 · **Updated:** 2026-08-29 · Owner: Arsh Zakee Chowhan
**Reviewed at:** every phase boundary, and whenever a provider is added or removed

This document exists because in `swarmd` the bottleneck is not CPU, memory, or
disk. It is **somebody else's rate limit**. Every design decision about agent
count, run duration, and batching follows from the numbers below, so they are
written down and dated rather than assumed.

---

## 1. Supply

The **declared** position, re-checked 2026-08-29 against each provider's own
documentation. Published limits are treated as hints; `swarmd providers probe`
discovers the real ones and the pool adapts (ADR-008), so where this table and
section 7 ever disagree, section 7 and `router/budget.py` are right.

| Provider | Requests/min | Requests/day | Tokens/day | Cost | Kind |
|---|---|---|---|---|---|
| Groq | 30 | 1,000 | 200,000 **per model** | free | quota |
| Google AI Studio | 30 | 1,000 | — | free | quota |
| OpenRouter | 20 | 1,000 | — | free | quota |
| Mistral | 60 | *no daily cap* | 1B/month | free | rate |
| NVIDIA NIM | 40 | ~33 | — | free | **grant, expires** |
| GLM 5.3 Flash *(paid overflow)* | 60 | — | — | $0.075/$0.25 per M | paid |

**Groq's binding constraint is tokens, not requests.** At the ~1,000 tokens a
call this system actually sends, 200,000 tokens/model/day is reached around 200
requests while the 1,000-request cap is still four-fifths unspent. Any plan
that counts Groq requests and ignores its tokens overstates it fivefold, which
is precisely the error that blocked a run at 98 requests.

**Mistral has no day to spread**, so it is `kind: rate` and is never
session-rationed (section 9). Cutting a day it does not publish into sittings
would invent a scarcity and waste the most generous free tier configured here.

**NVIDIA is a grant**: ~1,000 credits that never refill and expire 30 days
after issue. It is treated as spent.

**Latency is not the constraint.** Groq returns in ~0.3–0.7s, Google in ~2–3s.
With even three requests in flight, the rate limit saturates before latency
does. Concurrency is sized to keep the request budget full, not to the agent
count — which is why the population and the concurrency bound are separate
numbers (section 4).

---

## 2. Demand

### 2.1 Fixed cost per run

Independent of agent count. These are the loop's structural stages.

| Stage | Calls | Why this many |
|---|---|---|
| Criterion proposals | 3 | Fewer than 3 cannot show disagreement; more adds cost without adding independence |
| Criterion cross-check | 1 | One judge over the proposals |
| Adversarial attempts | 2 | Empty output and structurally-valid-but-vacuous output are distinct attacks |
| Re-author round (conditional) | 4 | Only when the adversary succeeds |
| DAG proposals | 3 | Same reasoning as criterion proposals |
| Plan judge/merge | 1 | |
| Skill distillation | 6 | 2 per candidate skill, capped at 3 candidates |
| Consolidation | 2 | |
| **Fixed total** | **~22** (26 with a re-author round) | |

### 2.2 Variable cost

Everything else is execution, and it is where a naive design dies:

```
naive:   500 agents x 10 steps            =  5,000 generation calls
       + 5,000 verification calls          =  5,000
                                             ------
                                             10,000 calls
                                             = 3.7 HOURS at 45 RPM
```

That is the version that takes hours. Section 3 is how it becomes 15 minutes.

---

## 3. Levers

Applied in order of leverage, not in order of cleverness.

### 3.1 Verification is code, not a model call — saves ~50%

ADR-009 requires the frozen criterion to be an **executable predicate**, not
prose. So every gate check is a function call: free, instant, deterministic.

This was chosen for correctness — a prose criterion means the model grades its
own homework. The capacity win is a consequence, and it is the largest single
saving available: it removes half of all calls before any optimisation starts.

### 3.2 Batched generation — K× per node

*Implemented: `swarm/batch.py`. Verified by counted provider calls, not by
estimate — `tests/swarm/test_batch.py` asserts a pool of N makes fewer than N
calls. Re-counted 2026-08-30 against a 4-node plan at 32 agents: a cold run
issues **10 calls total** (the fixed 6-call synthesis head — 3 criterion
proposers + 3 plan proposers — plus one batched generation call per node), and
a repeat of the same task issues **4**, because the run memo (`swarm/memo.py`)
serves the frozen criterion and plan without re-buying synthesis; the 4
per-node generation calls still happen, since a memo never supplies an answer.
The synthesis head does not shrink with the plan — a wider or deeper plan only
moves the generation term. See `docs/flow.md`'s 2026-08-30 entry and
`docs/interview_prep.md` §12 for the full derivation.*

One call returns K candidate variants rather than one. Population search wants
many candidates for the same step, which is exactly the shape a single prompt
can produce.

**How the variants reach the agents.** The run generates the batch *before*
spawning the pool and pre-seeds each agent's checkpoint with its variant, marked
`generate:1`. The worker's resume path then skips the generate step and charges
nothing for it. Batching and chaos recovery are the same operation seen from two
sides — work someone else already did — so there is no batching window, no
coalescing broker, and no timeout to tune.

**K = pool size, bounded.** `ADVISORY_POOL` is 32 (the widest pool a *profile*
may imply), `MIN_POOL` is 5 (the floor a node keeps in flight) and
`MAX_IN_FLIGHT` is 64 (how many agents run at once, whatever the population).
Generation is now O(1) per node, but **repairs are still one call each**, so the
worst case remains linear in pool size at `max_repairs` per agent. That is what
the bound is for; it is not a cost bound, since the ceiling handles cost.

**What it does not batch.** Repairs. A repair prompt carries one candidate's
specific failures, so two agents repairing different candidates are not asking
the same question.

### 3.3 Response cache — across runs, exact-keyed

*Implemented: `router/cached.py` wrapping the provider, so every call the system
makes goes through it rather than the one path someone remembered to
instrument.*

**Where the repetition actually is.** Not inside a run: each node's prompt
differs, and each repair prompt carries its own candidate's failures. The
repetition worth paying for is *across* runs — a session working a curriculum,
or two operators asking similar things. One cache per process, shared by every
run it serves. Measured on a live pair of identical runs: the second issued
zero generation calls and served all four nodes from the first.

**Exact matching, not similarity, and this is the correction that matters.**
The cache was built for paraphrase-heavy human input. This system's prompts are
assembled from templates: they share a long envelope and differ in one step name
and one instruction line. Measured cosine between three genuinely different plan
nodes: **0.97**, above the 0.95 threshold. Similarity matching therefore served
the `extract` node's answer to the `verify` node — every node producing the same
artifact, at a high hit rate and a low cost, while being wrong.

Raising the threshold does not fix it. Similarity here is dominated by shared
boilerplate and rises with template length, so a longer envelope pushes any two
prompts above any threshold. `CachedProvider` refuses a cache that is not in
`exact_only` mode.

**Two things opt out.**
- *Criterion and plan proposers.* Their whole purpose is independent opinions;
  serving proposer 1's answer to proposers 2 and 3 manufactures unanimity, and
  the merge would report full agreement across one opinion. They set
  `cache: bypass`, and their prompts now differ by angle as well.
- *Eval runs.* An eval measures variance across repeats. Serving repeat 2 from
  repeat 1 does not bias the bootstrap interval, it collapses it toward zero
  width — and a zero-width interval reads as a strong result. `SwarmRun` raises
  rather than accepting a cache on the `eval` profile.

Every hit writes a zero-cost ledger row carrying what the call *would* have
cost, so cache savings are a query rather than an estimate (ADR-007). On a free
tier that figure is $0 and the row still matters: what the cache buys there is
**requests**, which is the scarce resource.

### 3.4 Free red-team monitors — saves the safety tax

All five detectors are pure code: signature matching, ratio checks, policy
checks. Only genuinely ambiguous cases escalate to a model, capped at 5% of run
budget. An organ that consumed the resource it protects would be self-defeating
(ADR-010).

### 3.5 Model tiering

| Role | Model | Why |
|---|---|---|
| Worker steps | `llama-3.1-8b-instant` (Groq) | ~0.3s, cheap on the RPM budget, and worker steps are high-volume/low-stakes |
| Criterion authoring, judging, merging | `gemini-2.5-flash` (Google) | Low volume, high stakes — these decisions gate everything downstream |
| Red-team escalation | `gemini-2.5-flash` | An adversarial judgement is exactly where a weak model is worst |

Spending the strong model where volume is low and outcomes are decisive is the
whole of the tiering policy.

### 3.6 Net effect

```
600 execution calls
  x 8 (batching)          =  4,800 generated decisions
  / 0.4 (60% cache hit)   = 12,000 effective decisions
500 agents x 10 steps     =  5,000 decisions needed
                             -> fits, with 2.4x headroom
```

---

## 4. Run profiles

The figures below are `PROFILES` in `swarm/run.py`; they were resized once the
budget was measured rather than assumed, and the earlier 500-agent versions of
`standard` and `deep` are gone. `standard` at ~600 calls was half of total
daily capacity for a single run.

| Profile | Calls | Agents | Purpose |
|---|---|---|---|
| `smoke` | ~30 | 15 | CI on every PR; proves the loop runs end to end |
| `standard` | ~90 | 24 | The live watchable run — full loop, chaos, red-team |
| `deep` | ~280 | 64 | For a task worth spending on; ~an eighth of a day |
| `eval` | ~30 per task | 15 | One task inside a sweep, which multiplies it |

**Population and concurrency are separate numbers.** The agent count is the
population; `MAX_IN_FLIGHT` (64) bounds how many run at once, and `MIN_POOL`
(5) is the floor a node keeps in flight by default. An explicit `--agents` is
honoured in full — asking for 1000 gives 1000 — because a cap that cannot be
overridden is a lie about who is in control. What stops an oversized run is the
cost ceiling and the ration, both of which say so before it starts.

**The standard run and the eval are different products.** One is something you
watch. The eval is something you run overnight and read in the morning. Trying
to make one thing serve both produces a demo nobody waits through and an eval
too small to be evidence.

At 2,200 plannable requests/day, a 20-task × 2-arm × 3-repeat sweep at ~30
calls each is ~3,600 calls — more than a day. Sweeps are therefore paced across
sittings (section 8) rather than sized to fit one.

---

## 5. Saturation behaviour

Degradation is designed, not emergent. In order, as pressure rises:

1. **Cache absorbs it.** Hit rate climbs under repetitive load — the system gets
   cheaper exactly when it is busiest.
2. **Pool reroutes.** A 429 backs off that provider and traffic moves to the
   others (ADR-008).
3. **Scheduler applies backpressure.** Agents block on the bounded queue — the
   Phase 1 path. The run degrades to fewer *effective* agents and reports that.
4. **Paid overflow**, only if `--allow-paid` was passed, capped by the ceiling.
5. **Clean abort** with an itemised report.

What never happens: silently dropping work, or truncating a run while still
emitting numbers. A truncated run produces figures that look like results, which
is worse than no result.

---

## 6. Headroom and triggers

| Signal | Threshold | Action |
|---|---|---|
| `swarmd_rate_limited_total` rate | > 10% of calls | Add a provider. Cerebras is not a candidate — §7: its free tier now needs a card, 402 on every model as of 2026-08-28 |
| Cache hit rate | < 40% sustained | Investigate prompt normalisation before buying capacity |
| Daily request usage | > 80% of 15,900 | Add providers, or move `eval` to a multi-day schedule |
| `standard` profile wall clock | > 20 min | Re-derive this document; a lever has stopped working |

---

## 7. What the keys actually buy (measured 2026-08-28)

Every row below was produced by calling the provider, not by reading its
documentation. That distinction earned its place: the provider table this
project shipped with was assembled from docs, and **every entry in it was
wrong**.

| Provider | Working model | Latency | Throughput | Daily allowance | Kind |
|---|---|---|---|---|---|
| groq | `openai/gpt-oss-20b` | 0.81s | 384 tok/s | ~200 req *(token-bound)* | quota |
| google-aistudio | `gemini-3.5-flash-lite` | 1.25s | 29 tok/s | 1,000 req | quota |
| nvidia-nim | `nvidia/nemotron-3-super-120b-a12b` | 2.33s | 100 tok/s | ~33 req | **grant** |
| openrouter | `minimax/minimax-m3:free` | 2.30s | 20 tok/s | 1,000 req | quota |
| mistral-free | `open-mistral-nemo` | 0.62s | 54 tok/s | no daily cap | rate |
| cerebras | — | — | — | **none** | 402 |

**Two of these were re-checked on 2026-08-29 and both had been wrong in the
direction that costs capacity.** Groq's daily token limit is 200,000 per
*model*, not 100,000 across the account — the original figure was measured on
one model and applied to all of them, so a run reported "day budget exhausted"
while two other models were untouched. OpenRouter's cap is 1,000/day because
this account is funded; 50/day is the unfunded figure, and carrying it turned a
workhorse into a tie-breaker. Google's 15 RPM was contradicted by the journal's
own record of 16 successes inside one minute.

Both blocks that stopped work the day before were therefore **self-inflicted**:
the providers had capacity the table said they did not.

**Groq is the workhorse.** Roughly 4x the throughput of anything else here and
the joint-largest daily allowance, so it is ordered first.

**Cerebras is gone.** Its key returns `402 Payment required` on every model.
The free tier now needs a card on file, so it is not in the registry, not in
the budget table, and no longer priced as free in the ledger — a provider that
bills while priced at $0 would under-report real spend.

**NVIDIA is a grant, not a tier**, and this is the distinction the whole month
plan turns on. Roughly 1,000 credits, consumed at a variable rate per model,
**expiring 30 days after issue**. It never refills. Spread over its life that is
~33 requests/day; used as a workhorse it is gone inside a week and the month
has no burst capacity left. It therefore sorts *behind* the replenishing free
tiers despite also costing nothing.

Its catalogue also lies: `/v1/models` lists 83 entries and this account can
call four. The rest return `404 Not found for account`.

### The meter was reading double (found and fixed 2026-08-31)

Every figure in this document that came from *observed usage* rather than from
a provider's published allowance was overstated, and the cause was in this
repository rather than at any provider.

The ration and the budget tracker share one journal. A successful call wrote
three rows to it: the ration's reservation (`+1 request`, `+estimate tokens`),
the ration's settlement (`0 requests`, `actual - estimate`), and then a third
row from the pool's success path carrying the full cost again (`+1 request`,
`+actual tokens`). Summed — which is exactly what `window_state`, `grant_state`
and the session envelope all do — **one call cost the day two requests and
twice its tokens**.

The consequences were all in the direction of stopping early, which is why it
survived: a free tier was declared spent at half its real capacity, the session
envelope handed out half the slice it had, and the "groq 101,522 / 100,000
tokens BLOCKED" reading in [STATUS.md](STATUS.md) §5 was a doubled meter
reporting roughly 50,000 real tokens against a cap that was itself wrong by
half. Two independent errors pointing the same way is what made that day's
budget look four times tighter than it was.

A second fault sat on top of it. `observed_tokens_per_request` — the figure the
ration reserves against — filtered the journal to rows carrying a request
count, which kept the *reservation* row (the estimate) and dropped the
*settlement* row (the correction). It therefore averaged its own estimate and
could never converge on what the provider actually charged; it returned roughly
the midpoint of the 1,250-token default and the truth, forever.

Both are fixed and pinned by tests that fail against the previous code
(`tests/router/test_pool.py::test_one_call_costs_the_day_one_request`,
`::test_the_token_estimate_is_measured_from_settled_calls_not_itself`). Two
related faults in the same path were fixed with them: the ration grant was
taken once per *slot* but settled once per *attempt*, so a provider that failed
over from one model to the next settled one reservation twice and left the
served call charged zero requests; and the reservation was keyed on
`models[0]` rather than on the model that actually ran, which on a `per_model`
budget — groq's, the one that matters — rationed the whole account out of one
model's share.

**What this does NOT change.** The plannable total is computed from published
daily allowances, not from usage, so `~2,200 requests/day` stands. What changes
is how much of that a run may actually spend before the meter says stop: it was
half, and it is now all of it.

### What this sustains

```
plannable     2,200 requests/day   from published DAILY allowances
  week       15,400 requests
  month      66,000 requests
one-off          33 requests/day   from finite grants -- these stop when spent
unverified   86,400 requests/day   upper bound from a per-minute rate with no
                                   published daily cap. Assumes 24 hours of
                                   perfect saturation. Not a plan.
```

Per provider, with the basis each figure rests on -- because "1,000/day" means
something different when it is a request cap, a token cap divided by observed
call size, or a grant that never refills:

| Provider | Requests/day | Basis |
|---|---|---|
| google-aistudio | 1,000 | `daily_cap` |
| openrouter | 1,000 | `daily_cap` |
| groq | 200 | `daily_cap_tokens` |
| nvidia-nim | 33 | `grant` |
| mistral-free | 86,400 | `rate_only` — **excluded from the plan** |

**Groq's binding limit is tokens, not requests.** It publishes 1,000 requests
*and* 200,000 tokens per model per day. At the ~1,000 tokens per call this
system sends, the token budget runs out around **200 requests**. Discovered by
running until it broke: the CLI printed "day budget exhausted" beside
"98 / 1,000", because it was showing the dimension that was fine.

`rate_only` is excluded deliberately. Multiplying a per-minute rate by 1,440
produced a headline in which every run "fits", including runs that then ran out
within the hour — an 86,400 that is 100% extrapolation is not an allowance.

Daily capacity is now `min(request cap, token cap / observed tokens per call)`,
with the tokens-per-call figure measured from the usage journal rather than
assumed -- it moves with prompt size, which moves with schema hints and
retrieved skills.

Only the first figure is planned against. Folding the other two in would give a
headline of ~88,000/day that is 98% extrapolation, which is exactly the kind of
number this document exists not to print.

At the `smoke` profile's ~30 calls per run, 2,200/day is roughly **73 runs a
day**. A `standard` run at ~90 calls is about 24 a day.

### Windows, and why five hours is one of them

`swarmd providers budget` (and `GET /api/providers/budget`, and the Harness
view) report six windows: minute, hour, **5-hour session**, day, week, month.

The session window exists because it is the unit work is actually planned in.
"Can I run this afternoon" is not answerable from a per-minute rate, and it is
the question an operator asks before starting something that takes an hour.

Usage is summed from an append-only journal at `.swarmd/usage.jsonl`, not held
in a counter — the same reasoning as ADR-007. It is keyed per credential,
because that is the unit providers meter, and it survives restarts, because a
monthly budget held in a process is a process wearing a month's name.

Google's daily quota resets at **midnight Pacific** rather than on a rolling 24
hours, including the daylight-saving shift. Treating it as rolling under-uses
it all morning and over-commits against it at night.

---

## 8. Session rationing: spending a day across a day

Sections 1–7 say how much there is. This says how fast it may be spent, which
is a separate question and the one that was actually going wrong: a single
afternoon could consume a whole day's allowance, and every run after it failed
on 429s until the quota reset.

### The rule

```
session length     6 hours
sessions per day   4               (6 × 4 = 24 exactly; not a coincidence)
safety             0.9             headroom for the metering being wrong
envelope           (declared × 0.9 − spent in earlier sittings) ÷ sittings left
```

Applied **per credential and per dimension**. Requests and tokens are separate
ceilings and the first one reached is what stops the call, so both are
rationed; the refusal names which one bound.

Three properties, each of which was a bug before it was a rule:

- **A sitting does not shrink its own ration as it spends it.** Only earlier
  sittings leave the numerator. Subtracting the current sitting's own usage
  made the envelope fall with every call, so a run was refused well before
  reaching the slice it had been promised.
- **An unspent sitting rolls forward.** Dividing by *remaining* sittings rather
  than by four means an operator who runs nothing all morning is not held to a
  quarter of the day at 3pm.
- **A refusal is "not yet", never "not ever".** It carries the instant capacity
  returns, and the run parks until then rather than failing.

### Reset semantics

| Provider | Reset | Why it matters |
|---|---|---|
| google-aistudio | midnight **Pacific** | Treating it as rolling under-uses it all morning and over-commits at night |
| openrouter | midnight **UTC** | — |
| groq | **undocumented** | Treated as rolling and narrowed from `x-ratelimit-reset-*` headers |
| mistral-free | n/a | No daily cap, so nothing to spread |

Groq's page does not state how its daily window resets — not rolling, not a
timezone, nothing, and the blog claims of midnight UTC are unsourced. Rolling
is the error that under-uses rather than the one that double-spends.

A scheduled reset is not raced: calls inside a guard band before the boundary
wait for it rather than gambling on whose clock is right.

### What a run does when the slice is spent

It **pauses**, and the pause is durable. The criterion, plan, batch drafts,
economy balances, containment set and completed nodes are written to
`.swarmd/runs/<run_id>.json` before the wait begins, so a pause that outlives
the process is recoverable rather than merely survivable — verified by killing
the process mid-pause and resuming in a new one, then asserting the resumed run
reports the same integrity hash as an uninterrupted run.

`--no-wait` (and `no_wait` on `POST /api/runs`) turns the pause into a prompt
failure for callers that cannot sit through it. Waiting is the default, because
a run that dies has thrown away everything it already paid for.

`preflight` projects this before the run starts: `fits_this_session`,
`fits_today_with_pauses`, `spans_days`, or `exceeds_horizon`, with the first
pause and the projected finish. A yes/no verdict was the right shape when
running out meant failing; it stopped being right once running out means
waiting, since "finishes this evening after one pause" and "spans three days"
are the same answer to `fits` and only one of them is a reason not to start.

---

## 9. Assumptions, stated so they can be falsified

1. **60% cache hit rate.** Now the weakest number in this document, and worth
   stating precisely. Exact keying means the hit rate on a run of *genuinely
   novel* tasks is near zero: identical prompts are what hit, and unknown tasks
   do not repeat. The measured 100% on a repeated run is the ceiling, not the
   expectation. Sessions and curricula sit somewhere between, and nothing has
   measured where. If it lands near zero for real workloads, the `standard`
   profile's call budget rises and wall clock with it — batching, which does not
   depend on repetition, carries the plan on its own.
2. **K variants hold quality.** The simulated provider returns K genuinely
   different candidates because it was written to; a real model asked for eight
   distinct approaches may return three good ones and five rewordings. If it
   does, population diversity drops without the call count dropping back, and
   the honest response is a lower K rather than a louder prompt. Untested
   against a live model.
3. **~1,000 tokens per average call**, matching sections 1 and 7 and the
   `observed_tokens_per_request` journal (`router/budget.py`). The live reading
   of ~1,026 taken on 2026-08-30 — which priced groq's 200,000-token cap at 195
   requests for the day — **is withdrawn**: it was an average over a meter that
   counted each call's token ESTIMATE as well as its outcome, so it reported
   roughly the midpoint of the two and could not move off the 1,250-token
   default that fed it. Both faults are fixed (§7, 2026-08-31), and the figure
   is now measured from settled calls only. Until enough live calls have
   accumulated under the corrected meter, ~1,000 is the declared default and
   not a measurement — treat any per-provider request count derived from it as
   provisional. If agents carrying many retrieved skills push this past ~5,000,
   TPM becomes the binding constraint instead of RPM and this entire document
   inverts.
4. **Published per-account limits are per-account, not per-key.** Discovered
   empirically by the pool; if a provider meters differently, `providers probe`
   reports it.

Each assumption has a metric behind it, so being wrong is visible rather than
mysterious.
