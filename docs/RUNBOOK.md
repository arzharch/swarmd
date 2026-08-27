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
3. **Similarity threshold changed.** Check the configured value against the
   0.95 documented in `interview_prep.md`.

**Do NOT** respond by adding provider capacity. That hides the regression and
makes it permanent; the cache is a 2.5x multiplier and buying your way past a
lost multiplier is expensive and temporary.

---

## CostCeilingApproaching / CeilingAbort

**Severity:** ticket

**What it means.** Free-tier traffic should hold spend at exactly zero. Any
meaningful spend means paid overflow is carrying load. That is a *routing*
question, not a budget one — the interesting thing is why free capacity was
unavailable, not that money was spent.

**Confirm:**
```bash
# Cost by provider, straight from the ledger -- authoritative, unlike the dashboard
swarmd ledger report --run <run_id>
```

**Do now:**
- Ceiling abort is working as designed: the run stopped rather than
  overspending. Read the itemised report to see which stage consumed the budget.
- If the ceiling is genuinely too low for the workload, raise
  `SWARMD_COST_CEILING_USD` **deliberately and in a commit**, not as a hotfix.
  The value being a decision is the entire point of having it.

**Prevent:** the same fixes as `RateLimitRatioHigh` — paid overflow is only
reached when free capacity runs out.

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

**Severity:** ticket · containments outside a `--seed-rogues` run

**What it means.** The red-team organ contained real agents. Expected during
seeded-rogue tests; otherwise the population has evolved something the
detectors consider rogue.

**Confirm:**
```bash
swarmd run inspect <run_id> --containments   # immutable audit trail
```

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
```

**In-flight runs survive a control-plane rollout.** Runs are Jobs with their
own pods; replacing control-plane pods does not touch them, and all run state
lives in Postgres and Redis. This is the reason the control plane is stateless,
and it is worth verifying it stays true — a deploy that interrupts a 4-hour
eval sweep wastes 75% of a day's provider quota.
