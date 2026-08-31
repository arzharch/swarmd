# Production Readiness Review

**Status:** v1.0 · **Updated:** 2026-08-28 · Reviewed before any production deploy

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
| Architecture decisions recorded, including reversals | PASS | 13 ADRs; ADR-001 superseded, ADR-004 and ADR-006 amended |
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
