# Runbook

**Status:** v1.0 · **Updated:** 2026-08-28

One entry per alert in `observability/alerts.yml`. The rule is that an alert
without a runbook entry does not get to exist — an alert nobody knows how to
act on is a notification, and notifications train people to ignore pages.

Each entry answers four questions in the same order: what is actually broken,
how to confirm it, what to do now, and how to stop it recurring.

**First, always:**
```bash
kubectl -n swarmd get pods,jobs
kubectl -n swarmd logs -l app.kubernetes.io/component=control-plane --tail=100
swarmd providers probe          # what capacity actually exists right now
```

---

## ProviderPoolExhausted

**Severity:** page · `max(swarmd_provider_available) == 0` for 2m

**What it means.** Every provider is backed off simultaneously. Runs are
blocked on `NoCapacity`. Individual providers backing off is routine; all of
them at once is not, and it usually has a single common cause rather than
several coincident ones.

**Confirm:**
```bash
swarmd providers probe                        # per-provider reason
kubectl -n swarmd exec deploy/swarmd-redis -- redis-cli --scan --pattern 'swarmd:quota:*'
kubectl -n swarmd get externalsecret swarmd-provider-keys -o yaml | grep -A5 status
```

**Likely causes, in order of how often they are actually it:**

1. **Credentials expired or rotated.** `probe` reports auth failures rather
   than rate limits. Check the ExternalSecret synced; note that `envFrom`
   values are fixed at container start, so a *successful* rotation still needs
   a restart to take effect: `kubectl -n swarmd rollout restart deploy/swarmd-control-plane`.
2. **Genuine daily quota exhaustion.** `probe` reports rate limits on every
   provider. Check the 24h burn panel on the cost dashboard. If a `eval` sweep
   ran, it consumed ~75% of the day's budget by design (see CAPACITY.md).
   **Action:** wait for the window, or enable `SWARMD_ALLOW_PAID=true` if the
   run matters more than the $0.05.
3. **Redis lost, quota degraded.** Logs show `quota backend degraded to local
   buckets`. Pods are now limiting at 25% of real rate, so throughput looks
   like exhaustion without actually being it. **Action:** restore Redis; the
   pool recovers on its own and logs `quota backend recovered`.
4. **Egress blocked.** `probe` reports transport errors on everything. A
   NetworkPolicy change or a NAT gateway problem. Verify from inside a pod.

**Prevent:** add Cerebras and OpenRouter to the pool. Two providers is a single
point of correlated failure; the capacity plan's trigger for adding one is a
sustained rate-limit ratio over 10%, which is a separate, earlier alert.

---

## RateLimitRatioHigh

**Severity:** ticket · >10% of calls rate-limited over 10m

**What it means.** The pool is absorbing throttling correctly, but throughput
is below what runs were sized for. Not urgent — nothing is failing.

**Confirm:** overview dashboard, "Rate-limit rejections" panel, split by
provider. One provider dominating means a provider-specific problem; evenly
spread means demand genuinely exceeds supply.

**Do now:** nothing urgent. If a demo is imminent, reduce `--agents` or use the
`smoke` profile — throughput per run drops but the run completes.

**Prevent:** this is the capacity plan's documented trigger for adding a
provider. Cerebras first: largest free daily quota, no card, different company
(so not a correlated failure with the existing two).

---

## CacheHitRateCollapsed

**Severity:** ticket · <40% hit rate over 15m

**What it means.** The capacity plan assumes ~60%. Below 40%, the `demo`
profile no longer fits its 12–18 minute budget and everything feels slow.

**Confirm:**
```bash
# Cache savings should track call volume; a flat savings line with rising
# call volume is the signature of a normalisation regression.
```
Compare the cache-savings panel against call rate on the cost dashboard.

**Likely causes:**
1. **A prompt now embeds something unique per call** — a timestamp, a run id, an
   agent id. This is the usual cause and it is almost always a recent change to
   a prompt template. Check the last few commits touching prompts.
2. **Genuinely novel workload.** A curriculum that has moved to unfamiliar task
   types will legitimately miss. Verify against the eval task distribution
   before treating it as a bug.
3. **Someone re-enabled similarity matching.** `CachedProvider` requires
   `exact_only=True` and raises otherwise, so this shows up as a startup error
   rather than a silent regression — but if you see it, do not "fix" it by
   relaxing the constructor. Similarity matching on these prompts measured 0.97
   between genuinely different plan nodes and served them each other's answers.
   See the docstring on `router/cache.py`.

**A caveat on this alert.** A low hit rate on genuinely novel tasks is CORRECT,
not a regression: exact keying means identical prompts hit, and unknown work
does not repeat. Check what the run was doing before treating a miss rate as a
fault. Batching, not caching, is what carries the capacity plan.

**Do NOT** respond by adding provider capacity. That hides a real regression and
makes it permanent.

---

## BatchingDegraded

**Severity:** ticket · `batch_calls_saved_total` flat while agent count is high

**What it means.** Generation has fallen back to one call per agent. The run
still completes; it costs N times the requests it should, and requests are the
scarce resource on a pooled free tier.

**Confirm:** look for `batch generation failed for <node>` in the logs, and the
`batch_failed` event in the run's stream.

**Likely causes:**
1. **The provider is erroring on the batched request** — usually `max_tokens`.
   The batch scales `max_tokens` by K and caps at 8192; a model with a smaller
   output limit will reject it. Reduce the agent count for that run.
2. **The model stopped honouring the separator.** The parser degrades to fewer
   variants rather than failing, so the symptom is `variants` well below
   `requested` in the `batch_generated` event. Diversity drops; correctness does
   not.

**Not a cause for alarm on its own:** a `CeilingExceeded` inside a batch is
re-raised deliberately and aborts the run rather than falling back — falling
back there would spend more, one call at a time, past the limit that just
fired.

---

## CostCeilingApproaching

**Severity:** ticket · run spend past 70% of the ceiling

**What it means.** Free-tier traffic should hold spend at exactly zero, so any
meaningful spend means paid overflow is carrying load. That is a *routing*
question, not a budget one — the interesting thing is why free capacity was
unavailable, not that money was spent.

**Confirm:**
```bash
# Cost by provider, straight from the ledger -- authoritative, unlike the
# dashboard, because every figure is a sum over rows (ADR-007).
swarmd ledger report --run <run_id>
```

**Do now:** nothing to the ceiling. Find out why free capacity ran out — the
same investigation as `RateLimitRatioHigh`, since paid overflow is only reached
when the free tiers are exhausted or unavailable.

**Prevent:** the same fixes as `RateLimitRatioHigh`.

---

## CeilingAbort

**Severity:** ticket · a run stopped rather than overspending

**What it means.** The ceiling did its job. This alert is not a failure report;
it is the record of a control working, and the run's itemised report is the
useful artifact.

**Confirm:**
```bash
swarmd ledger report --run <run_id>    # which stage consumed the budget
```

**Do now:**
- Read the itemised report before touching anything. A run that aborted at the
  ceiling produced a partial result and said so; it did not truncate silently.
- If the ceiling is genuinely too low for the workload, raise
  `SWARMD_COST_CEILING_USD` **deliberately and in a commit**, not as a hotfix.
  The value being a decision is the entire point of having it.
- If spend climbed faster than expected, check `BatchingDegraded` first: a run
  that fell back to one call per agent costs N times the requests, and on a paid
  tier that is also N times the money.

---

## DeadLetterRateHigh

**Severity:** ticket · >20% of items dead-lettering over 15m

**What it means.** The bounded repair loop is not converging. Items are failing
the frozen criterion repeatedly and giving up.

**Confirm:** gate-outcomes panel, split by outcome. Then read the frozen
criterion for the affected run — its hash is a run output:
```bash
swarmd run inspect <run_id> --criterion
```

**Likely causes:**
1. **The criterion is too strict.** Adversarial synthesis produced something
   nothing can satisfy. Confirmed if dead-lettering is near 100% and started at
   run begin. **The criterion cannot be changed mid-run** — it is frozen by
   design (ADR-009). Abort, and let re-authoring happen on the next run.
2. **Model quality regression.** A provider silently changed a model behind an
   alias, or the pool is routing to a weaker fallback. Check which model served
   the failures in the ledger.
3. **A genuinely hard task.** Legitimate. The dead-letter rate is information,
   not a fault.

---

## RecoveryPathDead

**Severity:** page · kills continuing with requeues at exactly zero

**The only alert here that indicates a correctness bug rather than a capacity
problem.** Killed agents holding claimed work should have it requeued with the
checkpoint intact. Requeues sitting at exactly zero while kills continue means
the recovery path is not running at all, which invalidates SLO-2 and the
guarantee the whole system rests on.

**Why the condition is "zero" and not "kills exceed requeues".** Kills
legitimately exceed requeues: an agent killed while idle has no claimed work to
requeue. A kill-rate 0.9 kernel demo produces 595 kills against 404 requeues
with a matching integrity hash. An earlier version of this alert used a gap
threshold and would have paged during entirely healthy chaos.

**Confirm:**
```bash
swarmd ledger verify --run <run_id>     # memory vs durable rows
kubectl -n swarmd logs -l app.kubernetes.io/component=run --tail=200 | grep -E 'requeue|lease|checkpoint'
```

**Do now:**
1. **Stop chaos** on running work: `SWARMD_CHAOS_KILL_RATE=0`, restart the run.
   Losing work slowly is worse than pausing.
2. Capture the ledger and logs before anything is restarted. This is a
   correctness bug; the evidence is the valuable part and a restart destroys it.
3. Treat as a release blocker. SLO-2 has no error budget.

**Likely causes:** a checkpoint write failing silently; a lease expiring while
a step is genuinely still running (heartbeat interval vs lease duration drift);
a step that is not idempotent on resume.

**Prevent:** the CI chaos gate exists to catch exactly this before release. If
this fired in production, the gate has a coverage hole — find it and add the
case.

---

## UnexpectedContainments

**Severity:** ticket · containments outside a seeded-rogue run

**What it means.** The red-team organ contained real agents. Expected during
seeded-rogue tests; otherwise the population has evolved something the
detectors consider rogue.

**Confirm:**
```bash
swarmd run inspect <run_id> --containments   # immutable audit trail
```

**Reproduce the detectors deliberately** before concluding one is broken:

```bash
# Every pattern, injected into a real run. Exit code is the verdict.
swarmd swarm run "<task>" --profile smoke --seed-rogues all

# One pattern, to narrow down which detector regressed.
swarmd swarm run "<task>" --profile smoke --seed-rogues budget_siphon
```

Or `POST /api/runs {"seed_rogues": "all"}`, which is the same gate through the
service. A pass requires each pattern to be handled **by its own detector**:
containment by a different one fails, because it proves one detector twice and
another not at all.

**Read the audit before acting.** Two very different situations:
- **True positive** — selection pressure found an exploit. This is the system
  working, and the containment record is the most interesting artifact the run
  produced. Feed the pattern into the eval suite.
- **False positive** — a threshold is too tight and productive agents are being
  killed. Contained agents keep their checkpoints, so nothing was lost, but
  throughput suffered. Tune the threshold in a commit with a documented reason.

---

## ApprovalQueueStale

**Severity:** ticket · pending approvals unchanged for 24h

**What it means.** A non-zero queue is normal — it is a human queue by design.
A queue that never *drains* means the loop is blocked on a person who probably
does not know they are the bottleneck.

**Confirm:** `swarmd list`

**Do now:** review and decide. For skill-library approvals, remember what the
gate is for: it stands between a proposed skill and every future run that would
inherit it. Approving in bulk to clear the queue defeats the control.

---

## Deploy and rollback

```bash
# Deploy: digests only, never tags -- a tag can be repointed after it was tested
kubectl -n swarmd apply -k deploy/k8s/overlays/prod
kubectl -n swarmd rollout status deploy/swarmd-control-plane --timeout=5m

# Rollback
kubectl -n swarmd rollout undo deploy/swarmd-control-plane
kubectl -n swarmd rollout status deploy/swarmd-control-plane --timeout=5m
```

**After a rollback, re-apply from the manifests you rolled back TO.** `rollout
undo` changes the live Deployment without updating the
`kubectl.kubernetes.io/last-applied-configuration` annotation, so the next
`kubectl apply` re-applies the version you just rolled back from. kubectl warns
about this and the warning scrolls past in an incident. The rollback is not
finished until git and the cluster agree.

**Verified, not assumed.** This sequence was exercised end to end against a
real cluster: v1 deployed and Ready, v2 rolled out, `rollout undo` restored v1,
with `rollout status` gating each step and `maxUnavailable: 0` holding capacity
throughout.

**In-flight runs survive a control-plane rollout.** Runs are Jobs with their
own pods; replacing control-plane pods does not touch them, and all run state
lives in Postgres and Redis. This is the reason the control plane is stateless,
and it is worth verifying it stays true — a deploy that interrupts a 4-hour
eval sweep wastes 75% of a day's provider quota.
