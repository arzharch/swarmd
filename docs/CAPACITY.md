# Capacity Plan

**Status:** v1.0 · **Updated:** 2026-08-27 · Owner: Arsh Zakee Chowhan
**Reviewed at:** every phase boundary, and whenever a provider is added or removed

This document exists because in `swarmd` the bottleneck is not CPU, memory, or
disk. It is **somebody else's rate limit**. Every design decision about agent
count, run duration, and batching follows from the numbers below, so they are
written down and dated rather than assumed.

---

## 1. Supply

Measured 2026-08-27. Published limits are treated as hints; `swarmd providers
probe` discovers the real ones and the pool adapts (ADR-008).

| Provider | Requests/min | Requests/day | Tokens/min | Cost | Notes |
|---|---|---|---|---|---|
| Groq | 30 | 14,400 | 6,000 | free | Fastest; ~700 tok/s on Llama 3.3 70B |
| Google AI Studio | 15 | 1,500 | 250,000 | free | 1M context, strongest free quality |
| **Configured total** | **45** | **15,900** | **256,000** | **$0** | |
| Cerebras *(not yet configured)* | ~30 | ~1M tokens/day | 30,000 | free | Would roughly double daily headroom |
| OpenRouter `:free` *(not yet configured)* | 20 | 200–1,000 | ~20,000 | free | Daily cap documented inconsistently |
| GLM 5.3 Flash *(paid overflow)* | 60 | — | 200,000 | $0.075/$0.25 per M | ~180 calls inside the $0.05 ceiling |

**The binding constraint is requests per minute, not tokens per minute.** At
45 RPM and a realistic 1,500 tokens per call, we consume ~67,500 TPM against a
256,000 TPM ceiling — token headroom is 3.8×. Optimising token usage therefore
buys nothing. Optimising *call count* buys everything. This single observation
drives every lever in section 3.

**Latency is not the constraint either.** Groq returns in ~0.3–0.7s, Google in
~2–3s. With even three requests in flight, RPM saturates before latency does.
Concurrency is sized to keep the RPM budget full, not to the agent count.

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
calls, and a 32-agent run over a 4-node plan issues 8 calls total.*

One call returns K candidate variants rather than one. Population search wants
many candidates for the same step, which is exactly the shape a single prompt
can produce.

**How the variants reach the agents.** The run generates the batch *before*
spawning the pool and pre-seeds each agent's checkpoint with its variant, marked
`generate:1`. The worker's resume path then skips the generate step and charges
nothing for it. Batching and chaos recovery are the same operation seen from two
sides — work someone else already did — so there is no batching window, no
coalescing broker, and no timeout to tune.

**K = pool size, bounded.** `ADVISORY_POOL` is 32 and `HARD_POOL` is 64.
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

| Profile | Calls | Wall clock @45 RPM | Agents | Purpose |
|---|---|---|---|---|
| `smoke` | ~60 | ~2 min | 20 | CI on every PR; proves the loop runs end to end |
| `standard` | ~600 | **12–18 min** | 500 | The live watchable run — full loop, chaos, red-team |
| `deep` | ~1,800 | ~40 min | 500 | Enough curve points for a learning claim to mean anything |
| `eval` | ~12,000 | ~4.5 hr | 500 | 100 tasks x 2 arms x 5 repeats; a batch job, not interactive |

**The standard run and the eval are different products.** One is something
you watch. The eval is something you run overnight and read in the morning. Trying
to make one thing serve both is what produces a demo nobody waits through and an
eval too small to be evidence.

At 15,900 requests/day, one full `eval` sweep consumes ~75% of the daily budget:
**one sweep per day, maximum.** Adding Cerebras and OpenRouter roughly doubles
that headroom.

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
| `swarmd_rate_limited_total` rate | > 10% of calls | Add a provider (Cerebras first — largest free daily quota) |
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
| groq | `openai/gpt-oss-20b` | 0.81s | 384 tok/s | 1,000 req | quota |
| google-aistudio | `gemini-3.5-flash-lite` | 1.25s | 29 tok/s | 1,000 req | quota |
| nvidia-nim | `nvidia/nemotron-3-super-120b-a12b` | 2.33s | 100 tok/s | ~33 req | **grant** |
| openrouter | `minimax/minimax-m3:free` | 2.30s | 20 tok/s | 50 req | quota |
| mistral-free | `open-mistral-nemo` | 0.62s | 54 tok/s | no daily cap | rate |
| cerebras | — | — | — | **none** | 402 |

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

### What this sustains

```
plannable     2,050 requests/day   from published DAILY allowances
  week       14,350 requests
  month      61,500 requests
one-off          33 requests/day   from finite grants -- these stop when spent
unverified   86,400 requests/day   upper bound from a per-minute rate with no
                                   published daily cap. Assumes 24 hours of
                                   perfect saturation. Not a plan.
```

Only the first figure is planned against. Folding the other two in would give a
headline of ~88,000/day that is 98% extrapolation, which is exactly the kind of
number this document exists not to print.

At the `smoke` profile's ~16 calls per run, 2,050/day is roughly **128 runs a
day, 900 a week**. A `standard` run at ~600 calls is 3 a day.

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

## 8. Assumptions, stated so they can be falsified

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
3. **1,500 tokens per average call.** If agents carrying many retrieved skills
   push this past ~5,000, TPM becomes the binding constraint instead of RPM and
   this entire document inverts.
4. **Published per-account limits are per-account, not per-key.** Discovered
   empirically by the pool; if a provider meters differently, `providers probe`
   reports it.

Each assumption has a metric behind it, so being wrong is visible rather than
mysterious.
