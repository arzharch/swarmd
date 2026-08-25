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
A: A multi-agent orchestration runtime that runs staged pipelines of harnessed agents
with checkpoint/resume recovery, quality gates between stages, durable human-in-the-loop
approvals, and end-to-end tracing — proven by a production-shaped sales-operations engine.

**Q: Why multi-agent instead of the 1000-agent story?**
A: Because headcount isn't the hard problem. Coordination, quality control, human
approvals, and recovering from failed state are — and those only show up when agents have
purposeful work in dependent stages. I still run tens of agents concurrently and benchmark
the parallel speedup honestly; I just don't claim scale I can't make meaningful.

**Q: What do the agents actually DO?**
A: In the flagship LeadOps engine: enrichment agents fetch public signals and normalize
messy lead data; dedupe agents merge duplicates across thousands of records using
embeddings plus LLM confirmation; scoring agents apply an ICP rubric with structured
output; multiple outreach agents draft personalized emails concurrently; QA verifiers
check everything; a Supervisor deep-agent samples failures and patches prompts fleet-wide.

**Q: How does recovery from failed state actually work?**
A: Every agent checkpoints at step boundaries. Claims expire via heartbeat when an agent
dies; expired tasks requeue WITH their checkpoint, so a replacement agent skips completed
steps deterministically. Proven per-stage by chaos kills — final database integrity hash
must match the clean run.

**Q: How do you prevent bad output flowing downstream?**
A: Every stage has a verifier gate. Failures enter a bounded repair loop, then requeue,
then dead-letter with full trace reference — never silently forwarded. Each run produces
a quality report with pass rates and failure taxonomy.

**Q: How does human-in-the-loop survive a restart?**
A: Approval is a durable pipeline state (AWAITING_APPROVAL), not an in-memory callback.
Kill the process at review time, restart, state is intact; approve/reject via CLI; every
decision audited. And outreach never auto-sends — that's a hard product boundary.

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

## Section 6: Phase 5 — LeadOps (populate as you build)

**Q: Where does the data come from and is scraping ethical here?**
A: (to fill — open datasets, robots-aware fetching, committed fixtures for offline runs)

**Q: What did the Supervisor catch and fix?**
A: (to fill — THE demo story; keep a raw log of real interventions)

## Section 7: Phase 6 — Observability & benchmarks (populate as you build)

**Q: What broke when you added chaos to every stage?**
A: (to fill — keep a raw failure log; this question decides the interview)
