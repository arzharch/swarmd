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
A: It would be if I claimed a thousand simultaneous model calls. I measured the ceiling:
pooling Groq, Cerebras, Google AI Studio, Mistral and OpenRouter free tiers gives about
86,000 TPM, roughly 34 LLM calls a minute. So the honest claim is a thousand agents of
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

## Section 7: Red-team and containment (populate as Phase 8 lands)

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

**Q: What if agents learn to evade the detectors?**
A: Structurally, nothing prevents it, and I would rather say so than pretend otherwise.
The mitigations are that thresholds are not exposed to agents, that library poisoning is
judged against the control arm rather than self-reported metrics, and that a human approval
gate stands between a proposed skill and the library.

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

## Section 10: Unknown-task runs (populate as Phases 6-7 land)

**Q: What broke first at 500 agents?**
A: (to fill - keep a raw failure log; this question decides the interview)

**Q: Show me a synthesized DAG that was wrong, and what caught it.**
A: (to fill - structural validation rejections, with examples)

**Q: What did the cache hit rate actually turn out to be on unknown tasks?**
A: (to fill - the capacity plan assumes 60%; this is the assumption most likely
to be wrong, and the wall-clock budget depends on it)
