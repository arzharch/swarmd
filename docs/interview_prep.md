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

## Section 2: Phase 1 — Kernel (populate as you build)

**Q: Walk me through the checkpoint contract.**
A: (to fill — what's in a checkpoint, how resume skips steps deterministically)

**Q: How does heartbeat expiry avoid double-processing?**
A: (to fill — atomic claims, lease timing, idempotent effects)

## Section 3: Phase 2 — Pipeline & harnesses (populate as you build)

**Q: What exactly is a harness vs an agent vs a stage?**
A: (to fill — harness = toolset+prompt+loop policy; agent = running instance; stage = pool + verifier + policy)

## Section 4: Phase 3 — Quality & HITL (populate as you build)

**Q: What happens when a verifier is wrong?**
A: (to fill — dead-letter visibility, repair loop bounds, supervisor escalation)

## Section 5: Phase 4 — Model routing (populate as you build)

**Q: How does semantic caching avoid wrong hits?**
A: (to fill — threshold choice, false-hit cost analysis)

## Section 6: Phase 5 — LeadOps (populate as you build)

**Q: Where does the data come from and is scraping ethical here?**
A: (to fill — open datasets, robots-aware fetching, committed fixtures for offline runs)

**Q: What did the Supervisor catch and fix?**
A: (to fill — THE demo story; keep a raw log of real interventions)

## Section 7: Phase 6 — Observability & benchmarks (populate as you build)

**Q: What broke when you added chaos to every stage?**
A: (to fill — keep a raw failure log; this question decides the interview)
