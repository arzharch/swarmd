# Interview Prep — Growing Q&A Companion

> As features land, the interview questions they invite get answered HERE, in writing.
> Rule: if you can't answer a question below confidently, the feature isn't done.

## How this file grows (binding — see PLAN.md §0)

- Every feature commit adds the questions that feature *invites* — including the
  uncomfortable ones ("why not just use Temporal/Celery/LangGraph?").
- Every tunable parameter we introduce gets a **parameter question** here, answered at
  understanding level: what it does, why our value, what changing it causes. Example of
  the expected depth:
  > **Q: Why temperature 0.2 in the QA stage?**
  > A: Temperature scales how randomly the model samples next tokens. Near 0 the model
  > becomes near-deterministic — same input, same output — which is exactly what a
  > verifier needs for reproducible pass/fail decisions and chaos-test hash equality.
  > Higher values trade determinism for diversity, useful in DRAFT ideation but poison
  > for gates. We keep drafts at ~0.7 and verifiers at ~0.2 deliberately.
- Weekly ritual: re-read top-to-bottom; anything unanswerable becomes next week's
  first task.

---

## Section 1: Project framing (answerable NOW)

**Q: What is swarmd in one sentence?**
A: A runtime for generic agents thrown at tasks nobody scoped for them - it makes the
swarm agree on how success will be measured before it starts, generates its own plan,
executes it across a large worker pool under a hard cost ceiling, and reports whether it
improved against its own ablation.

**Q: Why 1000+ agents when your earlier design doc argued against exactly that?**
A: Because the workload changed, and I wrote the reversal down rather than quietly
editing history - ADR-001 is marked superseded by ADR-008. The old stance was right for a
staged pipeline, where more agents is just more fan-out. The current flagship runs
population search, a market that selects on verified success, and N-proposal criterion
synthesis. All three are statistically meaningless at N=20. Population size is load-bearing
now, so the cap had to go.

**Q: Isn't a thousand agents just fake parallelism?**
A: It would be if I claimed a thousand simultaneous model calls. I measured the ceiling.
Quote the current number, not the first one: `swarmd providers budget` prints
`plannable 2,195 requests/day` from published daily allowances, which is about 1.5 calls
a minute sustained over a day. (The "~86,000 TPM, roughly 34 LLM calls a minute" figure
in `flow.md` is the 2026-08-27 pooled reading. It predates the 2026-08-29 recount and
predates Cerebras leaving the pool — its key now returns 402 — so it is history, not the
ceiling today.) So the honest claim is a thousand agents of
which very few are mid-call at any instant. Skill retrieval, sandboxed execution,
verification, ledger writes and red-team monitoring are all pure computation. The LLM is
the scarce resource and the runtime's actual job is rationing it. Every agent-count figure
I publish carries cost per solved task next to it, which is the number that makes the
count falsifiable.

**Q: What do the agents actually DO?**
A: There is one generic worker implementation. Role, retrieved skill and budget are
injected at runtime - no per-stage subclasses, because a specialist pool would mean I had
already scoped the task. In a run they author candidate success criteria, attack those
criteria to see if garbage passes, propose competing decompositions of the work, execute
sub-plans in a sandbox, verify results against the frozen criterion, and propose skills
for the library.

**Q: How do you know it works on an unknown task rather than one you tuned for?**
A: The eval has two arms. A public arm of externally-authored tasks answers "is this
self-graded". A held-out custom arm across five domains - data wrangling, paper
reproduction, a broken repo, a puzzle, an API integration - answers "does it handle what
it wasn't built for". Acceptance criterion 2 is that a task from the held-out arm, never
seen during development, runs end to end with no code change.

**Q: How does recovery from failed state actually work?**
A: Agents checkpoint at step boundaries. A heartbeat lease covers claimed work; when it
expires the runtime requeues the task with its checkpoint intact, and the resumed agent
skips completed steps deterministically. Proven by output-hash equality between a clean
run and a chaos run at kill-rate 0.9.

**Q: How do you prevent bad output flowing downstream?**
A: A quality gate between stages, with a bounded repair loop and a dead-letter queue. The
difference from the usual version is where the gate's predicate comes from: it is authored
by the swarm and frozen before any solving happens, not written by me after I saw what the
agents produced.

**Q: How does human-in-the-loop survive a restart?**
A: AWAITING_APPROVAL is a durable state in Postgres with an append-only audit trail, so
approve/reject works from a different process than the one that queued it. In v3 the human
gate also guards what the system is allowed to learn - a skill enters the library only
after a person approves it.

## Section 2: Kernel (answered as built)

**Q: Walk me through the checkpoint contract.**
A: Every agent task is a list of named steps. After each step completes, the runtime
saves a Checkpoint — an ordered list of completed step names plus each step's output,
with a schema_version so old on-disk checkpoints are rejected rather than silently
misread. The checkpoint is advanced purely (with_step returns a new object), persisted
BEFORE the next step starts, and the claim's lease is refreshed at the same moment.
Resume = load checkpoint, skip every step already in completed_steps, continue from the
first missing one. Because steps are pure functions of (checkpoint data, task payload),
the skip is deterministic: same inputs, same outputs, no double side effects.

**Q: How does heartbeat expiry avoid double-processing?**
A: Each claimed task has a lease (`expires_at`). The worker refreshes it after every
step; a reaper loop expires claims whose deadline passed and requeues them with their
checkpoint intact. Double-processing is possible only if a worker is slow-but-alive
past its lease while another claims the work — so the lease must exceed worst-case
step duration. Even then, effects stay idempotent because step outputs are keyed by
step name in the checkpoint: the slower writer's result simply overwrites the faster
one's identical value. This mirrors Kafka consumer-group leases and Temporal's replay.

**Q: What happens when an agent is killed mid-run?**
A: Three mechanisms engage: (1) the cancelled worker's claim stays registered until
its lease expires — exactly like a crashed process leaving a stale lock; (2) the
reaper requeues the task with its checkpoint and emits TASK_REQUEUED; (3) the reaper
also notices the dead worker and respawns one to keep pool size at `concurrency`.
Tests prove the final output equals a clean run's output byte-for-byte.

**Q: How do you prove chaos didn't corrupt anything?**
A: The demo runs the same workload twice — clean and under seeded chaos — and hashes
the sorted set of completed task outputs. Task IDs are excluded (they're random UUIDs
that legitimately differ); the guarantee is about work done. At kill-rate 0.9 we've
measured 300+ kills and 200+ requeues with a byte-identical hash to the clean run.

**Q: Chaos found real bugs? Give an example.**
A: Two. First, after a resume-skip, steps received their own previous output instead
of the previous step's output — the skip path didn't track chain position, so any task
that survived a kill mid-pipeline computed wrong downstream data. Only visible when a
kill landed between specific steps; the integrity hash caught it. Second, the original
demo tuned step latency (10ms) faster than the chaos tick (50ms), so runs finished
before the first kill fired — chaos was silently a no-op. Both are exactly the class
of bug chaos testing exists to surface.

**Q: How is chaos kept deterministic?**
A: Seeded RNG. Same seed + same workload = identical kill sequence. That converts
"chaos output == clean output" from a probabilistic hope into a hard assertion, and
means CI never flakes on wall-clock luck.

**Q: Why an explicit state machine for agent lifecycle instead of flags?**
A: Illegal transitions raise immediately (SPAWNED→DONE, DONE→anything), turning "how
did it get into that state?" debugging sessions into loud test failures. KILLED vs
FAILED is semantic, not cosmetic: KILLED means external force mid-work → requeue
expected; FAILED means the agent exhausted retries → dead-letter path.

**Q: Why did you hand-roll the scheduler instead of using Celery/asyncio primitives?**
A: The scheduler IS the product. A min-heap over (priority, seq) tuples gives priority
ordering with FIFO fairness within a priority level; an asyncio.Semaphore gives bounded
capacity with blocking backpressure (producers wait rather than drop work). That's ~60
auditable lines of stdlib. Celery brings a broker, serialization boundaries, and ops
overhead that would obscure the recovery story this project exists to demonstrate.

## Section 3: LLM providers & routing

**Q: How do you handle LLM provider failures?**
A: Two layers of fallback. Inside OpenRouterProvider, models are tried in health-sorted
order — an EWMA error score with recency weighting demotes failing models for future
requests automatically. Across providers, FallbackRouter tries each provider's whole
chain before giving up. Typical production config: [OpenRouter free chain, Mock] —
real model first, deterministic guarantee last, so the pipeline never hard-fails.

**Q: Why free models only on OpenRouter?**
A: Cost control by construction rather than by policy: every default model ends in
":free", enforced by a test. For this project's purpose — demonstrating orchestration
correctness under chaos — model IQ variance matters less than zero marginal cost and
unlimited CI runs. Paid models would add budget plumbing without strengthening the
core story.

**Q: Why is the mock provider deterministic, and why does that matter?**
A: Response text is derived from a SHA-256 hash of (prompt, temperature bucket) — same
input, same output, forever. This makes chaos-test integrity hashes comparable across
runs (a requirement for the Phase gate: chaos run output == clean run output), makes
tests free and hermetic, and lets me demo offline. Real providers can't offer this;
that's precisely why the interface abstracts them.

**Q: What does temperature actually do, and what do you set it to?**
A: It scales how randomly the model samples next tokens. Near 0 the model becomes
near-deterministic — right for extraction/QA where verifiers need reproducible
pass/fail decisions. Higher values trade determinism for diversity — right for draft
ideation. We default 0.7 for drafts and would pin ~0.2 for verifier stages. The mock
provider buckets temperature into its hash so tests exercise different-temperature
paths deterministically.

**Q: max_tokens — why cap it?**
A: Bounds latency and cost per call. Too low truncates structured JSON mid-field
(which downstream verifiers catch as schema failures); too high wastes tokens on
rambling. 512 comfortably fits our stage outputs.


## Section 4: Pipeline & harnesses (answered as built)

**Q: What exactly is a harness vs an agent vs a stage?**
A: Harness = a reusable capability bundle (tools + system prompt + loop policy) —
e.g. the LLM harness with temperature 0.2 for verifiers. Agent = a running instance
executing steps with a lifecycle and checkpoints. Stage = a node in the pipeline DAG:
a named pool of agents doing one kind of work, with a quality gate at its output.
Harnesses are the "what agents can do", stages are "where work flows", agents are
"who's executing right now".

**Q: Why does your DAG execute level-by-level instead of streaming per item?**
A: Two reasons, one discovered by a deadlock. Streaming needs end-of-stream signals;
with multi-worker pools only one worker ever received the sentinel and the rest hung
forever — my first design did exactly this. Level barriers make stage completion
well-defined (queue drained = done) and give clean per-stage gate reporting. Cost:
downstream waits for full upstream batches, negligible at our scale.

**Q: How does structured LLM output stay reliable on free models?**
A: Pydantic schema in the prompt, JSON extraction from the reply, validation, and one
repair round that re-asks with the validation error appended. Provider-agnostic — no
reliance on native function calling that free OpenRouter models may lack. After the
retry it fails loudly; silent garbage is worse than a dead-letter.

**Q: What happens when a verifier is wrong?**
A: Three layers of protection. A verifier exception is caught and treated as a failure
reason (never crashes the pipeline). The item goes through bounded repair, then to the
dead-letter queue WITH the verifier's reason attached — so a systematically broken
verifier shows up as a taxonomy spike ("schema" failures everywhere), visible in the
run report rather than silently eating items.

**Q: Why bounded repair loops?**
A: Unbounded repair is a livelock dressed up as diligence — a bad item never exits.
The bound converts every item into a terminal outcome: passed, repaired, or
dead-lettered with full context. All three are countable, which is what makes the
quality report honest.

**Q: How does HITL survive a process restart?**
A: AWAITING_APPROVAL is persisted through a store protocol, not held in memory. On
restart a fresh manager over the same store lists pending requests and the pipeline
resumes. Decisions are immutable — deciding twice raises — and the audit trail is
append-only, so the evidence chain can't be rewritten.

**Q: Semantic cache threshold — why 0.95?**
A: It's the precision/recall knob. Below ~0.9 you serve semantically adjacent but
factually different answers — a wrong cache hit is strictly worse than a miss because
it's invisible. Above ~0.98 you've rebuilt exact matching with extra overhead. 0.95
catches paraphrase-level duplication while keeping false hits rare.

## Section 5: Stores & budgets

**Q: Why is the Postgres store lazy-connecting?**
A: Construction never touches the network — importing/configuring it in tests is free,
and CI never needs Postgres unless a test explicitly exercises it. Connection happens
on first use; schema creation is idempotent.

**Q: What happens when the token budget runs out mid-run?**
A: BudgetExceeded raises immediately — fail-loud accounting. Callers convert that into
a clean PARTIAL run with a report showing what completed and what didn't. Silent
truncation mid-item would corrupt outputs while looking successful.

## Section 6: The v3 pivot (answerable NOW)

**Q: You had a working flagship and threw it away. Defend that.**
A: LeadOps ran, was tested, and produced a clean integrity hash. It also produced no
number worth defending. Its quality gates were shape checks - does the JSON parse, is the
score between 0 and 10 - dressed up as quality control. Nothing in that pipeline could
fail in a way that mattered. And the src/ tree did not change by a single line for the
pivot, which is the evidence for the kernel-purity claim I had been making on faith. The
kernel was the work; the flagship was about 700 lines. LeadOps is still in the repo with
its tests green, as proof the runtime is not shaped around one domain.

**Q: How is this different from AutoGPT or any other autonomous agent loop?**
A: The direction of the success dependency. An autonomous loop decides at the end whether
it succeeded, which is the model grading its own homework. Here criterion synthesis is
stage zero: N agents independently author a machine-checkable predicate, a red-team tries
to satisfy it with degenerate output, and if garbage passes, the criterion is rejected and
re-authored. Only then, and only against a frozen content-addressed criterion, does solving
begin. Some tasks fail before a single solve attempt, and that rate is a reported metric.

**Q: What if the swarm writes a bad criterion?**
A: That is the central risk and I do not claim it is solved. Three things bound it. The
adversarial pass has to fail to pass garbage before the criterion freezes. CI carries
seeded weak-criterion fixtures that must be caught. And the public eval arm has externally
authored ground truth, so systematic criterion weakness surfaces as a gap between the two
arms rather than hiding inside my own numbers. When synthesis cannot converge, a human
authors the criterion and the fallback rate is published.

**Q: "Self-learning" is the most over-claimed phrase in this field. Why should I believe you?**
A: Because I made the number unfakeable rather than arguing about it. Every model call,
gate outcome, containment and verified success writes an append-only ledger row, and every
reported figure is a query over those rows - no in-process counters, because agents are
selected on reported success and anything an agent can write, selection pressure eventually
teaches it to write dishonestly. On top of that, `swarmd eval` refuses to emit an
improvement figure without a paired control run: same tasks, same seeds, skills disabled.
If the confidence intervals overlap it prints "no measured improvement", in those words.

**Q: What does "learning" mean here - are you training anything?**
A: No, and I label it that way everywhere. Learning means a skill library of verified
sub-plans plus consolidated prompts with versioned rollback. Zero weight updates. Calling
retrieval-plus-prompt-editing "training" is exactly the kind of slippage the ledger and
control arm exist to prevent.

**Q: Why a budget economy instead of just picking the best agent?**
A: Because "best" needs a currency to be measured in, and paying on output rewards
verbosity. Agents hold an allowance, are paid only on verified success, go bankrupt when
they burn it without producing, and profitable strategies clone. It also gives the
cost-efficiency story real teeth: the run has a hard 0.05 USD ceiling, so selection
pressure and budget pressure are the same pressure.

**Q: Why 0.05 dollars? That seems arbitrarily tight.**
A: It is chosen rather than derived - roughly 180 paid calls at GLM 5.3 Flash rates. Tight
enough that the run cannot succeed by giving up and paying, so caching, batching and
cross-provider routing have to actually work. Loose enough to absorb overflow when free
tiers throttle. Raising it is a config change; the point is that a ceiling exists at all,
because without one the rationing engineering never gets finished.

**Q: Why did you drop the offline mock default you argued for in ADR-004?**
A: A dashboard fed by mock output looks pixel-identical to one fed by real output. That
turns a convenience into a way to accidentally lie, and v3's claims are about capability
and cost, which a schema-shaped stub cannot evidence. The mock survives in tests/ only,
where it still earns its place: proving byte-identical output under random kills needs a
deterministic generator. CI enforces the boundary with an import check.

## Section 7: Red-team and containment (answerable NOW)

**Q: Why is red-teaming a runtime component rather than a test suite?**
A: Because the failure mode is emergent. The economy pays on verified success and clones
what profits, so any gap between "looks successful" and "is successful" is not just
possible - selection pressure actively searches for it. A test suite runs before that
population exists. Five detectors tail the live action log: budget siphon, criterion
gaming, loop, unsafe tool call, library poisoning.

**Q: Doesn't running monitors constantly blow the budget you are trying to protect?**
A: They are pure code - signature matching, ratio checks, policy checks - with no model
call. Only genuinely ambiguous cases escalate to an LLM judge, capped as a fraction of run
budget. An organ that consumed the resource it exists to protect would be self-defeating.

**Q: What stops a wrongly-contained agent from losing its work?**
A: Containment calls the same kill path chaos uses, so it inherits checkpoint recovery and
requeue. That is the reason to reuse it rather than write a separate quarantine path - the
recovery semantics are already proven by every chaos test in the suite.

Worth adding that this sentence was false for a while, and the way it was false
is instructive. It was true of the kernel and not of the swarm flagship, which
wrote no checkpoints at all and simply restarted a killed node. Nothing caught it
because the integrity hash still matched - deterministic work redone hashes
identically to work recovered, so the chaos gate could not have distinguished
them. The test that covers it now counts provider calls instead of comparing
output.

**Q: What if agents learn to evade the detectors?**
A: Structurally, nothing prevents it, and I would rather say so than pretend otherwise.
The mitigations are that thresholds are not exposed to agents, that library poisoning is
judged against the control arm rather than self-reported metrics, and that a human approval
gate stands between a proposed skill and the library.

**Q: How do you know the detectors work?**
A: For a while I did not, and the way I found that out is the more useful answer.
There were 37 passing red-team tests, all built the same way: construct an
`Action`, hand it to a detector, assert on the detection. That proves a detector
parses its input. It says nothing about whether a rogue agent inside a live run
ever produces that input.

So the gate now seeds real rogues into a real run - real credits through the
economy, real code through the sandbox, reported through the same path an honest
worker uses - and nothing tells the red-team which agents are seeded.

Implementing it found `BudgetSiphon` had never been able to fire in production.
Its threshold was 7,500 credits; the economy hands each agent 2,000 and bankrupts
it at zero, so an agent with no verified success could not reach it. The unit test
passed because it constructed the detector with a lower threshold. The seeded
siphon went bankrupt uncaught, which is what surfaced it.

**Q: Your gate says five rogues were contained. What does that actually prove?**
A: More than it used to. The first version of the seeder asked "was the agent
stopped?", and the seeded budget siphon was stopped - by the LOOP detector,
because its payloads repeated and digits are masked before hashing. The gate
would have reported five detectors working while proving four.

It now asks the red-team which detector fired and requires it to be the one under
test. Containment by a different detector is a distinct outcome that fails the
gate. There is a fourth outcome too: `criterion_gaming` is usually refused by the
frozen criterion before the detector sees it, which is ADR-009 working - reported
in its own column rather than counted as a detection, and not failed either,
because a gate that fails when the criterion does its job would train me to weaken
the criterion.

## Section 8: Cost, quota, and the production floor (answerable NOW)

**Q: Your system spends money on every call. How do you stop it running away?**
A: A hard $0.05 ceiling per run, checked at the harness boundary so no call path
can bypass it, and checked twice - before issuing with a conservative estimate,
and again once real usage is known. Discovering a breach only after the tokens
are spent means the ceiling was advisory. A breach raises with the itemised
report attached and the caller turns it into a cleanly aborted run, never a
truncated one, because a truncated run still emits numbers that look like
results.

**Q: Why does the cost ledger record cache hits as zero-cost rows instead of just
skipping them?**
A: So "what did the cache save" is a query rather than an estimate. Each hit row
carries what the call would have cost at that model's price, so cache savings
sum out of the same table as spend. Skipping them would mean the only honest
answer to "was the cache worth it" is an educated guess.

**Q: You say there is no counter. How is that enforced rather than just intended?**
A: A test appends a row directly to the ledger, bypassing the accountant
entirely, and asserts the reported total moves. A counter implementation fails
that test. The reason it matters: agents are selected on reported success and
paid on verified success, so anything an agent can write, selection pressure
eventually teaches it to write dishonestly. Removing the capability is cheaper
than policing it.

**Q: What happens if a model is not in your price table?**
A: It raises `UnpricedModel` and the call does not happen. Defaulting to zero
would silently disable the ceiling, which is the single control between a run
and an unbounded bill. The failure mode of being loud here is a startup error;
the failure mode of being quiet is a surprise invoice.

**Q: What did empirical rate-limit discovery find that the published limits got
wrong?**
A: OpenRouter's daily cap is documented as 50, 200, and 1000 by different
sources on the same day. That disagreement is the entire argument for treating
published limits as an ordering hint and letting an actual 429 be the
authoritative statement. `Retry-After` wins over our own backoff because it is
the provider telling us its limit; exponential backoff is only the fallback for
providers that say nothing.

**Q: Three pods, one Groq key. What happens?**
A: With naive in-process limiting, each pod politely holds itself to the account
rate and collectively they present three times it, get everything throttled, and
then each interprets a self-inflicted 429 as evidence about the provider and
tightens against a problem it caused. That is ADR-011. Quota moves to Redis,
evaluated as one Lua script because check-then-decrement across two round trips
is a race that is not rare under hundreds of agents, and refilled against the
Redis clock because pod clocks drift.

**Q: What happens when Redis is down - do you fail open or closed?**
A: Neither, deliberately. Pods degrade to local buckets at 25% of the real rate.
Fail-open would produce exactly the stampede the quota exists to prevent.
Fail-fully-closed would turn a Redis blip into a total outage of a system whose
whole premise is graceful degradation. 25% is sized so a plausible number of
blind pods still sums to under the account limit.

**Q: Why a token bucket and not a fixed window?**
A: A fixed window permits spending the entire minute's allowance in its first
second. That reads to the provider as a burst and earns a 429 despite a
perfectly legal average. Continuous refill is what "30 requests per minute"
actually means to the thing enforcing it.

**Q: Two API keys for the same provider - does that double your throughput?**
A: Yes, and the pool models it: one slot and one quota bucket per credential,
because quota is per account. Keying on provider name alone would throttle two
accounts as one and discard half the capacity. Worth adding: creating multiple
accounts specifically to evade a rate limit violates most providers' terms, so
the capacity answer I would actually give is cross-provider pooling plus a 60%
cache hit rate, which gets the same multiple legitimately.

**Q: Prometheus or the ledger - which one is the truth?**
A: Neither, and the split is deliberate. Prometheus is for OPERATING the system:
it may lose a scrape, and its counters reset when a pod restarts, which is fine
for "is cost climbing right now". The ledger is for CLAIMS and does neither. If
a dashboard and BENCHMARKS.md disagree, the ledger is right and the dashboard is
stale. That sentence is at the top of the metrics module.

**Q: Why is your metrics module on its own registry instead of the default?**
A: swarmd is a library as well as a service, and a host application that also
uses prometheus_client would collide with us on the global registry - a
duplicate-timeseries error at import time, which is the worst possible place to
find out. It also made the module reloadable, which is what let me actually test
the no-prometheus fallback rather than assume it.

**Q: How do you stop metric cardinality from exploding?**
A: A test asserts that no metric declares run_id, task_id, or agent_id as a
label. One unbounded label on a busy counter kills a Prometheus instance and is
not recoverable without dropping the series, so it is enforced rather than
documented. High-cardinality identity goes in traces and the ledger, which are
built to hold it.

## Section 9: Kubernetes and production operations (answerable NOW)

**Q: Why is a run a Job rather than a Deployment?**
A: A run has a beginning, an end, and a result - that is a batch workload. As a
Deployment the controller would restart a finished process forever, and "did
this run succeed" would be a question you answer by reading logs instead of by
reading a Job status.

**Q: So does it scale horizontally?**
A: More concurrent runs, yes. A faster single run, no - a run stays in one pod.
Distributing one run needs a distributed scheduler I have not built, and PRD
section 6 lists it as a v1 non-goal. The thing that genuinely does not survive
naive horizontal scaling is provider quota, which is why Redis is in the
architecture at all.

**Q: backoffLimit is 0. Why not retry failed runs?**
A: Re-running burns provider quota, which is the scarcest resource in the
system, and the ledger already holds everything needed to diagnose the failure.
Recovery WITHIN a run is what the checkpoint system is for; retrying an entire
run is a human decision, not a controller's.

**Q: You put runs on Spot instances. Isn't that asking for trouble?**
A: The product claim is that agents can be killed and work is not lost. Refusing
to run on Spot would be an odd lack of confidence in my own guarantee. A Spot
interruption is just another chaos event, and Karpenter gives a two-minute
drain. Control plane and Redis stay on-demand - an interrupted control plane is
a blip, an interrupted Redis is a quota-coordination gap.

**Q: Every container has resource limits except the control plane's CPU. Why?**
A: CFS throttling on a latency-sensitive asyncio event loop produces tail-latency
spikes that look exactly like provider slowness, which sends you debugging the
wrong system entirely. The request reserves the floor and the namespace
ResourceQuota caps the top, so it is bounded - just not per-container.

**Q: Why does Redis use the Recreate strategy instead of RollingUpdate?**
A: Two Redis pods behind one Service would split the quota buckets in half and
each half would permit the full rate - precisely the over-permissiveness the
quota exists to prevent. A few seconds of unavailability during a deploy costs
one degraded window; two pods costs a throttled account.

**Q: Your readiness and liveness probes hit different endpoints. Deliberate?**
A: Yes. Readiness goes false when the provider pool has no capacity, so a
saturated pod stops receiving NEW runs without being killed and losing the ones
it already holds. If liveness used the same signal, saturation would restart the
pod and destroy in-flight work - turning a capacity event into a correctness
event.

**Q: How do you keep credentials out of the cluster?**
A: External Secrets Operator syncing from AWS Secrets Manager, with IRSA so no
static AWS credentials exist in the cluster either. The prod overlay deletes the
placeholder Secret rather than leaving it blank, so a misconfigured ExternalSecret
fails loudly at pod start instead of quietly mounting empty keys and presenting
as a provider outage. And the egress NetworkPolicy blocks 169.254.169.254
specifically, because the metadata endpoint is the standard path from "code
execution in a pod" to "cloud credentials".

**Q: You run chaos in production. Justify that.**
A: Turning it off would make production the one environment where the recovery
guarantee is never actually tested. That is exactly backwards - the guarantee
matters most where the work is real.

**Q: What is your most uncomfortable production number?**
A: Infrastructure costs roughly 5,600 times more than inference - about $280 a
month against effectively $0 of LLM spend. If the goal were minimising cost,
this belongs on Fargate or a single VM. It is on EKS because the goal is
demonstrating that I can operate the thing, and I would rather say that plainly
than pretend the architecture is cost-optimal.

**Q: Why is "agent success rate" not an SLO?**
A: Because making it one would create pressure to weaken the frozen criterion.
The system would then hit its SLO by grading itself more generously, which is
precisely the failure ADR-009 exists to prevent. Capability belongs in
`swarmd eval` with a control arm; SLOs are for reliability. Correctness under
chaos IS an SLO, at 100% with no error budget, because a budget there would
imply an acceptable rate of silently losing work.

**Q: You banned mock data, then shipped a fake provider. Explain.**
A: ADR-006's concern was never that synthetic data exists - it was that
synthetic data is indistinguishable downstream. It enforced that by keeping the
mock out of certain code paths, which is a property of code organisation and one
refactor away from being false. ADR-012 moves the fence onto the data: every
ledger row the simulated provider writes carries simulated=true, any report
aggregating one is itself tainted, and refuse_simulated() raises before anything
publishes a number. The ban on reporting from synthetic data did not weaken - it
went from convention to a raise. What changed is that I can build Phases 6 to 10
before a credential exists.

**Q: Why is taint on the row rather than a run-level config flag?**
A: Because it has to survive a process restart and a copied file. A run-level
flag lives in memory; a ledger read back next week by a different tool would
have no idea. As a column in append-only JSONL, the artefact carries its own
provenance wherever it goes.

**Q: What if someone just removes the flag?**
A: Then they have made a deliberate, visible code change that fails the tests
asserting the taint propagates. I would not claim it is tamper-proof - anyone
with commit access can delete any control. The property I am claiming is that
presenting synthetic data as real requires a decision rather than an accident,
and forgotten environment variables are the accident I was actually worried
about.

**Q: Your fake provider is slow and sometimes fails on purpose. Why?**
A: A fake that answers instantly hides every concurrency and backpressure bug
the scheduler exists to handle, and one that never fails leaves the fallback
chain, the repair loop, and dead-lettering completely unexercised. That is
exactly how a system passes all its local tests and falls over the first time it
meets a real API. 50ms and a seeded, deterministic failure rate.

## Section 10: The loop, end to end (answerable NOW)

**Q: Walk me through what happens when I give this an unfamiliar task.**
A: Nothing solves anything first. Three agents independently author a
machine-checkable success criterion from different angles - what artifact must
exist, what would prove it was NOT done, what number settles it objectively.
Their proposals are merged on majority agreement; if they share no check at all
that is escalated rather than resolved, because disagreement about what success
means is information. A red-team then tries to satisfy the merged criterion with
empty output, constant output, repeated tokens, the prompt echoed back, and
zero-valued artifacts. If any of those pass, the criterion is rejected and
re-authored. Only once something survives does it get content-addressed and
frozen, and only then does planning start.

**Q: What if it never survives?**
A: The task fails with no solve attempt, and the reason is recorded. That is the
correct outcome: proceeding would grade results against a criterion known to be
weak, and every downstream number would inherit the lie. When I first wrote the
end-to-end test I gave it a criterion of `output_nonempty` plus
`min_distinct_words` and the run kept failing - because echoing the task passes
both. The system was right and my test fixture was wrong.

**Q: Why is the plan generated rather than written?**
A: A hand-drawn pipeline means the task was one I already scoped. Three agents
propose decompositions, each is structurally validated before anything runs -
acyclic, dependencies resolvable, every node reachable from a root, bounded size
- and invalid ones are discarded rather than executed hopefully. The winner is
picked on computable properties: width, because wall clock is the scarce
resource; brevity, because each node costs at least one call; and whether
instructions name an artifact rather than describe an activity. Not judged by a
model - that would be a second opinion with no ground truth costing a call per
proposal.

**Q: The generated plan runs on the same executor as everything else?**
A: Yes, and that was the point of writing the Phase 2 executor to take stages as
data. There is exactly one execution path. A parallel path for generated plans
would mean the thing I tested and the thing that runs are different.

**Q: What actually makes a 500-agent run finish in fifteen minutes?**
A: Four levers, and the first is by far the largest. Because the criterion is
executable code rather than prose, verification costs zero model calls - a naive
design would spend one per check and that alone is 5,000 calls. Then batched
generation returns K variants per call, the semantic cache absorbs the
near-identical prompts population search produces, and the red-team detectors are
pure code. Without those, 45 requests a minute means three and a half hours.

**Q: How do you know chaos is not quietly losing work?**
A: The integrity hash. It is order-independent, because chaos changes the order
work completes in and must not change the result, and it excludes contained
work. CI runs the kernel demo at kill-rate 0.9 and requires byte equality with
the clean run - 595 kills, matching hash. That is SLO-2, and it has no error
budget: a budget there would imply an acceptable rate of silently losing work.

**Q: Something interesting from that number?**
A: 595 kills against 404 requeues, with the hashes matching. My own alert
compared those two counters and would have paged during entirely healthy chaos.
An agent killed while idle has no claimed work to requeue, so the gap is normal.
The alert now fires only on requeues at exactly zero while kills continue, which
is unambiguous.

**Q: Your fake provider drove the whole system. Isn't that testing itself?**
A: It drove the ORCHESTRATION, and every row it produced is marked simulated -
the eval harness refuses to generate BENCHMARKS.md from it and the dashboard
shows a banner. What it proves is that the loop runs, chaos recovers, containment
works, and cost is bounded. What it explicitly does not prove is capability, and
the README says so rather than showing a curve.

**Q: What is the weakest part of this?**
A: The capacity plan assumes a 60% cache hit rate and that assumption is
untested on real unknown-task workloads. If it comes in at 30% the demo profile
no longer fits its wall-clock budget and every profile has to be re-derived. It
is listed in CAPACITY.md section 7 as the assumption most likely to be wrong,
with the metric that would falsify it.

**Q: Sell me on one design decision you would defend hardest.**
A: That criteria are a declarative check language rather than model-written
Python. The obvious implementation is to have the model emit a check function
and exec it. That would be inspectable only by reading generated code, would
hash differently for a renamed variable so criteria could not be compared across
runs, and - the real problem - would execute model-written code to decide
correctness in a system whose agents are selected on passing that check. The cost
is that expressiveness is bounded and a genuinely novel predicate needs a new
check kind. I will take that over running arbitrary model output as the arbiter
of truth.

## Section 11: Operations (answerable NOW)

**Q: Your CI has seven jobs. Why not one?**
A: They are split by what a failure MEANS. A hermetic test failure is a code
regression and blocks. The live smoke run against real providers is
informational and continues on error, because its failure is a provider outage.
A red build people learn to ignore is worse than no build.

**Q: What did you find when you first ran CI?**
A: That it had never run. The workflow triggered on `main`; this repository's
default branch is `master`. It had been green-by-absence since the repo was
created, which is the worst state for a pipeline to be in because it looks fine.
`mypy src` had also been failing on a pre-existing type error nobody saw.

**Q: You run chaos in production. Defend it.**
A: Turning it off makes production the one environment where the recovery
guarantee is never tested. The guarantee matters most where the work is real.

**Q: Talk me through a page for ProviderPoolExhausted.**
A: Every provider backed off for two minutes, so runs are blocked. First
`swarmd providers probe`, which distinguishes the four causes in the order they
actually occur: credentials rotated (note that envFrom is fixed at container
start, so even a successful rotation needs a restart), genuine daily quota
exhaustion, Redis lost so quota degraded to 25% local buckets, or egress
blocked. Individual providers backing off is routine; all of them at once
usually has one common cause.

**Q: What is your most uncomfortable production number?**
A: Infrastructure costs about 5,600 times more than inference - roughly $280 a
month against effectively zero LLM spend. If minimising cost were the goal this
belongs on Fargate or one VM. It is on EKS because the goal is demonstrating I
can operate it, and I would rather say that than pretend the architecture is
cost-optimal.

**Q: Is this production ready?**
A: No, and docs/PRR.md says which items block and why. Three things: no
live-provider validation at 500 agents, so the capacity assumptions are
untested; no on-call rotation, and a system with paging alerts and one
maintainer has alerts rather than on-call; and the egress policy allows 443
anywhere, which is the loosest control shipped. It is ready for a demo and
evaluation deployment. The operational work is further along than the empirical
work, and that ordering was deliberate - building the curve before the apparatus
that makes it trustworthy is how unfalsifiable claims get made.

## Section 10: Unknown-task runs (populate as Phases 6-7 land)

**Q: What broke first at 500 agents?**
A: (to fill - keep a raw failure log; this question decides the interview)

**Q: Show me a synthesized DAG that was wrong, and what caught it.**
A: (to fill - structural validation rejections, with examples)

**Q: What did the cache hit rate actually turn out to be on unknown tasks?**
A: (to fill - the capacity plan assumes 60%; this is the assumption most likely
to be wrong, and the wall-clock budget depends on it)

## Section 12: Capacity levers, measured (answerable NOW)

**Q: Your capacity plan claims a 500-agent run fits fifteen minutes. Does it?**
A: The two levers doing the heavy lifting exist now and are verified by counted
provider calls: a 32-agent run over a four-node plan issues 10 calls — 6 of
synthesis plus one batched generation call per node — and a second identical run
issues 4, because the memo serves the criterion and the plan. (This line said 8
for the cold figure until it was re-counted; the always-paid synthesis head is 6
on every shipped profile and does not shrink with the plan. The repeat figure of
4 was right.)
For a while neither lever existed while the document counted both, and the pool
was capped at 16 to hide the consequence.

What I would not claim yet is the hit rate on real workloads. Exact keying means
genuinely novel tasks hit near zero - identical prompts are what hit, and unknown
tasks do not repeat. The 100% measured on a repeated run is the ceiling, not the
expectation. Batching is what carries the plan, because it does not depend on
repetition at all.

**Q: How do you batch K candidates without a coalescing broker?**
A: I do not coalesce. The run generates the batch before spawning the pool and
pre-seeds each agent's checkpoint with its variant, marked as a completed
`generate` step. The worker's resume path - already built for chaos recovery -
skips it and charges nothing.

That was the useful realisation: batching and recovery are the same operation seen
from two sides, namely work someone else already did. A broker would have needed a
batching window, a timeout, and a policy for when fewer callers arrive than
expected. Three sources of flakiness, replaced by an ordering constraint.

**Q: You put a semantic cache in front of the provider. What went wrong?**
A: It served one plan node's answer to another. Worker prompts share a long
template and differ in a step name, so cosine similarity between three genuinely
different nodes measured 0.97 - above the 0.95 threshold. The symptom was a fast,
cheap run at a high hit rate whose nodes all produced the same artifact.

The fix is not a higher threshold. Similarity here is dominated by shared
boilerplate and rises with template length, so a longer envelope pushes any two
prompts above any threshold. Semantic matching assumes the varying part of a
prompt is most of the prompt, which is true of human paraphrases and false of
machine-assembled ones. The cache is exact-keyed now, and the wrapper refuses a
similarity cache rather than warning about it.

**Q: What else did adding a cache nearly break?**
A: The plan proposers. Three proposers were sending an identical prompt and
relying on sampling for variety, so a cache in front of the provider turned three
competing DAGs into one drawn once and copied twice - with the plan selection
reporting a clean winner over two copies of itself.

Fixed twice over, deliberately. The proposers now decompose under different
priorities, which is a better design regardless of caching, and both proposer
paths set `cache: bypass` so a future edit that unifies the prompts cannot quietly
reintroduce it.

**Q: Why can an eval run not use the cache?**
A: Because an eval measures variance across repeats. Serving repeat 2 from repeat
1 does not bias the bootstrap interval, it collapses it toward zero width - and a
zero-width interval reads as a strong result. `SwarmRun` raises rather than
accepting a cache on the eval profile. It is enforced rather than documented,
because this is exactly the mistake someone makes while trying to speed up a slow
eval.

**Q: Anything else the cache surfaced?**
A: Two bugs in adjacent code, both of the same shape - an error handler that was
too broad.

An unpriced model turned a $0 cache-hit row into an exception, the batch caught it
as a provider failure, and batching silently disabled itself. A cache hit cannot
move the ceiling, so refusing it protected nothing; `charge_call` still raises,
because there the money is real.

And a `CeilingExceeded` raised inside a batch fell into the generic handler, so
the pool fell back to individual generation - spending more, one call at a time,
past the limit that had just fired. That one is worth stating plainly: a
`except Exception` around a call that can breach a budget will eventually swallow
the breach.

**Q: Why let the operator exceed your own advisory pool cap?**
A: Because a cap the operator cannot override is a lie about who is in control,
and the cost ceiling is the real protection either way - the run aborts on budget
with an itemised report rather than silently costing more than it was allowed.
What the operator gets instead of a veto is a `pool_above_advisory` event naming
the reason, so an expensive run is a decision rather than a surprise.

The hard bound at 64 per node is a different concern: it is not about cost, it
bounds concurrent in-flight work so one level cannot turn a rate limit into a
thundering herd.

**Q: The supervisor rewrites prompts. What stops it making things worse?**
A: It does not decide whether its own patch helped. It proposes from the
criterion's failure taxonomy; the consolidator, which measures the control arm,
gates it. Every patch is a hypothesis measured against the pass rate before it,
and one that did not help is reverted - otherwise prompts accumulate constraints
forever, each individually plausible, with nobody able to say which are
load-bearing.

It is off by default, which is not timidity: a patched prompt is a confound, so an
eval arm has to be able to run with the stock prompt and know that is what it ran.

**Q: If I gave you one more week, what would you do?**
A: Nothing on this list, because the list is not the constraint - credentials are.
Everything here has run against a simulated provider, so three numbers are
unmeasured and each could come back worse than hoped: the real cache hit rate,
whether a real model asked for eight distinct approaches returns eight or returns
three and five rewordings, and the learning curve. Until those exist with their
control arm, the project does not claim the system improves, and STATUS.md says so
in those words.

The structural work I would do is unify the kernel `Runtime` and the swarm
executor. They share the `Checkpoint` contract but not the loop, which is a real
duplication with a real cost, and I would rather name it than let it read as an
oversight.

## Section 13: Not paying twice — idempotency, the memo, the prompt prefix (answerable NOW)

### Idempotency

**Q: I double-click submit. What happens?**
A: Nothing, if you sent an `Idempotency-Key`. Same key with the same body
returns 200 with the ORIGINAL run_id and `Idempotent-Replay: true`, and no
`SwarmRun` is constructed at all — that last part is what a test asserts,
because returning the right id while quietly starting a second population would
look identical from outside. Without the header you get two runs, deliberately.
`POST /api/runs` answers 202 and starts a background task, so a dropped
response, a proxy timeout or a retrying CI job otherwise buys two populations,
two criteria, two plans and twice the quota for one question. On the ~2,200
plannable requests/day in `docs/CAPACITY.md` section 7 — which section 1
declares authoritative over its own supply table, and which `swarmd providers
budget` currently prints as `plannable 2,195 requests/day` — a duplicated
`standard` run at ~90 calls is a meaningful bite out of the day. (`~1,146` is
the pre-recount figure; grepped, it survives in comments in `router/budget.py`,
`server/app.py`, `server/idempotency.py` and `swarm/run.py`, and in the dated
entries in `flow.md`, and it is not the current budget.)

**Q: Why not just hash the body and dedupe automatically?**
A: Because re-running an identical task on purpose is a normal operation here —
an A/B arm, a flake hunt, a chaos comparison — and a body-hash fallback would
hand an operator who forgot the header yesterday's run id instead of the run
they asked for. "No header means a new run" is unconditional, with no
environment flag to weaken it. The cost is that clients must opt in; the
alternative silently breaks the one workflow this project runs most.

**Q: Same key, different body?**
A: 422, no run started, and the response never contains the first run's id.
That last constraint is deliberate: a conflict is a client bug, and leaking the
id of a run the caller did not create turns a bug report into an information
disclosure. A malformed key is 400 — the key must be 8–200 characters of
`[A-Za-z0-9_.:-]`, and the eight-character floor is not cosmetic, because a
client that "just picks something" collides across unrelated requests and a
collision here hands one caller another caller's run.

**Q: What is the scope of a key, and how long does it live?**
A: Global to the pod's store, 24 hours, and it also expires early if the run it
points at is gone from the RunStore — whichever comes first. The sweep runs at
startup beside `run_store.prune()`. Twenty-four hours covers every retry anyone
actually makes (an HTTP retry budget is seconds, a CI re-run is minutes, an
operator re-issuing a curl is hours) and is short enough that the same key
reused next week for a different question is treated as new rather than
replaying something unrelated.

**Q: Does it survive a restart?**
A: Yes, and that is the case it exists for. Records are one file per key under
the RunStore root, written with RunStore's exact discipline — temp file in the
same directory, fsync, `os.replace`. The retry that arrives after a deploy is
precisely the retry worth deduplicating, so an in-memory dict would have
covered only the cases that did not matter. One detail worth mentioning because
it was a real bug in waiting: RunStore's filename sanitiser drops `.` and `:`,
which `KEY_RE` allows, so two distinct keys would have shared a file. A sha256
suffix on the filename fixes it.

**Q: What about the race between two simultaneous requests with the same key?**
A: A per-key asyncio lock closes the check-then-create window, and a
construction that fails calls `release()` so a retry is never stuck behind a
phantom "pending" reservation. A pending record older than 120 seconds is
disbelieved, because that can only come from a process that died mid-construction
and holding a key hostage forever after a crash is worse than the small chance
of a duplicate.

**Q: Three pods behind one ingress?**
A: It can still double-accept, and I would rather say so than imply otherwise.
The lock and the file store are per-pod; a stat and a write are not atomic
across machines. It is stated in the module docstring rather than buried, and
`IdempotencyStore` is a deliberately narrow surface —
lock/get/reserve/complete/release/prune — so a Redis or Postgres backend drops
in behind it. NOT DONE, listed as a follow-up.

### The run memo

**Q: The owner said "if it learns a task, the next time a similar one comes in
it should fire instantaneously." Does it?**
A: Partly, and "instantaneously" is the word to take away first. A memo hit
skips SYNTHESIS ONLY. The workers still run, every node is still executed
against the real current task, and every candidate is still graded — so the
wall clock of a memo-served run is dominated by exactly the work a cold run
does, minus a serial head.

The precise version. Every run opens with a serial, always-paid head:
`profile.proposers` calls to author a criterion, the same number again to
author a plan, and nothing else may start until both freeze. `proposers` is 3
on every shipped profile, so that head is 6 calls. Measured with the
call-counting provider in `tests/swarm/test_memo.py`, a cold `smoke` run of its
two-node fixture issues 8 provider calls in total (6 synthesis, 2 batched
generation) and the repeat issues 2; swapping in a three-node plan gives 9 and
3. Batched generation issues one call per plan node, not one per agent, so the
pool size does not multiply it, and a repair round adds calls on top. So the
honest headline is "6 calls, out of 8 or 9 plus repairs" — the numerator is
fixed by the profile and the denominator is set by the plan, which is why I do
not quote a single percentage.

A memo carries a criterion and a plan. It does not carry candidates, outputs,
artifacts or a verdict — by construction there is no stored answer in it to
serve.

**Q: So a memo hit could still fail the run?**
A: Yes, and that is the point. The worst case of a bad memo is a run graded
against a criterion it would probably have written itself. The worst case of an
answer cache is a run that reports success without doing anything, which is why
this is not one.

**Q: How similar is "similar"? Where is the threshold?**
A: There is no threshold, and that is a decision rather than an omission. The
key is exact: strip, collapse internal whitespace, casefold, hash. No embedding,
no token overlap, no cosine. This repo already has the counter-example in
`router/cache.py` — three genuinely different plan nodes measured 0.97 similar
against a 0.95 threshold, so one node's answer was served to another and the run
reported a high hit rate while being wrong. Similarity on machine-assembled text
is dominated by shared boilerplate and rises with template length rather than
with sameness. A paraphrase therefore MISSES the EXACT key, which is cheap next
to grading one task by another task's definition of done.

What happens to it next is the near tier, and it is worth being exact rather
than saying "and then it pays six calls". Measured end to end, running the
`smoke` fixture twice over a shared memo store and counting synthesis calls:
`"Compute the total cost of 3 pens at 1.25 dollars each"` followed by
`"please compute the total cost of 3 pens at 1.25 dollars each for me"` gives
different exact keys, the same `abstract_fingerprint`, and a second run that
pays **0 synthesis calls** with `criterion_memo_revalidated` emitted and
`served_from` naming the first run. `"summarise the paper"` and `"give me a
summary of the paper"` have different fingerprints, because `summarise` and
`summary` are different action tokens, so that one really does pay in full.

Now the asymmetry that example is one capital letter away from, because I had
it wrong here and the honest version is more interesting than the tidy one.
Capitalise the politeness word — `"Please compute the total cost of 3 pens at
1.25 dollars each for me"` — and the same run pays all **6** synthesis calls
and emits `memo_refused` with `"task literals do not line up with the stored
task"`. The fingerprints still match. What does not match is
`literal_map(stored_task, this_task)`, which scans the RAW task text, where
`TERM` is a literal kind matching a capitalised word. `task_shape` casefolds
before abstracting, deliberately, so TERM never fires on a task and a re-typed
capital cannot mint a second shape. `literal_map` does not casefold, so
`_scan("Please compute ... ")` returns `[TERM 'Please', NUMBER '3', MONEY
'1.25 dollars']` against `[NUMBER, MONEY]` for the stored task, the kind
sequences differ, and it returns `None`. Sentence-initial `Compute` escapes
because `_is_method_phrase` excludes method vocabulary from TERM; `Please` is
not method vocabulary.

That is a refusal, not a wrong answer — the run pays for its own criterion —
but it means the near tier's reach is narrower than "casefolding makes
paraphrases match", and a doc that claimed the capitalised example worked was
claiming something that does not execute.

The near tier is still a discrete equality on task SHAPE, never a continuous
score, so it does not reopen the door this answer just closed — it adds a
second exact key, not a threshold on the first.

**Q: What about the near-match tier?**
A: It is shipped, and its notion of "match" is worth being precise about,
because it is neither the exact tier's `normalise`/hash nor the skill gate's
signature.

The key is `generalise.abstract_fingerprint` — `TaskShape.fingerprint`, which
hashes three things: the ORDERED sequence of literal kinds, the sorted set of
method vocabulary, and the COUNT of distinct subject nouns. Note what is
absent: the subject nouns themselves. That is exactly the difference from
`task_signature`, the skill gate's key, which hashes the sorted set of literal
KINDS plus the sorted set of SUBJECTS. Pens and pencils are two signatures
(two pieces of evidence, because they are two questions) and one fingerprint
(one shape of work, rebindable from either onto the other). Both are discrete
equality tests on a sha256, never a continuous score, so neither can repeat the
0.97-above-0.95 mistake `router/cache.py` documents. Two consequences worth
knowing: reordering the literals changes the fingerprint (the sequence is
ordered, deliberately, because positional rebinding is only well defined when
the kinds line up in order), and a synonym swap in the method verb changes it
too.

`MemoStore.by_fingerprint` is a scan over `entries()` filtered to that
fingerprint, sorted by `updated_ts` descending, excluding this task's own key.
Nothing is served on the match alone. `_criterion_from_near_memo` runs
`literal_map(stored_task, this_task)` — positional and kind-checked, `None` if
the two do not line up — then `rebind`s the stored criterion's literals, then
runs the subject-leak guard, then `malformed()`, then RE-ATTACKS the rebound
criterion against the new task's text before trusting it, exactly like the
exact tier. `_plan_from_near_memo` rebinds and revalidates through
`planner.validate()`.

The subject-leak guard, stated as it actually is rather than
unconditionally. `rebind` rewrites URL/PATH/DATE/MONEY/PERCENT/QUOTED/NUMBER
and deliberately not TERM, so an ordinary subject noun survives verbatim. That
is fine in plan text a human reads and wrong in a check PARAMETER a grader
compares byte-for-byte. So `leaked_subject_terms(rebound_text, source, target)`
first computes the SOURCE's subject stems minus the TARGET's; if that set is
empty it reports nothing at all, which is correct — a criterion rebound from
"3 pens" onto "1 pen" keeps the word "pens" and should be allowed through,
because pen and pens are the same subject. Only when the source has a subject
the target lacks does the guard look, and it compares STEMS, so a pens-derived
criterion rebound onto a pencils task is caught whether the surviving parameter
spells it "pens" or "pen". Verified by calling the function on that pair
directly: `['pens']` and `['pen']` for the two spellings, and `[]` for the
same-subject pair, for a method word like "cost", and for a word that belongs
to the target task.

A rebind that fails any of this is REFUSED (`_near_refuse`), not deleted — the
failure is a property of the PAIRING of two tasks, not of the stored entry,
which stays exactly as good for its own task or the next one that rebinds
cleanly.

**Q: An exact memo is refused. Does the near tier get a turn?**
A: Only if the refusal happened at ADMISSION. This is a real boundary, it is
not written down anywhere else, and I had the general version of it in this
document without the exception.

`run()` computes both lookups up front, in this order:

```python
memo = self._memo_lookup(task)
near_memo = self._near_memo_lookup(task) if memo is None else None
```

`_memo_lookup` returns None for a miss and for an ADMISSION refusal — the
provenance run never completed, the entry is past `MEMO_MAX_AGE_S`, or the run
store's own document disowns it. In those cases `near_memo` is looked up and a
different entry of the same shape serves the run. Measured: refuse a pencils
memo by marking its provenance run `interrupted`, with a valid pens memo of the
same fingerprint in the store, and the run pays **0** synthesis calls,
`criterion_memo_revalidated` fires, and `served_from` names the pens run.

But when admission PASSES, `_memo_lookup` returns the entry, `near_memo` is
already None, and the deep revalidation that follows — `_criterion_from_memo`
re-parsing, re-hashing, calling `malformed()`, and re-attacking — has no near
attempt to fall back to. Measured on the same fixture: store a pencils memo
whose criterion now loses to `attack` (only degenerate checks), keep the valid
pens memo alongside it, and the run pays all **6** synthesis calls. One
`memo_refused` on the exact tier, no `criterion_memo_revalidated`, and the pens
memo — which would have rebound cleanly — is never consulted.

I do not think that ordering is obviously right; the pens memo was there and
was good. It is what the code does, and the reason is the comment above those
two lines: both lookups are taken at one instant so the criterion and the plan
a run reuses come from the SAME provenance entry, and re-running the near
lookup after a deep refusal would reopen that. Naming the cost is better than
implying the fallback is universal.

What is excluded from the near lookup is only the refused DOCUMENT, via
`by_fingerprint(fingerprint, exclude_key=key_for(task))` — never the near
mechanism itself.

Still not built, and to be clear this is a design of mine and not a written
requirement -- `docs/SPEC.md` has no memo section: a fuller replay tier — stored worker results, an
environment closure over system/skills/grader/sandbox digests, hermeticity and
volatility gates. Nothing here replays a candidate, so those gates would guard
a path that does not exist.

**Q: How do you stop a stale memo from poisoning a run?**
A: A chain of gates in three places, and a miss is always cheaper than a wrong
hit. In load order, with what each one does to the entry:

`MemoStore.get` — (1) a document written by an older schema raises
`IncompatibleMemo` and is IGNORED, neither quarantined nor deleted, because
version skew is not tampering and it is the reader that is new. (2) The entry's
own `entry_hash` must equal `content_hash()` of its payload, and (3) the stored
criterion must still hash to its recorded `criterion_hash`. Either mismatch is
QUARANTINED into `memos/quarantine/` — moved aside, never deleted, because a
document that does not hash to its own contents is evidence that something
which is not this code changed it.

`TaskMemo.reusable` — (4) `status` must be `completed`, (5) a criterion and its
hash must both be present, (6) the entry must be younger than
`MEMO_MAX_AGE_S` (30 days). Each returns a REASON STRING rather than a bool,
because a memo that is never reused is otherwise indistinguishable from one
that never existed, and `memo_miss_reason` is the number to watch rather than
`memo_hit_rate`.

`SwarmRun._memo_admission` — (7) a second opinion from the run store: if the
run document for `entry.run_id` still exists and its status is not `completed`,
the entry is refused AND DELETED.

`SwarmRun._criterion_from_memo` / `_plan_from_memo` — (8) the criterion must
re-parse, (9) re-hash, (10) pass `malformed()`, and (11) be RE-ATTACKED against
THIS run's task text under today's attack set — not trusted because it survived
an attack last month, and the attack is pure code so it costs no provider call.
(12) The plan must revalidate through `planner.validate()` and (13) hash-check.
Anything failing here is DELETED unconditionally; none of them get better on a
second read. (Gate 9 is defensive in practice: `get` has already quarantined a
criterion whose hash does not match, so a run reaching the delete branch for
that reason would mean the entry arrived from somewhere other than the store.)

The one that took a bug to get right is (7), and it is the only gate that
deletes on a PROVENANCE fact rather than a document fact. A memo whose OWN
`status` field still says `completed` but whose run-store document disagrees —
a control-plane shutdown marking it `interrupted` after the fact, say — is
deleted rather than quarantined, specifically because
`MemoStore.remember()` refuses to overwrite an entry that still looks reusable
to its own check, which would otherwise refuse this task's memo forever with
nothing able to write a replacement. A memo that is merely stale, or whose own
status already says something other than `completed`, needs no deletion:
`remember()` already treats those as safe to overwrite the next time this
task's criterion freezes.

**Q: Why does an unfinished run not get to leave a memo?**
A: The entry is written when each stage freezes but stays unusable until the
originating run reaches `completed` — until that criterion actually graded real
work and the run passed its own gate. A criterion frozen by a run that then
failed is exactly the criterion not to inherit: unproven at best, and at worst
the reason the run failed.

**Q: Can an eval use it?**
A: No — `SwarmRun(profile="eval", memo=...)` raises, mirroring the existing
cache ban. An eval measures variance across repeats; serving repeat 2 from
repeat 1 does not bias the bootstrap interval, it collapses it toward zero
width, and a zero-width interval reads as a strong result. It is a raise rather
than a note because this is precisely the shortcut someone takes while trying to
speed up a slow eval.

**Q: What does the memo save, in numbers?**
A: `calls_avoided`, and deliberately not dollars. A memo hit writes a
`memo_hit` ledger row the same way a cache hit writes a zero-cost row, so "what
did the memo save" is a query rather than an estimate. But `would_have_cost`
stays 0.0 with the reason stated: the avoided proposer call never chose a
provider or a model, so pricing it would mean inventing a route it was never
given — and on a pooled free tier the scarce resource is requests anyway.
Reporting an invented dollar saving is exactly the dishonesty the ledger exists
to prevent. NOT DONE: if a dollar figure is ever wanted, it has to be priced at
the pool's default model at charge time and labelled as such.

**Q: What is not wired into the memo?**
A: `swarm session` (it cycles repeated tasks to build a learning curve, and
removing synthesis from repeats 2..N would flatten the very curve it measures),
`eval`, and the CLI `runs resume` path — so a CLI-resumed run cannot settle the
memo its earlier process wrote, and that entry simply ages out. A resume always
beats a memo inside the run too: lookup is skipped when `state.criterion` or
`state.results` is set, because swapping a criterion in mid-flight would grade
existing results against something that did not produce them. And, like
idempotency, the store is per-pod: two replicas can double-write a memo.

**Q: The widest thing your "exact" key forgives, and where that choice came
from.**
A: Case. `normalise` does three things -- strip, collapse internal whitespace,
casefold -- so `"Summarise X"` and `"summarise x"` share a memo, and
`test_normalisation_forgives_only_layout` pins that as a HIT, not a miss. On
where it came from, I want to be straight rather than impressive: `docs/SPEC.md`
has no memo section and no clause about this key, so this is an implementation
decision, not a divergence from a written requirement I can show you. The
argument for it is that the three operations cannot change what is being asked
-- a heredoc newline, a doubled space from a paste, a capitalised first word --
while punctuation and word order are left alone, so `"Compare A to B"` and
`"Compare B to A"` stay different keys. The safety net is that no answer is
replayed and the criterion is re-attacked against the new task text on every
hit. It is one line to reverse if you would rather case were significant.

### The prompt prefix

**Q: Why does the ORDER of a prompt cost money?**
A: Because every provider in this pool is OpenAI-compatible, and Groq/OpenAI
automatic prefix caching keys on a byte-identical LEADING prefix of the rendered
conversation. The old layout sent one user message ordered TASK, STEP, REQUIRED,
checks, skills, failures — so the first thing differing between two agents was
STEP, on the second line, and everything after it, including the criterion block
and the skills block (by far the largest part of the prompt), fell outside the
shared prefix and was re-read cold on every worker call. A `smoke` run makes
far fewer calls than its `target_calls` label of 30 suggests — measured, 8 on a
two-node plan and 9 on a three-node one, because batched generation issues one
call per plan node rather than one per agent — so the absolute saving here is
small; what makes it worth doing is that it is the same defect at every profile
size, and `deep` is 280. Only `WORKER_SYSTEM`, 640 characters, was ever shared.

**Q: What exactly is cached now?**
A: Three layers, split by how often they change. Run-stable — the base prompt,
`TASK:`, and the frozen criterion — computed once per run by `build_run_system`.
Node-stable — the skills retrieved for that plan node — appended by
`build_node_system`. Both ride in the system message, which renders first. The
user message carries only the volatile tail: `STEP:`, `REQUIRED:`, the previous
attempt's failures, and — on a batch call — the "produce K separate candidates"
instruction.

Say what changed carefully, because the loose version of this sentence is
wrong. No block is added, dropped or reworded. But two blocks move AHEAD of two
others as well as into a different role: legacy renders TASK, STEP, REQUIRED,
GRADED; hoisted renders TASK, GRADED, then STEP, REQUIRED. So "the same bytes
under a different role" understates it — it is the same blocks, reordered and
re-split. Measured by capturing `len(request.system)` and `len(request.prompt)`
on every worker call of a `smoke` run under each arm. The run makes two, one
batched generation call per plan node:

```
                          legacy                 hoisted
node gather         640 sys + 525 user     871 sys + 294 user   = 1,165
node verify         640 sys + 526 user     871 sys + 295 user   = 1,166
```

Same total on each row, so the reorder moved bytes and added none. Say the
numbers exactly: the hoisted system message is 871 characters, not 872, and the
526 user turn is `verify`'s, which pairs with 295 — `verify`'s instruction is
`produce report.json` against `gather`'s `produce notes.json`, one character
longer. The legacy 640 is `WORKER_SYSTEM` on its own. And `system + prompt`
concatenated is NOT byte-equal across the two arms, because the join between
two adjacent blocks is a blank line inside one message and a role boundary
between two.

**Q: So what exactly is proven identical, and what is not?**
A: PROVEN. Against `ScriptedProvider` — a stub that returns one fixed worker
output regardless of what it is sent — a hoisted run and a legacy run of the
same task produce the same criterion hash, byte-identical `Candidate.output`
for every node, and the same integrity hash
(`test_moving_prompt_bytes_between_roles_does_not_change_the_result`). That is
proof the reorder does not drop a field, duplicate one, or otherwise corrupt
what the pipeline grades. It is guarded against being a tautology by
`test_the_two_prompt_layouts_are_genuinely_different_prompts`, which asserts
every legacy worker prompt contains `TASK:` and no hoisted one does — so the
two arms really did send different bytes.

NOT PROVEN, and measurably false the moment the responder reads its input.
`SimulatedProvider` seeds its synthetic text on
`sha256(f"{system}|{prompt}|{temperature}")`, and hoisting moves bytes across
that `|`, which changes the digest and therefore the generated wording. Checked
directly: hand it `LLMRequest(prompt=S + "\n" + U, system="")` and
`LLMRequest(prompt=U, system=S)` — the same information, split differently —
and the two responses differ, while `tokens_in` is the same because both roles
are now counted. So the equality claim holds for a content-insensitive test
double and nothing stronger. What a real model does with the same information
reordered is the NODE PASS RATE parity gate below, and that gate HAS NOT BEEN
RUN.

**Q: How do you know two agents send identical bytes?**
A: You cannot get it from retrieval alone, and that was the subtle part.
`SkillLibrary.retrieve` scores by success rate and `record_use` moves success
rates DURING a run, so agent 1 and agent 12 querying the same library with the
same text can be offered a different ordering — and a reordered skills block is
a different prefix. So the run resolves one `NodePrefix` per node and hands the
same frozen object to both `worker.execute` and `_batch_generate`. A test drives
a full smoke run and asserts every worker AND batch request carries a system
message that starts with `WORKER_SYSTEM` and is strictly longer than it, which
no fallback can produce. Negative control executed: under
`SWARMD_PREFIX_ORDER=legacy` that test fails with "sent the bare base prompt:
its node prefix was dropped", so it is sensitive to the exact failure it names.
A hand-counted grep would not have caught it: the failure is a second call site
that forgets the prefix, and the grep would still have found the first one.

**Q: Is the saving measured or estimated?**
A: Measured or absent, never inferred. `LLMResponse` carries `cached_tokens` AND
`cached_tokens_reported` — two fields rather than one nullable count, because
"this provider does not report cached tokens" and "this provider reports that
nothing was cached" are different facts, and reading the first as the second
turns a working prefix cache into an apparent no-op or the reverse. A negative
or non-numeric value is read as "not reported" rather than clamped to zero,
because a provider sending nonsense has told us nothing and recording nothing as
a measurement is how a fabricated saving gets into a report. The count lands on
the ledger row and the `prefix_cache` report block; nothing is derived from
prompt length.

**Q: A run reports cached_tokens=0. Is that a bug?**
A: That question is exactly why `ProviderSpec.prefix_cache` now rides on every
`pool.probe()` row and prints in `swarmd providers`. A zero from
`google-aistudio`, labelled `explicit`, is expected — it needs an explicit cache
handle we do not create. A zero from Groq, labelled `auto`, means the shared
prefix is not being hit and something has broken it. The label is the only thing
that separates the two.

**Q: What did hoisting nearly break?**
A: Three things, and each one made a number look BETTER, which is the dangerous
direction. The simulated provider seeded its output hash on `prompt` only, so
after the hoist two runs of different tasks against different criteria would
have produced identical synthetic output and every offline integrity hash would
have been blind to the task and the criterion. Both offline providers counted
`tokens_in` from `request.prompt` alone — hoisting MOVED prompt bytes into
`system`, it did not delete them, so the reorder would have appeared to cut
prompt tokens by roughly the size of the hoisted block while the real bill was
unchanged, a fabricated saving landing straight in the capacity forecast.
Re-measured on the `smoke` fixture in `tests/swarm/test_run.py` (task
`summarise the source records`, node `gather`, no skills retrieved), the user
turn falls from 525 characters to 294 on a batched generation call and from 273
to 42 on a plain worker call, while `system + prompt` is 1,165 and 913
respectively under BOTH arms — identical totals, which is the whole point. Both
now count `system + prompt`. (The plain-call row needs the batch call to return
nothing, because an ordinary run batches generation and makes no plain worker
call; the batched row is what a run really sends.)

**Q: Why is similarity-based reuse unsafe here in general?**
A: Same argument in three places, which is why I trust it. The response cache
learned it by serving one plan node's answer to another at 0.97 cosine. The
memo inherits the conclusion in the strongest available form: it HAS a
near-match tier, and that tier is still a discrete sha256 equality on task
shape rather than a score with a threshold, because the lesson was not "do not
generalise" but "do not generalise on a continuous similarity of
machine-assembled text". And skill retrieval hit the same class of error from
the other side: with a pen-price skill approved, "Compute the average rainfall
in millimetres for 12 cities" retrieved it on the strength of the word
"compute" and the presence of a number; that too is fixed with a subset test
over literal kinds, not a threshold. The general statement: semantic matching
assumes the varying part of a prompt is most of the prompt. That is true of
human paraphrases and false of machine-assembled text.

**Q: What is NOT proven about the prefix change?**
A: Quality. Moving the criterion into the system role changes how a model
WEIGHTS it, and a cheaper run that grades worse is a regression however good the
cache numbers look. The acceptance gate is NODE PASS RATE parity on the `eval`
profile at fixed seeds — never `cached_tokens`, which measures the mechanism
rather than the outcome — and that gate HAS NOT BEEN RUN, because it needs live
providers and a task corpus. `hoisted` is the default on the byte-equivalence
argument alone (identical integrity hash under a scripted provider), the exact
two-command procedure is in `worker.py`'s module comment, and
`SWARMD_PREFIX_ORDER=legacy` is the rollback if the gate later fails. Also not
built: Gemini explicit context caching, which is unreachable through the
OpenAI-compatible shim this pool talks to and needs its own create/TTL/delete
handle lifecycle; and the optional `prefix_group` model-affinity routing hint.

### The learning loop

**Q: Memorisation or transfer — which is this?**
A: Both, in two separate mechanisms, and conflating them is how "self-learning"
gets over-claimed. The memo is MEMORISATION, explicitly: an exact key, one task,
no generalisation, and it reuses only how to grade and how to decompose. The
skill library is the transfer claim, and it is the one that has to be defended,
because a skill is offered to tasks it has never seen.

**Q: What stopped the library learning the answer instead of the method?**
A: It did learn the answer, once — distillation stored the longest successful
output, `{"accuracy": 94.3, "baseline": 82.1}`, as a skill instruction, so a
later run on different numbers would have been handed those and told they
worked. The system was reliably generating exactly what its `library_poisoning`
detector exists to reject. Now `abstract()` replaces literals with typed
placeholders in one ordered pass, `strip_source_terms()` removes the
subject-matter nouns a step shares with its own task (which shape-abstraction
structurally cannot see), and `validate_instruction()` RAISES rather than
repairs when an instruction still shares a whole literal with its source task.
Raising matters: a repair would have produced a quietly weakened skill.

**Q: Two verified successes used to promote a skill. What changed?**
A: The bar counts distinct task SHAPES, not successes. Two agents passing the
same node of the same run satisfy "two successes" — but they share a task, a
criterion and a prompt, so that is one observation counted twice, and the
library ends up offering advice proven on exactly one question. Two distinct
shapes is the smallest number that can distinguish "this worked" from "this
works on more than the thing it came from".

**Q: What counts as a distinct shape, and how do you stop me farming it?**
A: `generalise.task_signature`: the HEAD NOUNS of the task's noun phrases,
minus method vocabulary and function words and folded to singular, plus the
sorted KINDS of literal it carries — both as sorted sets, so word order does
not enter. The first version was keyed on the abstracted SENTENCE, which
inherits `memo.normalise`'s deliberate "a paraphrase MISSES" property — correct
for an exact-match cache, exactly wrong here, because a miss MANUFACTURES the
second piece of evidence. One request reworded with "please" and "for me"
pushed a candidate to promotable. That was found in review, not by me. Method
verbs are excluded for the same reason, so compute → calculate cannot mint a
second shape.

What I can actually show, by running `task_signature` over the restatements a
farmer would reach for first. Base: `"Compute the total cost of 3 pens at 1.25
dollars each"` → `bac37e579ae7c1e7`. Politeness padding (`"Please compute ...
for me"`), swapped digits (`"9 pens at 4.75 dollars each"`), a number spelled
out (`"three pens at 1.25 dollars each"`), the whole sentence uppercased, the
whole sentence lowercased, and the literals fronted (`"AT 1.25 DOLLARS EACH, 3
PENS -- COMPUTE THE TOTAL COST"`) all return that same `bac37e579ae7c1e7`. Six
restatements, one signature, closed by measurement rather than by assertion.
The spelled-out one was the most recent to close: matching only digits made
`"three pens"` and `"3 pens"` two shapes until `_digits_for_shape` folded the
cardinals inside `task_shape`.

**Q: Where does that still leak?**
A: A restatement that introduces genuinely new subject matter. "Compute the
total cost of 3 pens at 1.25 dollars each for the invoice" scores a second shape
because "invoice" is a content noun no lexicon can rule out. I claim that is the
defensible boundary — a task mentioning something new is somewhat new — not that
it is airtight. The signature is only as good as two fixed, human-authored lists
(the method lexicon and the function words), which is deliberate: a
corpus-derived stoplist would move the signature week to week.

**Q: Which way does it fail?**
A: Toward collapsing. "count the pens" and "list the pens" share a signature and
count as one shape, so the cost is a promotion that does not happen. Splitting
one task into two shapes is the poisoning channel, so that is the error to
avoid. Separating them would need either method verbs in the signature
(re-opening synonym farming) or per-token hashes of the task's own words — a
dictionary-attackable leak of exactly the text this feature refuses to store.
Two disclosed exceptions sit on the wrong side anyway, and both are SPLITS —
which is the dangerous direction, because the bar counts distinct signatures,
so a split hands a promotion its second piece of evidence for free.

The first is `_stem`. Its bare-`s`-before-`es` tie-break folds the common `-se`
subject class correctly — measured, `cases` → `case`, `niches` → `nich` and
`niche` → `nich`, `queries` → `query`, `boxes` → `box`, `pens` → `pen` — at the
cost of words whose singular already ends in a bare `s`. Measured on that same
run: `bus` → `bus` but `buses` → `buse`; `gas`/`gases`; `lens` → `len` but
`lenses` → `lense`; `virus` → `viru` vs `viruses` → `viruse`; `campus` →
`campu`. Each of those mints two signatures across singular and plural. It is
NOT fixed, deliberately: `lens` and `pens` end in the identical two letters, so
no suffix rule separates "already singular" from "needs stripping" without a
lexicon, and `generalise.py`'s whole claim is that it keeps no lexicon and
makes no model call.

The second is structural, and it is much narrower than it was. It used to be
the fronted noun phrase: `"Pens: compute the total cost of 3 at 1.25 each"`
contributed no subject at all and split from the ordinary phrasing. That is
CLOSED — `task_shape` now falls back to content words when nothing was
introduced, and measured, the fronted sentence and `"compute the total cost of
3 pens at 1.25 each"` both score `64f9e0ced633ac1f`. What is left is the
fallback's own blind spot: it keeps every content word and cannot tell an
unrecognised verb from a noun, so in a sentence with NO determiner anywhere a
verb outside `METHOD_LEXICON` is read as subject matter. Measured,
`task_signature("tally widgets")` is `343c262fa82dfce7` against
`04d865fa68cef42b` for `"count widgets"`. It needs BOTH conditions where the
old one needed only a fronted phrase, and one determiner removes it: `"tally
the widgets"` and `"count the widgets"` both score `04d865fa68cef42b`. The
answer when such a verb turns up is to add it to `METHOD_LEXICON`, not to widen
the fallback.

**Q: Does the bar actually gate approval?**
A: It gates the HUMAN QUEUE (`SkillGate.submit`) and `SkillLibrary.approve()`
itself. A candidate below the bar is still recorded, still unusable, still
accruing evidence, and still appears in `result.proposed_skills`; what the
queue controls is whether a reviewer is asked. `approve()` separately refuses
a candidate whose recorded `evidence_tasks` is non-empty but short of
`MIN_DISTINCT_TASKS`, unless the caller passes `force=True` — which the
skill's own record then carries as `approval_note`, so a bypass is visible on
the skill rather than only in whoever's log invoked it. `--auto-approve` still
can approve a one-shape candidate, but explicitly now: it passes `force=True`,
which is the documented "bypasses review, but visibly" behaviour, not a way
around the check. The one place the check stays silent on purpose: a skill
with NO recorded `evidence_tasks` at all — hand-authored, never proposed
through `run.py`'s distillation path — skips it entirely, both because a rule
about distinct task shapes has nothing to say about a candidate never counted
against it, and because several tests construct skills via a bare `propose()`
with no evidence as fixtures for unrelated features and then approve them
without `force` — `test_skill_integrity.py` alone has six unforced `approve()`
calls, most of them on evidence-free fixtures. I have not measured how many an
unconditional check would break, and I would rather say that than quote a
number I did not count. The vacuous case is not merely tolerated, either: it is
pinned by its own test,
`test_a_candidate_with_no_tracked_evidence_is_not_gated_by_the_bar`, so
tightening it would be a deliberate change to a stated property rather than an
accident.

NOT COVERED, and it is the obvious attack: a caller that reaches `propose()`
without an `evidence_task` gets a skill the bar cannot speak about. `run.py`'s
distillation path always supplies one, so this is a hole for a hand-authored
skill or a direct API caller, not for the learning loop — but it is a hole.

**Q: You fixed retrieval too. What was wrong with it?**
A: With the pen skill approved, "Compute the average rainfall in millimetres for
12 cities" and "Compute the total distance of 5 marathons at 42.2 km each" both
retrieved it, on the strength of "compute" and the presence of a number. Neither
is a unit-price question, and a wrong skill actively misleads a worker where no
skill merely leaves it to reason from the task. Retrieval now abstracts BOTH
sides and, once a stored pattern names `MIN_SHAPE_SLOTS` (2) or more distinct
literal kinds, requires the incoming task to carry ALL of them — a subset
test, not a fraction. The pen skill's pattern is `{NUMBER, MONEY}`; rainfall
and marathon-distance each carry only `NUMBER` and are refused outright rather
than partially credited. A proper-noun kind is excluded from the count on both
sides, because counting it let a capitalised word do a missing literal's job.
The pattern's method vocabulary has to agree too, unless the task states no
method at all — a bare noun phrase makes no claim to contradict.

**Q: How strong is that precision claim? Where exactly is the boundary?**
A: Tighter on the axis it actually checks than the project's first pass, and
the honest gap moved rather than closed. Rather than assert it, here is the
boundary probed: distil the pen skill, approve it, and run `library.retrieve`
over a battery. Its stored pattern is `Compute the total cost of slot_number
slot_term at slot_money each json_parses min_distinct_words`.

```
REFUSED   Compute the average rainfall in millimetres for 12 cities
REFUSED   Compute the total distance of 5 marathons at 42.2 km each
              -- both carry NUMBER only; the pattern needs {NUMBER, MONEY}
REFUSED   List 5 refunds over 20.00 dollars
REFUSED   Compute the total budget of 5 teams at 20.00 dollars each
              -- both carry both kinds; the METHOD set disagrees
                 ("cost" is in the pattern and not in the task)
RETRIEVED Compute the total cost of 12 notebooks at 3.40 dollars each
RETRIEVED 7 pencils at 40c each
RETRIEVED 5 teams in the Boston office at 20.00 dollars
```

So the two probes a reviewer found are refused outright — missing even one of
the pattern's literal kinds is disqualifying, not down-weighted below a
threshold. And the boundary is genuinely two-sided: `"the total budget"` is a
related question and is refused too, because the method check is a SUBSET test.
Precision was bought with recall.

The residual is the last row, and it is exactly the disclosed one. A task that
states NO method vocabulary skips the method check entirely, by design, because
refusing it would delete `"7 pencils at 40c each"` — the headline transfer case
— along with it. So a task carrying every one of the pattern's literal kinds
and no stated method still retrieves. `"5 teams in the Boston office"` was
refused before only because it had no MONEY; add a price and it is offered a
unit-cost method it never asked for. That is a design choice named in
`_shapes_agree`'s own docstring, not a boundary measured against a corpus, and
I would not claim it is the right cut — only that it is the cut, and which way
it errs.

**Q: So has learning improved anything, measurably?**
A: Not yet, and the last real measurement went the wrong way — treatment 0/5
against control 2/5, node pass rate 56.7% against 65.6% — which was traced to
distillation anchoring skills on plan node names that are generated fresh every
run. That is fixed; it has NOT been re-measured, because the daily quota is
exhausted, and the same is true of this batch's changes. Everything in this
section is a mechanism with a test, not a curve. The project still does not
claim the system improves, and STATUS.md still says so.

**Q: What else did you design for learning and not build?**
A: The question used to be "what else in the learning spec is unbuilt", and
that framing was false — there is no learning spec. I grepped: `Episode`,
`ObservationStore`, `MIN_DISTINCT_CRITERIA` and `MIN_DISTINCT_RUNS` appear only
in `docs/flow.md` and this file. No document in `docs/` defines them and no
source file mentions them. They are my own sketch, so calling them unmet
requirements would have borrowed authority from a document that does not exist.

Named as my own unbuilt design, then: the `Episode` / `ObservationStore` /
out-of-band corpus-wide promotion path (evidence lives on the Skill as a tuple
of `task_signature` values instead), `MIN_DISTINCT_CRITERIA` and
`MIN_DISTINCT_RUNS`, `Skill.transfers` and the transfer-rate term in the
retrieval score, the never-transferred prune rule, `mark_retired` and the
no-resurrection loop, the "(unproven: worked X of Y)" prompt annotation, and
the families/skill_evidence endpoint. `merge_templates` is implemented and unit-tested but not yet wired
into instruction construction, because a single-task distillation has no second
template to merge against. No schema-version constant was added either: a new
build reads an old file because the new fields default, and an old build refuses
a new file through `SkillLibrary._load`'s existing unknown-field rejection, so a
version integer would be a second, redundant mechanism.

**Q: Why is shape and literal matching stdlib regex rather than a model call?**
A: The same reason criteria are a declarative check language: everything in
`generalise.py` runs on the distillation and retrieval paths, which are
already model-adjacent output being fed back into this system's own inputs.
Asking a model "is this the same shape?" would put a second untrusted author
on the only write path into the library and would cost a provider call per
node on a quota-bound system where the provider is the thing being rationed.
`abstract`, `task_signature`, `literal_map` and `leaked_subject_terms` are
pure functions of a string — no I/O, no swarmd imports at all, which is
`generalise.py`'s own stated rule — so a chaos-integrity hash stays comparable
across runs and a reviewer can say exactly why a token was classified as a
literal or a subject. `skills.py`'s `_shapes_agree` follows the same
deterministic-code-not-a-model-call discipline one layer up, over the
primitives `generalise.py` exports. The cost is
the one named throughout this section: a fixed, human-authored rule set misses
what a model would catch (a synonym, an unlisted content word) and can only be
widened by editing the rule, never by a model quietly generalising further on
its own.

**Q: If a run's provenance turns out bad after the fact, does anything it
already taught the library get rolled back?**
A: No, and this is a real gap rather than a designed-around one. The memo has
exactly this rollback: `_memo_admission` deletes a memo whose own `status`
says `completed` but whose run-store document disagrees, because leaving it
would refuse that task forever. The skill library has no equivalent. `_distill`
calls `SkillLibrary.propose()` for every node that passed, before the run's
own final status is set, and `Skill.provenance_run` records which run produced
each piece of evidence but nothing ever reads that field back against the
run's later status. A run cancelled mid-distillation, or later marked
`interrupted` by a control-plane shutdown, can leave evidence already banked
on a skill with no path that retracts or re-checks it. It is bounded by the
same human gate everything else is — a poisoned candidate still needs
`MIN_DISTINCT_TASKS` shapes and a reviewer's approval before it does anything
— but the bound is the ordinary evidence bar, not a provenance check built for
this specific failure. Named here rather than left to be discovered the way
the memo bug was.

## Section 14: The version a senior engineer would actually ask

> Section 13 explains the mechanisms. This section is the adversarial pass over
> them — the follow-ups I would ask if someone showed me this work. Rule for
> every answer here: it must name what is NOT covered, or it does not count as
> an answer.

### Idempotency

**Q: What is the key scoped to?**
A: The store, and nothing narrower — no per-operator, per-tenant or
per-endpoint namespace on the record. `_entry_key` validates the header against
`KEY_RE` (8–200 characters of `[A-Za-z0-9_.:-]`) and passes it straight through
as the record's identity. What IS scoped is the body fingerprint:
`fingerprint()` hashes `{"endpoint": ..., "payload": ...}` with
`sort_keys=True`. Two consequences. Reusing one key across `POST /api/runs` and
`POST /api/runs/{id}/resume` is a body conflict, not a replay, because
`endpoint` is inside the hash. And two clients serialising the same request
with different JSON field order are correctly treated as one request, because a
body's field order is not meaningful.

NOT COVERED: two unrelated callers who happen to pick the same key collide, and
the loser is handed the winner's run. The eight-character floor exists to make
that unlikely, not impossible — a client that "just picks something" is the
failure mode it was sized against. Per-caller scoping would need caller
identity inside the record key, which is a change to `IdempotencyStore`, not a
config flag.

**Q: Same key, different body. Why 422 and not 409?**
A: Because 409 is already used, for a different state, and the two would
otherwise be ambiguous. 409 means the key is RESERVED and another request is
still constructing its run — a transient state that resolves. 422 means the key
is settled and the pair (key, body) is unprocessable: well-formed key,
well-formed body, incompatible together. A client can usefully retry a 409;
retrying a 422 with the same body is pointless. 400 is reserved for a key that
fails `KEY_RE`, because that is a header problem the client cannot fix by
changing its payload. Three distinct failures, three codes.

The conflict response deliberately does not name the first run's id. Leaking
the id of a run this caller did not create turns a client bug into an
information disclosure, and there is nothing the caller can legitimately do
with it.

NOT COVERED: a reservation is disbelieved after `PENDING_STALE_S` (120s), so a
process that died mid-construction frees its key rather than holding it hostage
forever. That trade admits a small duplicate window — a construction that takes
longer than 120 seconds and then succeeds could coexist with a second
acceptance. I took that over a crash making a key permanently unusable.

**Q: Is it durable across a restart?**
A: Yes, and that is the case it exists for. One file per key under the RunStore
root, written with RunStore's discipline — temp file in the SAME directory (so
`os.replace` is a rename rather than a cross-device copy), fsync, `os.replace`.
The retry that arrives after a deploy is precisely the retry worth
deduplicating, so an in-memory dict would have covered only the cases that did
not matter. A test opens a fresh store over the same directory and finds the
record.

One detail worth mentioning because it was a bug in waiting: RunStore's
filename sanitiser drops `.` and `:`, which `KEY_RE` allows, so two distinct
keys would have shared a file. `path_for` appends a sha256 suffix, which is
what makes the name unique.

**Q: What breaks with two replicas?**
A: It can double-accept, and I would rather say so than imply otherwise. The
`asyncio.Lock` is per-process and the store is a per-pod filesystem; a stat and
a write are not atomic across machines. Two pods handed the same key at the
same instant can both see "no record", both reserve, and both start a run. It
is in the module docstring rather than buried, and `IdempotencyStore` is a
deliberately narrow surface — lock / get / reserve / complete / release / prune
— so a Redis or Postgres backend drops in behind it. NOT BUILT. The same
limitation applies to the memo store, for the same reason.

**Q: Why no body-hash fallback when the header is absent?**
A: Because re-running an identical task on purpose is a normal operation here —
an A/B arm, a flake hunt, a chaos comparison, the control run every eval needs.
A body-hash fallback would hand an operator who forgot the header yesterday's
run id instead of the run they asked for, and it would do so silently. So "no
header means a new run" is unconditional, with no environment flag to weaken
it. The cost is real: clients must opt in, and a client that never sets the
header gets no protection at all. I took that over silently breaking the
workflow this project runs most.

### Prefix caching

**Q: Why does prompt ORDER matter for a provider cache?**
A: Because automatic prefix caching keys on a byte-identical LEADING prefix of
the rendered conversation, not on set membership. Everything from the first
differing byte onward is a miss, whether or not those bytes were sent before.
The old layout put `STEP:` on the second line, so the divergence point was two
lines in, and the criterion block and the skills block — by far the largest
part of the prompt — sat after it and were re-read cold on every worker call.
Only `WORKER_SYSTEM`, 640 characters, was ever shared. Moving the run-stable
and node-stable material ahead of the volatile tail is the whole mechanism.

**Q: What exactly is proven identical after hoisting, and what is not?**
A: PROVEN: against `ScriptedProvider`, a stub that returns one fixed worker
output regardless of what it is sent, a legacy run and a hoisted run of the
same task produce the same criterion hash, byte-identical `Candidate.output`
per node, and the same integrity hash. Guarded against being a tautology by a
second test asserting the two arms really did send different bytes — every
legacy worker prompt contains `TASK:` and no hoisted one does.

NOT PROVEN, and false the moment the responder reads its input.
`SimulatedProvider` seeds on `sha256(f"{system}|{prompt}|{temperature}")`, and
hoisting moves bytes across that `|`, so the same logical request returns
different text under the two arms — checked directly. Nor is it literally the
same string: measured on the `smoke` run's `verify` worker call, legacy is 640
characters of system plus 526 of user and hoisted is 871 plus 295 — the same
1,166 total and the same blocks, but REORDERED (legacy renders TASK, STEP, REQUIRED, GRADED;
hoisted renders TASK, GRADED, then STEP, REQUIRED) and re-split, so `system +
prompt` is not byte-equal across the arms. "The same bytes under a different
role" is the sentence to avoid.

NOT COVERED AT ALL: quality. Moving the criterion into the system role changes
how a model WEIGHTS it, and a cheaper run that grades worse is a regression
however good the cache numbers look. The acceptance gate is NODE PASS RATE
parity on the `eval` profile at fixed seeds, and it HAS NOT BEEN RUN — it needs
live providers and a task corpus. `hoisted` is the default on the
scripted-provider equivalence alone; `SWARMD_PREFIX_ORDER=legacy` is the
rollback.

**Q: How is the saving measured rather than estimated?**
A: `LLMResponse` carries two fields, not one nullable count: `cached_tokens`
and `cached_tokens_reported`. "This provider does not report cached tokens" and
"this provider reports that nothing was cached" are different facts, and
reading the first as the second turns a working prefix cache into an apparent
no-op, or the reverse. `parse_cached_tokens` reads
`usage.prompt_tokens_details.cached_tokens` (and the Anthropic-shaped
`cache_read_input_tokens`), treats a negative or non-numeric value as NOT
REPORTED rather than clamping it to zero, and the pair lands on the ledger row
and in the run report's `prefix_cache` block. Nothing is derived from prompt
length.

NOT MEASURED: under the offline providers the count is 0 with
`reported=False`, and the report says not-measured rather than zero-saving. So
there is no measured prefix-cache saving anywhere in this repo today — the
mechanism is verified, the benefit is not. `ProviderSpec.prefix_cache` rides on
every `pool.probe()` row and prints in `swarmd providers` precisely so a zero
can be read correctly: a zero from a provider labelled `explicit` is expected,
a zero from one labelled `auto` means the shared prefix is being broken.

**Q: Why is similarity-based reuse unsafe here?**
A: Because it was tried and measured. `router/cache.py` served one plan node's
answer to another: worker prompts share a long template and differ in a step
name, so cosine similarity between three genuinely different nodes measured
0.97 against a 0.95 threshold. The symptom was a fast, cheap run at a high hit
rate whose nodes all produced the same artifact. The fix is not a higher
threshold — similarity here is dominated by shared boilerplate and RISES with
template length, so a longer envelope pushes any two prompts above any
threshold you pick. Semantic matching assumes the varying part of a prompt is
most of the prompt; true of human paraphrases, false of machine-assembled text.
The cache is exact-keyed now and the wrapper refuses a similarity cache rather
than warning about it.

Both later features inherit the conclusion. The memo's near tier is a discrete
equality on a sha256 of task SHAPE, never a score. Skill retrieval is a subset
test over literal kinds and method vocabulary, never a fraction.

**Q: Why can Gemini explicit context caching not be used?**
A: Because this pool talks to every provider through one OpenAI-compatible
chat-completions adapter, and explicit caching is not expressible in that
request shape: it needs its own create / TTL / delete handle lifecycle and a
cache id carried on each call. It is deferred rather than forgotten — the
registry labels `google-aistudio` as `prefix_cache="explicit"` and `swarmd
providers` prints it, so a `cached_tokens=0` from that provider reads as "we do
not create the handle" rather than "the cache is broken". NOT BUILT.

### The memo

**Q: What makes a near hit safe?**
A: Four things in sequence, and the hit is refused if any of them fails.
(1) `literal_map` is positional AND kind-checked: the Nth literal of the stored
task becomes the Nth literal of the new one, and only when every kind matches
in order. It returns `None` when they do not line up, which is the answer that
makes the caller pay for its own synthesis rather than guess. (2) `rebind`
matches whole tokens only, so the `2` inside `1.25` is never rewritten.
(3) `leaked_subject_terms` catches a source-only subject noun surviving inside
a check PARAMETER. (4) The rebound criterion is re-checked by `malformed()` and
RE-ATTACKED against the new task's text.

NOT COVERED: `rebind` deliberately does not rewrite TERM, so a plan node's
human-facing instruction can still read as the source task's subject. That is
correct for prose a worker reads for sense, and is exactly why gate (3) exists
for the criterion, where the same surviving word is compared byte-for-byte by a
grader. Separately, the fingerprint is order-sensitive on literal kinds and
synonym-sensitive on the method verb, so plenty of genuinely similar tasks miss
the near tier outright — a miss, which costs six calls and nothing else.

**Q: Why re-run the adversarial attack instead of trusting the stored
criterion?**
A: Three reasons, and the first generalises. The attack is code that lives in
THIS build: `degenerate_candidates` may have grown a case since the criterion
froze, and a criterion today's attack set defeats must not be inherited on the
strength of surviving last month's. Second, one of the degenerate candidates is
built from the TASK STRING, so the attack is not task-independent — a criterion
that survived attack against pens has not been attacked against pencils. Third,
it is pure code: no provider call, no quota, no reason not to.

NOT COVERED: attack only tries DEGENERATE candidates. It cannot notice a
criterion that is well-formed, non-degenerate and simply about the wrong
subject — which is the hole `leaked_subject_terms` was written to plug, and why
that guard is a separate check rather than a stricter attack.

**Q: Why is a result never replayed?**
A: Because the failure modes are not comparable. The worst case of a bad memo
is a run graded against a criterion it would probably have written itself — the
workers still ran, the artifacts are real, the verdict is real. The worst case
of an answer cache is a run reporting success without doing anything, and on a
near hit it is worse still: pens' stored total is arithmetically wrong for
pencils, so a replay would be confidently, silently incorrect. A memo carries a
criterion and a plan. There is no stored candidate, output, artifact or verdict
in it to serve, so this is a property of the data structure rather than a
policy someone has to remember.

**Q: What invalidates an entry, and why is tampering quarantined while an
interrupted provenance run is deleted?**
A: The full gate list is in Section 13; the difference between the two outcomes
is the interesting part. A document whose `entry_hash` or `criterion_hash` does
not match its own payload was changed by something that is not this code. That
is evidence, and destroying evidence is the wrong response, so it is moved into
`memos/quarantine/`. A provenance run marked `interrupted` after the fact is an
ordinary operational event, not tampering — and there is a concrete reason
deleting is REQUIRED rather than merely tolerable: `MemoStore.remember()`
refuses to overwrite an entry that still looks reusable to its own check, and
this entry's own `status` field still says `completed`. Left on disk it would
refuse every future run of that task while no run could ever write a
replacement. Deleting it is what lets the next successful run leave a fresh
memo.

NOT COVERED: nothing rolls back what an interrupted run already taught the
SKILL LIBRARY. See the last question under Incentives.

### Learning

**Q: Memorisation or transfer — how do you know which you have?**
A: They are two separate mechanisms and I keep the labels apart deliberately.
The memo is MEMORISATION and is described as such: an exact key plus a
same-shape near key, one question at a time, reusing only how to GRADE and how
to DECOMPOSE. The skill library is the transfer claim, and it is the one that
has to be defended, because a skill is offered to tasks it has never seen.

How you tell them apart in this repo: the memo's evidence is a key hit, which
proves only that the same question was asked. The library's evidence is
`evidence_tasks` — a tuple of `generalise.task_signature` values from DIFFERENT
task shapes. That is the whole design of the bar.

Be exact about which hash that is, because the code's naming points the wrong
way. `run.py` computes `task_key = task_signature(task)` and passes it as
`evidence_task=`; `SkillLibrary.record_evidence` then calls the parameter
`task_fingerprint` and `Skill`'s docstring says "abstract task FINGERPRINTS" in
the loose sense of "a hash rather than the text". It is NOT
`generalise.abstract_fingerprint`. Verified by running a `smoke` run over
`"compute the total cost of 3 pens at 1.25 dollars each"` with a real
`SkillLibrary` and reading the record back: `evidence_tasks` is
`('bac37e579ae7c1e7',)`, which is `task_signature(task)`, while
`abstract_fingerprint(task)` is `db1c9854b830f5a9` and appears nowhere on the
skill. The distinction matters: the memo's near tier indexes on
`abstract_fingerprint`, so pens and pencils are ONE memo shape and TWO pieces
of skill evidence, which is exactly the behaviour each mechanism wants.

NOT ESTABLISHED: whether transfer actually helps. The last real measurement
went the wrong way (treatment 0/5 against control 2/5; node pass rate 56.7%
against 65.6%), traced to distillation anchoring skills on plan node names
generated fresh every run. That is fixed and has NOT been re-measured, because
the daily provider quota is exhausted. Everything in this section is a
mechanism with a test, not a curve.

**Q: Why two DISTINCT task shapes rather than two successes?**
A: Because two agents passing the same node of the same run satisfy "two
successes" while sharing a task, a criterion and a prompt. That is one
observation counted twice, and a library built on it offers advice proven on
exactly one question. Two distinct shapes is the smallest number that can
distinguish "this worked" from "this works on more than the thing it came
from".

**Q: How is that bar farmed, and what stops it now?**
A: The farm is a MISS, not a hit — every restatement that fails to match
MANUFACTURES a second piece of evidence. The first implementation was keyed on
the abstracted SENTENCE, which inherits `memo.normalise`'s deliberate "a
paraphrase MISSES" property: correct for an exact-match cache, exactly
backwards here. One request reworded with "please" and "for me" pushed a
candidate to promotable. Found in review, not by me.

`task_signature` now hashes the head nouns of the task's noun phrases (minus
method vocabulary and function words, stem-folded) plus the sorted KINDS of
literal, both as sets. Measured against the base task `"Compute the total cost
of 3 pens at 1.25 dollars each"` (`bac37e579ae7c1e7`), every one of these
returns that same signature: politeness padding, swapped digits, the sentence
uppercased, the sentence lowercased, and the literals fronted. Method verbs are
excluded from the subject for the same reason, so `compute` → `calculate`
cannot mint a shape.

NOT COVERED: a restatement that introduces genuinely new subject matter.
`"...for the invoice"` scores `64f93b39b25f93e2` — a second shape — because no
lexicon can rule out a new content noun. I claim that is the defensible
boundary, not that it is airtight. And the signature is only as good as two
fixed, human-authored lists (the method lexicon and the function words), which
is deliberate: a corpus-derived stoplist would move the bar week to week.

**Q: What residual remains, and which way does each one fail?**
A: Two. The direction is the part that is easy to state backwards, and in a
document about a farming defence that is the worst single thing to get wrong,
so here is the rule before the cases.

`MIN_DISTINCT_TASKS` counts DISTINCT signatures. So a rule that SPLITS one task
into two signatures hands a promotion its second piece of evidence for free —
that is the farm, and it is the dangerous direction. A rule that MERGES two
genuinely different tasks into one signature withholds evidence instead: the
cost is a promotion that does not happen. The design errs toward merging on
purpose. Saying "both residuals are on the splitting side, so they only cost a
promotion" attaches merging's benign consequence to splitting's failure mode,
and that is exactly inverted.

**Residual one — `_stem`'s bare-`s`-before-`es` tie-break. It SPLITS, so it can
manufacture evidence.** It folds the common `-se` subject class correctly
(measured: `cases` → `case`, `queries` → `query`, `boxes` → `box`, `pens` →
`pen`, and `niche` and `niches` both → `nich`) at the cost of words whose
singular already ends in a bare `s`: `bus` → `bus` but `buses` → `buse`;
`gas`/`gases` likewise; `lens` → `len` but `lenses` → `lense`; `virus` → `viru`
against `viruses` → `viruse`; `campus` → `campu`. At signature level that is
`"count the lens in the tray"` → `1d5513adf70035da` and `"count the lenses in
the tray"` → `d74a788bf7d23bee`: one question, two signatures, where `"count
the pen"` and `"count the pens"` correctly give one — computed just now,
`520803586b95d146` for both.
Ask about lenses twice, once in each number, and the bar is cleared. NOT FIXED,
deliberately: `lens` and `pens` end in the identical two letters, so no suffix
rule separates "already singular" from "needs stripping" without a lexicon, and
this module's entire claim is that it keeps none and calls no model. It does
not reach the near tier — subjects enter `abstract_fingerprint` only as a
count, so both spellings fingerprint `2ebf0629e20c2e86`.

**Residual two — the fallback's blind spot. It SPLITS too, and it replaced a
wider residual that is now closed.** The old one was the fronted noun phrase: a
determiner-less phrase at the front of a sentence contributed no subject, so
`"Pens: compute the total cost of 3 at 1.25 each"` split from the ordinary
phrasing AND merged with the fronted pencils sentence. Both halves are gone.
`task_shape` now falls back to content words when nothing was introduced, and
measured, the fronted pens sentence and `"compute the total cost of 3 pens at
1.25 each"` both score `64f9e0ced633ac1f`, while the fronted pencils sentence
scores `c1596a4da1991a26` — the same as ITS ordinary phrasing, so the two
subjects stay two signatures.

What the fallback costs instead, pinned by
`test_the_fallback_leaves_a_narrower_residual_and_this_is_it`: it keeps every
content word and cannot tell an unrecognised verb from a noun. So in a sentence
with NO determiner anywhere, a verb outside `METHOD_LEXICON` is read as subject
matter. Measured, `task_signature("tally widgets")` is `343c262fa82dfce7` — its
subjects are `('tally', 'widget')` — against `04d865fa68cef42b` for `"count
widgets"`. One question, two signatures, so it is farmable in principle. It is
strictly narrower than what it replaced because it needs BOTH a determiner-less
sentence AND an unrecognised verb, where the old one fired on any fronted
phrase; adding a determiner avoids it entirely, and `"tally the widgets"` and
`"count the widgets"` both score `04d865fa68cef42b`. If that test ever fails,
the fix is to add the verb to `METHOD_LEXICON`, not to widen the fallback.

If asked which residual is the one to worry about, the answer is residual ONE.
`_stem` is unchanged by any of this, Latin/Greek bare-`s` singulars still split
across singular and plural, and splitting is the direction that manufactures
evidence. It is not harmless.

**And one residual that is now closed.** `"three pens"` used to mint a second
signature against `"3 pens"` — one task, written twice, clearing the bar with
no second task solved. `_digits_for_shape` folds `zero`..`twenty` and
`thirty`..`ninety` to digits inside `task_shape`, so both now score
`bac37e579ae7c1e7`. Folded there and NOT inside `abstract()` generally, because
`abstract()` also renders distilled skill instructions, where a small number is
usually method guidance: `"write one paragraph"` means what it says. Verified
in both directions — `abstract("write one paragraph")` comes back unchanged
while `abstract("write 1 paragraph")` gives `"write <NUMBER> paragraph"`, and
re-running the suite with `abstract()` wrapped to fold cardinals too fails
exactly one test out of 1,241 (`1 failed, 1240 passed, 1 skipped, 1
deselected`): `test_run.py::test_distillation_without_artifacts_describes_the_
step_only`, on `'When a step calls for this: write <NUMBER> paragraph'`. A word
list is defensible here for a reason it would not be for filler adverbs: the
cardinals are a closed class and cannot go stale.

**Q: What does the human gate actually protect?**
A: It is the last thing between a proposed instruction and a string injected
into future workers' prompts, and it is where a person sees the abstracted
instruction and the retrieval pattern before either is used. The evidence bar
is enforced in two places rather than one: `SkillGate.submit` decides whether a
reviewer is ASKED, and `SkillLibrary.approve()` refuses a candidate whose
recorded `evidence_tasks` is non-empty but short of `MIN_DISTINCT_TASKS`. The
second matters because the first is a caller-side check, and anything reaching
`approve()` by another route — a stale duplicate, `--auto-approve`, a direct
call — used to bypass it. `force=True` is the audited escape: the bypass is
written onto the SKILL'S OWN RECORD as `approval_note`, not only into whoever's
log invoked it, so a later reader sees it. `--auto-approve` can still approve a
one-shape candidate, but explicitly, by passing `force=True`.

`SkillGate.submit` also dedupes on `skill_id`, because distillation re-proposes
the same content-addressed skill from every task that produces it. Without the
dedupe, a third shape queued a SECOND request for a skill the second shape had
already queued, and deciding the stale one after the fresh one was approved
silently un-approved and retired it — so the dedupe turns that into an audited
no-op.

NOT COVERED: a skill with NO recorded `evidence_tasks` at all — hand-authored,
never proposed through the distillation path — skips the bar entirely. That is
deliberate (a rule about distinct task shapes has nothing to say about a
candidate never counted against it) and it is also a hole a determined operator
can walk through. And the gate protects against a bad SKILL; it does nothing
about a bad reviewer.

### Determinism

**Q: Why is shape matching deterministic code and not a model call?**
A: Three reasons, in the order they actually bind. Trust first: everything in
`generalise.py` runs on the distillation and retrieval paths, which are already
model-adjacent output being fed back into this system's own inputs. Asking a
model "is this the same shape?" would put a second untrusted author on the only
write path into the library — and the agents producing that output are selected
on passing checks. Cost second: a provider call per node, on a system whose
scarce resource is provider calls. Legibility third: a reviewer can say exactly
why a token was classified as a literal, a subject or method vocabulary, and
can change it by editing a rule rather than by re-prompting.

**Q: What would break if it were a model call?**
A: The chaos-integrity guarantee first. SLO-2 asserts a run under kill-rate 0.9
produces a byte-identical output hash to a clean run; a nondeterministic
classifier on the distillation path makes that comparison meaningless. Then the
evidence bar: "two distinct task shapes" would depend on a sampler, so the same
two tasks could count as one or two on different days — and a bar that moves is
a bar farmable by retrying. And the memo's near tier would be reusing a
criterion on a model's say-so, which is exactly the "model grades its own
homework" direction this project exists to avoid.

**Q: How is determinism proven?**
A: Three ways, and none of them is an assertion in a docstring.

Structurally: `generalise.py` imports only `functools`, `hashlib`, `re` and
`dataclasses` — no swarmd imports at all, which is the module's own stated
rule, so no provider, no LLM, no async and no network can reach it. `skills.py`
adds `generalise`; `memo.py` adds `generalise` and `RunStore`, which is
filesystem only.

By construction: every fingerprint is sha256, never the builtin `hash()`, which
is salted per process.

By running it: `task_signature`, `abstract_fingerprint` and `memo.key_for` over
the same task under `PYTHONHASHSEED` 0, 1 and 42 return identical digests
(`bac37e579ae7c1e7`, `db1c9854b830f5a9`, and a key beginning `5902dce6`). That
is the check that would catch set or dict iteration order leaking into a hash
payload.

NOT COVERED: determinism is not correctness. Every one of these functions is a
fixed, human-authored rule set. It will miss what a model would catch — a
synonym, an unlisted content word — and it can only be widened by editing the
rule, never by the system generalising further on its own.

### Incentives

**Q: What does a clone actually INHERIT?**
A: This is the question I would ask, and the honest answer is uncomfortable.
`Economy.reproduce` clones agents whose profit clears `clone_threshold` with at
least two successes, and the parent pays `starting_balance` out of its own
balance, so reproduction is not free — free reproduction would let one lucky
agent flood the population, which is drift rather than selection. The child
inherits `lineage`, an incremented `generation`, and a copy of the parent's
`traits`.

And `traits` is `{"node": name}`. That is the only value passed at either spawn
site in `run.py`, and nothing anywhere reads `traits` back to change how an
agent behaves — not its prompt, not its temperature, not its retrieval. So a
clone is behaviourally identical to a fresh agent on the same node. The market
is real — payment on verified success, bankruptcy, cloning at a cost — and what
it selects over is nearly uniform, so selection currently has almost no
heritable material to act on. NOT BUILT: heritable traits carrying actual
behavioural variation.

**Q: Can you incentivise a stateless LLM call at all?**
A: Not in the sense the word usually means, and pretending otherwise is where
this class of system starts lying. You cannot change how a model thinks by
paying it: there is no gradient, no memory across calls, and the agent has no
representation of its own balance to reason about. What an economy CAN do is
change the POPULATION — who keeps running, who multiplies, and whose output
becomes a skill other agents are shown. That is selection over a population of
identical samplers, not incentive to an individual. Stated that way it is a
defensible mechanism; stated as "agents are motivated to succeed" it is a
category error.

**Q: Why would harsher failure penalties make the population worse?**
A: Because the cheapest way to avoid a penalty is to avoid attempting. Today an
agent is charged for every attempt through `Economy.spend` and paid
`success_reward` only when the frozen criterion passes, so failure costs the
attempt and nothing more. Add a penalty on top and the dominant strategy shifts
toward producing nothing rather than producing something that might be graded —
and a population selected for abstention has a beautiful pass rate and does no
work. The measurable symptom would be attempts falling while pass RATE climbs,
which is one reason pass rate alone is never the thing to select on.

I have to be precise about the status of that argument: it is reasoning about a
change that has NOT been made, not a measured result. There is no code path
today by which an agent CHOOSES not to attempt — the worker calls the provider
unless it cannot afford to — so abstention is not currently an available
strategy at all. The argument is about why I have not added the penalty.

**Q: Where does credit assignment break across nodes?**
A: At the node boundary, and it is a real gap. `settle` is called by the worker
that produced a candidate, against the criterion for THAT node. An agent whose
plausible-but-wrong artifact passes its own node and then breaks a downstream
node is still paid in full, and the downstream agent that fails on bad input
still loses its attempt. Nothing propagates a downstream verdict backward. So
the market rewards local pass rate, not contribution to the run.

NOT BUILT, and named rather than implied: ESCROW SETTLEMENT — hold a node's
payment until the nodes depending on it have settled, then release or claw
back. That is the designed fix and it does not exist.

**Q: What else is designed but not built?**
A: Four things, each labelled as such wherever it appears.
ESCROW SETTLEMENT — the credit-assignment fix above.
HERITABLE TRAITS — clone-to-clone behavioural variation for selection to act
on; today `traits` carries `{"node": name}` and nothing reads it back.
EFFICIENCY-WEIGHTED DISTILLATION — `Account.efficiency` is computed and
reported, but distillation does not weight a candidate skill by the credits its
source spent, so a wasteful success teaches exactly as strongly as a cheap one.
PROVENANCE ROLLBACK for the skill library — below.

**Q: If a run's provenance turns out bad after the fact, is what it taught
rolled back?**
A: No, and this is a gap rather than something designed around. The memo has
exactly this rollback: `_memo_admission` deletes an entry whose own `status`
says `completed` but whose run-store document disagrees. The library has no
equivalent. `_distill` calls `SkillLibrary.propose()` for every node that
passed, BEFORE the run's final status is set; `Skill.provenance_run` records
which run produced each piece of evidence, and nothing ever reads that field
back against the run's later status. A run cancelled mid-distillation, or later
marked `interrupted` by a control-plane shutdown, leaves evidence banked on a
skill with no path that retracts or re-checks it. It is bounded by the ordinary
evidence bar and the human gate — not by a provenance check built for this
failure.

### Operations

**Q: What happens when every provider is spent?**
A: The run PAUSES; it does not fail. That was a deliberate change. The pool
used to raise `NoCapacity` after thirty seconds of finding nothing available,
which is right for a per-minute bucket and wrong for a spent daily ration: the
capacity is back in four hours and the run would be throwing away a criterion,
a plan and half a level of finished work over a limit behaving exactly as
documented. So a wait longer than the pool can absorb becomes ONE run-level
pause: every in-flight agent parks on a single Event inside `pool.complete` —
before any step is committed, so a parked agent holds a consistent checkpoint —
the run state is flushed to disk, and a ticker wakes them when the ration
frees. `NoCapacity` still exists, for "every provider is backed off or
unavailable", which is a different statement from "capacity returns at a stated
time".

`--no-wait` is the escape hatch and raises `Paced` instead of parking, because
CI does not want to sit for four hours in a pipeline. It is opt-in rather than
the default, because the default has to be the thing that finishes.

**Q: What does a paused run look like to monitoring?**
A: This is the reason the pause is one object rather than N sleeps. Sixty-four
agents each sleeping four hours is the same wall clock and a completely
different operational picture: nothing says the run is waiting, nothing says
why, nothing says when it returns — and a parked run emits nothing, finishes
nothing and errors on nothing, which is indistinguishable from a hang to
everything watching.

So: `run_paused` is a gauge set to 1 while parked and back to 0 on resume;
`pauses` is a counter labelled by provider and DIMENSION (which window bound);
`pause_seconds_total` is a counter labelled by provider and REASON; one event
carries the binding provider and dimension; and a heartbeat
re-states "still waiting, ETA X" every 60 seconds, so a dashboard connecting
mid-pause is not blank for hours. A pause that re-forms inside
`EXTENSION_WINDOW_S` is reported as an EXTENSION of the same pause rather than
a fresh one, so an operator sees a moving ETA instead of a stutter that reads
as a crash loop; three extensions marks it stalled — still not a failure, by
decision. On the run document, `status` becomes `paused` with `paused_reason`
and `resumes_at`, and that is persisted, so the pause survives the process.

**Q: What is not measured yet?**
A: The list I would want a reviewer to hold me to.
- Prefix-cache saving. `cached_tokens` is 0 with `reported=False` under every
  offline provider, so there is no measured saving anywhere in this repo yet.
- Hoisted-versus-legacy node pass rate. The parity gate needs live providers
  and a corpus; it has not been run.
- Whether learning helps. The last measurement went the wrong way, the cause
  was fixed, and re-measurement is blocked on the daily quota.
- The real cache hit rate on unknown tasks. CAPACITY.md assumes 60% and names
  this as the assumption most likely to be wrong.
- Retrieval precision against a corpus. The boundary in Section 13 is a handful
  of probes I ran, not a precision figure.
- Anything at 500 agents against live providers.

There was also a flaky test here, and it is fixed rather than still open.
`tests/test_kill_resume_process.py` spawns three real processes and waits for
the first to park; it was written against a 120-second deadline, and the test's
own comment records that a loaded machine overran that often enough to fail it
about half the time. The fix was to raise the deadline to
600 seconds. That number is a CEILING ON PATIENCE, not an expected duration —
nothing waits longer than it needs to, and the test polls for the parked run
document and returns as soon as it appears. Timed three consecutive runs on an
idle box just now: **2.44s, 2.40s, 2.39s** of call time. It is not a flaky test
any more. Raising a deadline is not
always a fix, but here the assertion was about behaviour and the timeout was
only there to stop a hang, so a deadline tight enough to fire on scheduling
noise was measuring the machine rather than the product.

---

## Section 15: The learning loop, and why it never turned (answerable NOW)

**Q: Your eval reported "no measured improvement" for months. Was the system
not learning?**

It was never asked. Four separate mechanisms produced that sentence for reasons
unrelated to learning, and each one was found by looking at what the code
actually did rather than what it returned.

1. `swarmd eval` constructed both arms without a skill library, so
   `self.skills = skills if use_skills else None` was `None` in both. The arms
   were the same code. Fixed in the CLI on 2026-08-29.
2. `_eval_runner` in the control plane kept doing exactly that for four more
   days, so every eval started from the dashboard was a null-result generator.
3. The session trained on the same tasks the eval measured, so any improvement
   it did show would have been memorisation.
4. The success rate counted runs stopped by capacity as failures, so a sweep
   long enough to outlast the day's quota measured leftover quota and reported
   it as capability -- and the loss landed on whichever arm hit the wall, so the
   delta moved too.

With all four fixed it still produced nothing, and that is where the real answer
is: the library could not promote a single skill, ever, for two structural
reasons. ADR-014.

**Q: What were the two reasons?**

A skill becomes reviewable only once it is `promotable`: verified on two
DISTINCT task shapes. Both preconditions for clearing that bar failed.

*The corpus had no shared structure.* Twelve evaluated tasks with twelve
disjoint output shapes. An approach distilled from a median repair is never
proposed by a colour-ordering puzzle, so no skill ever accrued a second shape.
This is unreachable at any sample size -- a thousand unrelated tasks promote
exactly as many as twelve: none.

*Identity fragmented on wording.* `make_skill_id` hashes the instruction, the
instruction is written by a model, so the same approach came back phrased
differently every run and minted a fresh record starting again from one shape.

**Q: You tried the second fix alone first, measured it, and reverted it. Why
was that right, and why did you then put it back?**

Measured against the old corpus it changed nothing: grouping 22 records by
abstracted name and pattern still gave zero approaches with two shapes, because
no two tasks shared an approach in the first place. Shipping a change to what
"the same skill" means, on evidence that it fixes nothing, would have been
shipping on faith.

What the measurement showed is that merging is *insufficient*, not that it is
unnecessary. That distinction is the whole lesson. On a corpus with families it
becomes load-bearing: two tasks propose the same approach, phrase it
differently, and without merging the evidence lands in two records of one shape
each instead of one record with two. Both changes had to land together, and
neither alone would have turned the loop.

**Q: Why not lower `MIN_DISTINCT_TASKS` to 1? The bar is what is blocking you.**

Because the bar is the claim. `MIN_DISTINCT_TASKS = 1` means "this worked once"
is enough to put an approach in front of a reviewer and inject it into every
future run that retrieves it. That is not a transfer claim, and the retrieval
path fills with one-off advice -- which is the failure the bar was added to
prevent after the library filled with skills anchored to plan node names.

Lowering a bar because nothing clears it is how a metric stops measuring
anything. The corpus was wrong, not the bar.

**Q: What is `approach_key`, and why does it deliberately exclude the plan
step?**

It is the identity used for evidence accrual: a sha256 over the abstracted
artifact-shape name plus the closed vocabulary of criterion check kinds that
graded it. `skill_id` still hashes the instruction, so a stored record still
verifies against its own content.

The plan step is excluded because plans are synthesised per task. A key
containing the step can only ever match another proposal from the SAME task --
which is precisely the evidence the bar refuses to count. So a step-inclusive
key is not merely strict, it is structurally incapable of ever accruing the
second shape. That took a measurement to see: four proposals from one task,
four distinct step texts, no cross-task match available at any sample size.

**Q: Then two different steps of one task merge into one skill. Is that not a
bug?**

It is a stated cost, and it is asserted in a test so it stays deliberate. Two
steps merge only when they produce the same artifact shape under the same
checks -- and those two records were already competing for the same retrieval
slot, because the index terms come from the same name. The library was not
distinguishing them either. Which of two approaches survives is what the human
gate and success-rate pruning decide; `approach_key` only decides what counts
as one.

**Q: Why is the training set unexpressible in `EvalRequest` rather than just
documented as off-limits?**

Because a convention a person has to keep is what failed here twice in one day.
A session trained on the tasks an eval then measured, and nothing stopped it;
an eval ran with identical arms, and nothing stopped that either. Measuring over
the tasks a library was built from is memorisation, so `SessionRequest.arms`
accepts `train` and `EvalRequest.arms` does not. The pattern refuses it before
any code runs.

**Q: You started a 30-cell ablation and cancelled it at cell 5. Justify
spending nothing there.**

A free check first: neither approved skill retrieved for the task it was built
for. Across the five custom tasks the hit counts were 0, 0, 0, 1, 0 -- and the
one hit offered a permissions-diagnosis skill to a puzzle about the order of
coloured houses.

The cause is index size. `_idf` weights terms across approved skills; with two
of them it produced two distinct weights across 32 terms. An IDF with no spread
is not a weighting -- ranking collapses to raw term overlap. So the experiment
could not discriminate and its null was knowable in advance. Finishing it would
have spent roughly 500 requests, most of what the day had left, to confirm
something already established. The engineering call is to spend budget on
experiments that can distinguish between hypotheses.

**Q: So what is the minimum useful size of a skill library?**

Unmeasured, and now on the list as a measurement rather than an assumption.
What is established is that two is below it. This is a property of any TF-IDF
index, not of this system: with N documents, a term's IDF can take at most a
handful of values, and below some N the ranking carries no information.

**Q: The dashboard sent no operator token for weeks. How did the tests not
catch it?**

Because the tests exercised the server and the client separately, and the fault
was in the gap. Server tests asserted that a gated endpoint refuses a request
without a token -- true, and passing. The frontend typechecked and built. What
nothing asserted was that the browser sends one. Reads are ungated, so the page
rendered live provider data and looked entirely healthy right up to the moment
someone pressed a button.

The general shape: **two clients of one contract, one of them tested.** The same
shape produced the eval's missing skill library, fixed in the CLI four days
before the service.

**Q: Why does `/readyz` check the approval store when `/healthz` does not?**

Because the two probes answer different questions and a wrong answer to either
is expensive in a different way. Liveness asks "should this process be killed";
a dependency failure there turns one outage into two by restarting pods that
were working. Readiness asks "should new work come here", and a control plane
that cannot reach the approval store cannot run a session -- but the runs it is
already holding are fine.

That distinction was not theoretical. A session was accepted, spent a task's
provider quota, and only then hit `ConnectionRefusedError` out of
`_auto_approve`, because the store is not touched until the first
consolidation. Ready-looking service, dependency checked too late.

### Parameter questions

**Q: `MIN_DISTINCT_TASKS = 2`. Why two, and what does changing it cause?**

Two is the smallest number that makes the question "does this transfer?"
answerable at all: one task's evidence, drawn twice, is one observation. Setting
it to 1 turns the library into a record of things that worked once and puts
unvalidated advice into every future run. Raising it to 3 or more makes the bar
stricter and the corpus requirement harder -- with families of three, an
approach would need to recur across three members, which the current corpus
would supply only sometimes. Two is the point where the bar means something and
a realistic corpus can clear it.

**Q: Five families of three, now eight. Why three members?**

Two would be the minimum to supply a second shape, and gives no margin: if one
member fails its criterion, the family produces nothing. Three means one member
can fail and the approach still accrues its second shape. Beyond three the
returns fall off per unit of provider quota -- a fourth member of an existing
family adds a shape to approaches that already have two, while a new family adds
a document to a retrieval index that badly needs documents.

**Q: `retrieve(min_score=0.15, limit=3)` -- unchanged today. Are they still
right?**

`min_score` is unexamined by today's work and remains as documented: low enough
that a related skill with different phrasing surfaces, high enough that an
unrelated one does not. What today showed is that the floor cannot compensate
for an index with no discriminating power -- at two documents, scores are
term-overlap counts and the floor admits or rejects on that basis alone. The
parameter is not wrong; it is being asked to do a job that belongs to corpus
size.

`limit = 3` is a prompt-budget decision, unchanged: skills are injected into a
worker prompt, and prompt length is token cost on a quota-bound system.


### Design fault or model fault? (answerable NOW)

**Q: Your distilled skills scored 5 successes in 53 uses, and three of four
candidates carried their source task. Is that the model being bad at writing
advice?**

No, and the distinction decides whether the problem is fixable. Each class was
traced to the line that let it through (ADR-015), and none of them was the
model.

`stock_count` reached a skill because `strip_source_terms` compared WORDS
against the task's vocabulary, and that token is not in it -- "stock" and
"count" are. The abstraction tokenised prose and never considered that a writer
packs two words into one identifier.

`<NUMBER>! = 6` survived because the NUMBER pattern ended `(?![\w.])`. That
guard exists so `1.25` is not split in half; it also rejected the period that
ends a sentence, which made a number in final position invisible.
`"the answer is 42."` yielded no literals at all -- and the end of a sentence is
where an answer gets stated.

Advice about synthetic probes survived because distillation abstracts the PLAN
STEP, which is prose written for one task by a planner that knows the domain.
Abstraction removes literals and words the task used. It cannot remove domain
knowledge the planner introduced, because "domain" is not a structural
property: `probes` looks exactly like `parse` to a rule that does not already
know the subject.

In all three the model did what it was asked and the pipeline failed to enforce
the property it needed. Two are now fixed. The third is not deterministically
detectable, and saying so is more useful than shipping a filter that pretends
otherwise.

**Q: You built a detector for the third class and did not ship it. Why?**

It did not separate the cases. The signal was the share of an instruction's
content words appearing in neither the task nor the method lexicon -- the
symmetric partner to `strip_source_terms`, catching what the planner INVENTED
rather than what it borrowed. Scored against the four candidates already judged
by hand it read 0.41, 0.52 and 0.35 for the three rejects and 0.33 for the one
approval.

0.35 against 0.33 is not a threshold. Shipping it would have meant tuning a
float on four samples until it reproduced a judgement already made, which is
how a metric turns into a rationalisation for a decision taken on other
grounds. The measurement is recorded so the next person does not rebuild it.

**Q: So what stops a contaminated skill now?**

The control that already caught these: scoring and pruning. It worked -- both
skills were retired automatically with the reason on the record. It was slow,
because `prune` runs at consolidation, every N tasks, while a skill can be
retrieved by every node of every task in between. That is how one reached 26
uses at 0%.

Retrieval now applies the same rule against the same constants, so a skill past
`PRUNE_MIN_USES` with a success rate under `PRUNE_MIN_SUCCESS_RATE` is not
offered even before consolidation formalises the retirement. The exposure is
capped at the evidence threshold rather than at the consolidation interval.

**Q: Isn't the real answer to stop embedding the plan step's prose at all?**

It is *an* answer, and it would eliminate the class by construction: build the
instruction from the artifact shape, the check kinds and the method verbs, and
never from prose. No prose, no leaks.

It is not taken yet because the prose is what tells a worker HOW, and trading
all of it for cleanliness is a bet about which loses more -- contaminated advice
or uninformative advice. That is settleable by an ablation between the two
distillers, and this project's rule is that a bet of that shape gets measured
rather than argued. It needs a library large enough for the ablation to
discriminate, which is the same precondition G-4 is waiting on.

**Q: `generality` scored 1.00 on the worst candidate. What is it actually
measuring?**

Method-vocabulary density in the originating step -- how much of it was method
rather than the task restated. Contamination does not reduce that: an
instruction naming `stock_count` and `ledger_count` is still mostly method
words, so it scores high while being unusable.

That makes it a genuine signal about one thing and no signal at all about
another, which is worth knowing before trusting it in review. A reviewer
reading the instruction catches these; a reviewer reading the score does not,
and anything that automated approval on it would have admitted all three
rejects.
