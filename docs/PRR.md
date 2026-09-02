# Production Readiness Review

**Status:** v1.1 · **Updated:** 2026-09-01 · Reviewed before any production deploy

Section 11 is a dated re-review. Section 10's verdict stands as written on
2026-08-28 and is not edited in place -- a review rewritten to match what was
later found is a review nobody can check.

A PRR is a checklist you fill in honestly or not at all. Items are marked
**PASS**, **PARTIAL**, or **BLOCKED**, and the partials are the useful part —
a review with no partials has usually been graded generously.

---

## 1. Architecture and dependencies

| Item | State | Evidence / gap |
|---|---|---|
| Failure domains documented with blast radius | PASS | [DEPLOYMENT §6](DEPLOYMENT.md) |
| Every external dependency has a degradation path | PASS | Provider → pool reroutes; Redis → local buckets at 25%; RDS → in-flight runs hold checkpoints; frontend → runs unaffected |
| Stateless control plane | PASS | All run state in Postgres/Redis; in-flight runs survive a control-plane rollout |
| No single point of failure that loses data | PASS | Multi-AZ RDS in prod; ledger fsynced per row; Redis holds only 5-min TTL quota |
| Single-run distribution across pods | **BLOCKED (by design)** | PRD §6 non-goal. Horizontal scale = more concurrent runs. Claiming otherwise would need a distributed scheduler nobody has built |

## 2. Capacity

| Item | State | Evidence / gap |
|---|---|---|
| Bottleneck identified and measured | PASS | [CAPACITY.md](CAPACITY.md) §1: provider limits, not CPU. For Groq specifically it is TPM that binds before RPM — the 200,000-token/model/day cap is spent around 200 requests while the 1,000-request cap is still four-fifths unspent, so a plan that counts requests and ignores tokens overstates Groq fivefold |
| Load model derived, not guessed | PASS | Fixed + variable call budget per run profile |
| Saturation behaviour designed | PASS | Cache → reroute → backpressure → paid overflow → clean abort. Never silent truncation |
| Headroom triggers defined | PASS | CAPACITY §6: rate-limit ratio >10% ⇒ add a provider |
| Assumptions listed with falsifying metrics | PASS | CAPACITY §7. The 60% cache-hit assumption is the one most likely to be wrong |
| Load test at production scale | **PARTIAL** | 500-agent runs exercised against the simulated provider only. A real 500-agent run needs live keys and a full day's quota |

## 3. Observability

| Item | State | Evidence / gap |
|---|---|---|
| Golden signals instrumented | PASS | `observability/metrics.py` — traffic, errors, latency, saturation |
| Cost as a first-class signal | PASS | Per-provider spend, cache savings, ceiling aborts |
| Cardinality policy enforced | PASS | Test asserts no `run_id`/`agent_id` labels |
| Metrics vs. reporting boundary stated | PASS | Prometheus operates; the ledger reports. Written at the top of the module |
| Dashboards provisioned as code, read-only | PASS | `observability/grafana/`, `allowUiUpdates: false` |
| Distributed tracing | PASS | OTel bridge with correct parent linkage → one Jaeger trace per run |
| Structured logs | PASS | JSON formatter with `extra` promoted to fields and credential redaction; the ConfigMap setting is now read |

## 4. Alerting and on-call

| Item | State | Evidence / gap |
|---|---|---|
| Every alert is actionable | PASS | Rule for inclusion in `alerts.yml` |
| Every alert has a runbook entry | PASS | Enforced by a test, not a convention |
| Alerts do not fire during healthy operation | PASS | Long `for:` windows. One false positive was found and fixed: kills legitimately exceed requeues, so the gap-threshold alert would have paged during healthy chaos |
| Page vs. ticket severity separated | PASS | Only pool exhaustion and a dead recovery path page |
| On-call rotation | **BLOCKED** | Single maintainer. Stated rather than pretended |
| Request identity for incident reconstruction | PASS | Request id on every response and log line |

## 5. Reliability

| Item | State | Evidence / gap |
|---|---|---|
| SLOs defined and measurable | PASS | [SLO.md](SLO.md), all four computed from the ledger |
| Error budget policy agreed in advance | PASS | SLO §"Error budget policy" — decided when nobody is under pressure |
| Chaos testing in CI | PASS | kill-rate 0.9, integrity hash equality, blocks release |
| Chaos in production | PASS | On by default. Off would make prod the one place the guarantee is untested |
| Graceful shutdown | PASS | 120s termination grace, preStop drain delay |
| Recovery verified, not assumed | PASS | Kill-and-resume with byte-identical output |

## 6. Security

| Item | State | Evidence / gap |
|---|---|---|
| No secrets in the repository | PASS | ExternalSecret + IRSA; no `secret_version` in Terraform state; a test asserts no keys in manifests |
| Restricted Pod Security Standard | PASS | Namespace-enforced; non-root, read-only rootfs, dropped capabilities, seccomp |
| Default-deny network policy | PASS | Cloud metadata endpoint explicitly blocked |
| Least-privilege RBAC | PASS | Jobs + pod reads only; no Secret access for the control plane |
| Untrusted code isolated | PASS | Subprocess sandbox, stripped env, resource caps, path-traversal refusal |
| Image scanning in CI | PASS | Trivy, fails on HIGH+, SBOM generated |
| Sandbox is a real boundary | **PARTIAL** | Defence in depth, not a security boundary against a determined attacker. The container is the actual boundary, and the module says so rather than implying more |
| Egress restricted to known destinations | **PARTIAL** | Currently 443 to anywhere except RFC1918 and metadata. Should narrow to provider CIDRs — named as the first thing to tighten |
| Access control on the run API | PASS | Operator token on every mutating endpoint and the event stream (ADR-013); refuses to start bound off-host without one |
| Service not publicly routable | PASS | Ingress source allowlist failing closed in the base; no LoadBalancer or NodePort points at the control plane, asserted by a test |
| Request limits | PASS | Body cap before parsing, sliding-window rate limit as a quota defence, edge limits in the Ingress |
| Attack surface minimised | PASS | Interactive API docs disabled outside dev — a live client for every endpoint served without the token |
| Secrets kept out of logs | PASS | Redaction on both formatters; sensitive field names replaced wholesale |
| Data retention documented | PASS | [SECURITY.md](../SECURITY.md) section 5, per data class |

## 7. Deployment

| Item | State | Evidence / gap |
|---|---|---|
| Infrastructure as code | PASS | Terraform, remote state with locking |
| Images pinned by digest in prod | PASS | A tag can be repointed after it was tested |
| Progressive rollout with automatic rollback | PASS | DEPLOYMENT §4, triggers agreed in advance |
| Config changes roll pods | PASS | kustomize ConfigMap hash suffix |
| Database migrations are expand/contract | PASS | Documented; the moment you most want to roll back is right after a migration |
| Environment parity | PASS | Same manifests, overlays differ only where justified |
| Rollback tested | PASS | Exercised on a real cluster (k3s, k8s 1.31): v1 deployed and Ready, v2 rolled out, `rollout undo` restored v1, each step gated on `rollout status`. `maxUnavailable: 0` held throughout. Not yet exercised on EKS, where the difference is the load balancer's deregistration delay, not the rollback |

## 8. Cost

| Item | State | Evidence / gap |
|---|---|---|
| Hard spend ceiling enforced in code | PASS | Harness boundary, checked before and after each call |
| Unknown models refuse rather than defaulting to free | PASS | A zero-priced model silently disables the ceiling |
| Cost attributable | PASS | Ledger rows carry provider, model, stage, agent |
| Infrastructure budget with forecast alerting | PASS | Terraform budget at 80% forecast, not only 100% actual |
| Cost/benefit stated honestly | PASS | Infrastructure is ~5,600× inference. Recorded rather than buried |

## 9. Documentation

| Item | State | Evidence / gap |
|---|---|---|
| Architecture decisions recorded, including reversals | PASS | 15 ADRs; ADR-001 superseded, ADR-004 and ADR-006 amended, ADR-014 and ADR-015 added 2026-09-01 for the learning loop |
| Runbook per alert | PASS | [RUNBOOK.md](RUNBOOK.md) |
| Capacity plan with dated measurements | PASS | [CAPACITY.md](CAPACITY.md) |
| Decision log with alternatives and follow-ups | PASS | [flow.md](flow.md) |
| README describes what shipped | PASS | Rewritten against reality, including what is not yet claimed |

## 10. Verdict

**Not production-ready, and the gaps are the reason rather than a formality.**

Blocking for a real production deployment:

1. **No live-provider validation at scale.** Everything at 500 agents has run
   against the simulated provider. The capacity plan's central assumption — a
   ~60% cache hit rate — is untested on real workloads, and the whole wall-clock
   budget depends on it.
2. **No on-call rotation.** A system with paging alerts and one maintainer has
   alerts, not on-call.
3. **Egress is wider than it should be.** 443-to-anywhere is the loosest control
   in the deployment and the first to narrow.

Explicitly NOT blocking, though it looks like it should be: **no user
authentication.** That is a decision for a single-tenant operator-run service,
documented in ADR-013 with compensating controls that are tested rather than
described. The day a second operator needs access, an authenticating proxy at
the Ingress is a deployment change rather than a rewrite, because the boundary
was put at the edge deliberately.

Ready for a **demo and evaluation deployment** now: the loop runs end to end,
chaos integrity holds, cost is bounded, containment works, and every number
traces to a ledger row.

The honest summary is that the *operational* work is further along than the
*empirical* work. The measurement apparatus is built and tested; the
measurements themselves need provider quota and time. That ordering was
deliberate — building the curve before the thing that makes the curve
trustworthy is how unfalsifiable claims get made.

---

## 11. Re-review, 2026-09-01

Six roles, one question each, answered against the state of the tree rather
than against section 10. Where a role's blocker from v1.0 no longer holds, it
says so and why; where it holds, it says that too.

### Product

**Does it do the thing on the tin?** Partly, and the part that does not is
now the only interesting one.

The claim is: generic agents take a task nobody scoped for them, author their
own success criterion, decompose it, solve it, and get better at it. The first
four happen, on live providers, at $0.00, with every number traceable to a
ledger row. Both held-out tasks ran end to end with no code change on
2026-09-01 -- one produced the right answer on 15 of 20 nodes, one produced a
wrong answer on 0 of 30. That is honestly "takes an unseen task end to end",
not "solves it".

The fifth -- gets better -- has never been measured, and until today nobody
could say why. Now it can be said precisely: the skill library could not
promote anything, for two structural reasons unrelated to quota (ADR-014).
Fixed; the loop turns; the measurement itself is still outstanding.

**Verdict: PARTIAL.** Ship-blocking for the improvement claim only. Everything
else the product says it does, it demonstrably does.

### QA

**What would I refuse to sign?** Three things, down from four.

1. **The learning claim.** Two approved skills exist for the first time, on
   their own evidence. Whether retrieving them helps is unmeasured, and the
   first attempt was stopped deliberately: an IDF index over two documents has
   two distinct weights across 32 terms, so it ranks by term overlap and
   offered a permissions-diagnosis skill to a colour-ordering puzzle. An
   experiment on that index cannot discriminate.
2. **Scale.** Every live run has been `smoke`. `standard` and `deep` are sized
   against the measured budget and have never met a real provider.
3. **Volume.** The success rate rests on single-digit task counts.

Item 2 changed the same day it was written: a `standard`-profile chaos run went
through on live providers — 25 of 25 nodes, 34 agents with 9 killed and resumed
from checkpoint, integrity hash intact, $0.00. What has still never met a real
provider at that scale is `deep`.

Down from four because the ablation now compares two genuinely different
configurations -- it did not, over HTTP, until 2026-09-01, and every eval ever
started from the dashboard before that compared a configuration against itself.

**What I will sign:** the test suite means something. 1,280 tests, and the ones
added this week were each verified to fail against the code they pin by
stashing it. Four accounting defects and four measurement defects were found by
asking what the ledger contains rather than what the code returns.

**Verdict: CONDITIONAL.** Sign for evaluation. Do not sign the improvement
claim.

### SRE

**Would I carry the pager?** Not for production, and not for the reason v1.0
gave.

v1.0's blocker 3 -- egress 443-to-anywhere -- is **closed**: the NetworkPolicy
now allows provider CIDRs, with the failure mode written next to it (a provider
changing ranges looks like an outage). Blocker 2, no on-call rotation, stands
unchanged: alerts plus one maintainer is not on-call, and no code fixes that.

New since v1.0, and found by running the thing rather than reading it: a
session was accepted, spent a task's provider quota, and only then discovered
the approval store was unreachable, because that store is not touched until the
first consolidation. `/readyz` now checks it and `POST /api/sessions` refuses
before spending. That class of fault -- ready-looking service, dependency
checked too late -- is exactly what a readiness probe is for.

Also: port 8000 lands inside a Windows Hyper-V reserved range on at least one
developer machine, and uvicorn dies at bind with `[winerror 10013]`. Documented
in the README and the runbook rather than worked around.

**Verdict: PARTIAL.** Fine for a single-operator evaluation deployment. Not
production without a second person.

*Added after the re-review:* the acceptance-scale chaos run has now happened
against live providers, so v1.0's blocker 1 — "everything at 500 agents has run
against the simulated provider" — is narrowed rather than closed. 34 agents
recovered under chaos on real traffic; the 500-agent claim remains untested.

### Security

**What is the worst an outsider can do?** Unchanged and still bounded: burn
provider quota, read prompts and artifacts in flight, corrupt the approval
audit trail. ADR-013's compensating controls hold and are tested.

One finding, and it is the good kind. The dashboard sent no operator token at
all, so on a gated control plane every mutating request was refused 401 and the
event stream closed 1008. That is a **usability** failure of a security control,
not a hole -- the gate held; the browser simply could not pass it, and nothing
in the UI could say so. Fixed, plus `GET /api/auth` so a locked control plane
looks locked instead of looking healthy.

Worth naming as an accepted risk: the two skills approved today were approved by
the operator running this work. They met their evidence bar on their own -- both
records carry an empty `approval_note`, so no `force` and no bypass -- but the
human gate is only as strong as the human, and here that human is the same party
that built the corpus.

**Verdict: PASS for the stated threat model.** ADR-013 remains the right call
for one principal.

### AI engineering

**Is the measurement apparatus trustworthy?** More than it was this morning,
and that is not a compliment to this morning.

Three ways it was producing numbers that described something other than what
they claimed, all fixed today: an ablation whose arms were the same code over
HTTP; a session that trained on the tasks the eval then measured; a success rate
that counted runs stopped by capacity as failures, so a long sweep measured
leftover quota and reported it as capability.

The structural fix is the interesting one. Measuring over the training set is no
longer discouraged, it is **unexpressible**: `SessionRequest.arms` accepts
`train`, `EvalRequest.arms` does not. A convention a person has to keep is what
failed here twice in one day.

Remaining known-unknown: the retrieval index's minimum useful size. Two
documents is demonstrably below it. Where the threshold sits is itself
unmeasured, and now on the list as a measurement rather than an assumption.

**Verdict: PARTIAL.** The apparatus is sound. The measurements are outstanding.

### Backend

**Would I take this codebase on?** Yes, with one reservation.

Determinism is real and enforced -- sha256 everywhere `hash()` would have been,
stdlib-only in the matching path, no model in any decision that has to be
reproducible. Idempotency is a contract with tests rather than a header that is
sometimes honoured. The ledger is genuinely append-only and every reported
figure is a sum over it.

The reservation is drift between the CLI and the service. Both are clients of
the same code, tests covered the CLI, and two fixes landed on one side only --
the eval's missing skill library sat unfixed in the control plane for four days
after being fixed in the CLI, and every dashboard eval in that window was a
null-result generator. The new tests pin both paths, but the pattern is worth
watching: **two clients, one of them tested.**

**Verdict: PASS with a watch item.**

---

## Go / no-go, 2026-09-01 (revised at end of day)

The bar was restated during the day and the revision matters: **not 500 agents,
but 5-10 agents running correctly on a task nobody scoped.** Anyone wanting the
500-agent number can supply their own credentials and measure it. What follows
is judged against the stated bar.

### Measured, not asserted

Five tasks belonging to no suite in this repository, live providers, skills
disabled:

| task | size | nodes solved | agents passed |
|---|---|---|---|
| bakery waste flags | `--agents 5` | 7/7 | 14/14 |
| library overdue fines | `--agents 8` | 4/4 | 8/8 |
| server uptime shortfall | `--agents 10 --chaos` | 5/5 | 10/10 |
| warehouse box counts | `--agents 8` | 7/7 | 14/14 |
| parallel scheduling | `--agents 8` | 4/4 | 7/8 |

Plus a `standard`-profile chaos run: 25 of 25 agent outcomes across 5 nodes, 34
agents with 9 killed mid-run and resumed from checkpoint without re-buying the
work, integrity hash intact.

Every run $0.00, every criterion authored and frozen by the system itself,
every artifact traceable to ledger rows.

| | |
|---|---|
| **Evaluation deployment** | **GO** |
| **The stated product bar (5-10 agents, unknown task)** | **GO** — five for five |
| **Production deployment** | **NO-GO** — one maintainer, no on-call rotation |
| **Publishing an improvement claim** | **NO-GO** — deliberately unmade, see below |

### Sign-offs

**Product: GO.** The claim is that generic agents take a task nobody scoped,
author their own success criterion, decompose it and solve it. Five unknown
tasks at the stated sizes did exactly that, including one that had failed
totally twice before its cause was found.

**QA: GO for the stated bar, with one number withdrawn.** The `nodes_passed`
field counted AGENT OUTCOMES under a node's name, so every run this system has
reported was understated -- a task reading "7/8 nodes" had solved all four of
its nodes, with one agent of a pool of two failing. Nodes and agents are now
counted separately. The holdout `hold-schedule-1`, recorded here this morning
as 15/20, was 4 of 4 nodes.

**SRE: GO for single-operator evaluation, NO-GO for production.** Unchanged and
not a code problem: alerts plus one maintainer is not an on-call rotation.
Everything else moved -- egress narrowed to provider CIDRs, the approval store
now gates readiness after a session was found spending quota before discovering
it could not reach one, and recovery under chaos is verified on real traffic
rather than a deterministic mock.

**Security: GO for the stated threat model.** ADR-013 holds. The dashboard now
passes the operator token it was silently omitting, and a locked control plane
looks locked instead of looking healthy.

**AI engineering: GO on the apparatus, claim withheld.** Four ways the
measurement machinery was producing numbers about something other than what
they claimed were fixed today, and three of four skill-contamination channels
were closed. The fourth is not deterministically detectable, which was
established by building the detector, measuring it (0.35 against 0.33 across
the judged cases) and refusing to ship a threshold tuned on four samples.

**Backend: GO with a watch item.** Determinism is enforced rather than
intended. The watch item is drift between the CLI and the service: two clients
of one contract, and fixes have twice landed on only one side.

### Addendum, 2026-09-03: the measurement ran

The ablation this section said was outstanding has been run. Five tasks from the
`custom` arm (excluded from training by construction), three repeats, both arms,
5 agents, 30 runs, $0.00:

```
  treatment   solved 3/15   20.0%  CI[0.00, 0.40]   nodes 41.5%
  control     solved 2/15   13.3%  CI[0.00, 0.33]   nodes 39.6%
  paired delta +0.067       CI[-0.20, +0.33]

  VERDICT: no measured improvement
```

**The claim below stands unchanged, now on evidence rather than on absence of
evidence.** The distinction matters for a reader deciding what this system is:
"we have not measured it" and "we measured it and it did not move" are different
statements, and only the second is a result.

Two findings recorded against the mechanism rather than for it: treatment was
worse at pass@1 (20% against 40%), and containments doubled (8 against 4, mostly
network egress). Both are within noise at n=15. Both point the wrong way, and
the second is an open safety question about the worked-example mechanism added
the same day.

**Review discipline held.** Three candidates reached the gate; one was approved.
Reading the rejects found two defects in the exemplar path -- a computed answer
being served to any task of the same shape, and a literal guard that was inert
against JSON because `abstract` captures quoted spans with their quotes. Both
fixed, both pinned by tests. The gate found them before the reviewer approved
anything, which is the argument for having it.

### The improvement claim is deliberately not made

The learning loop turns -- skills promote on real evidence, get retrieved,
scored, and pruned when they fail. Its first verdict on its own skills was
negative (5 successes in 53 uses), the causes were traced to four pipeline
defects rather than to the model, and three are fixed.

What is NOT claimed is that the system improves. That needs an ablation over a
library built entirely after those fixes, at a size where a retrieval index can
rank at all -- and this repository's standing rule is that such a claim gets
measured, never argued. `BENCHMARKS.md` is still deliberately ungenerated for
the same reason.

### Scope boundaries, chosen rather than pending

- **500-agent runs:** out of scope by decision. Supply credentials and measure.
- **Paid traffic:** the cost ceiling has never been exercised against real
  spend, because `SWARMD_ALLOW_PAID` is false and every run to date cost $0.00.
  Closing it means deliberately spending money, and the operator's standing
  instruction is free tiers only. Out of scope by that instruction, not
  outstanding.
