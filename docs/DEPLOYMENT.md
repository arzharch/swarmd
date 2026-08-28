# Deployment Plan

**Status:** v1.0 · **Updated:** 2026-08-28 · Target: AWS (Azure mapping in §7)
**Prerequisite:** SPEC Phase 11 green. Deployment is explicitly deferred until then.

---

## 1. What is actually being deployed

Four things, with deliberately different shapes:

| Component | Kubernetes shape | Why |
|---|---|---|
| Control plane | Deployment, 3 replicas | Stateless HTTP + WebSocket. Replicas exist for availability during deploys, not for throughput — throughput is capped by provider quota, not by us. |
| Run | **Job**, one pod per run | A run has a beginning, an end, and a result. That is a batch workload. |
| Frontend | Deployment, 2 replicas | Static-ish Next.js. No credentials, no database. |
| Redis | Deployment, 1 replica, `Recreate` | Quota coordination only (ADR-011). Two replicas would split the buckets and each would permit the full rate. |

**A single run is not distributed across pods.** PRD §6 lists multi-node
execution as a v1 non-goal, and the honest claim is: horizontal scale means
*more concurrent runs*, not a faster single run. Anyone who asks "so does it
scale?" should get that sentence, not a diagram.

The thing that genuinely does not survive naive horizontal scaling is provider
quota, which is why ADR-011 exists and why Redis is in this diagram at all.

---

## 2. Target architecture (AWS)

```
                         Route 53
                            │
                     ACM cert ─ ALB  (ingress-nginx)
                            │
        ┌───────────────────┴───────────────────┐
        │              EKS cluster              │
        │                                       │
        │  ns: swarmd                           │
        │   ├── control-plane  Deployment ×3    │
        │   │      └── creates ──┐              │
        │   ├── run-*           Job ×N ◄────────┘
        │   ├── frontend        Deployment ×2   │
        │   └── redis           Deployment ×1   │
        │                                       │
        │  ns: monitoring                       │
        │   └── kube-prometheus-stack           │
        │        (Prometheus, Grafana,          │
        │         Alertmanager)                 │
        │                                       │
        │  Karpenter ── node provisioning       │
        └───────┬───────────────────┬───────────┘
                │                   │
         RDS Postgres        Secrets Manager
         (Multi-AZ)          (via External Secrets + IRSA)
                │
         S3 ── ledger archive, eval reports
                │
       ┌────────┴────────┐
       │  Provider APIs  │  Groq · Google AI Studio ·
       │   (egress 443)  │  Cerebras · OpenRouter
       └─────────────────┘
```

### Component choices and the alternatives rejected

**EKS over ECS/Fargate.** Fargate would be simpler and cheaper for this
workload — genuinely. EKS is chosen because Jobs, HPA on a custom metric, PDBs,
and the kube-prometheus-stack operator are the industry-standard vocabulary,
and because the run-as-Job model maps onto it exactly. The cost is real: EKS
control plane is ~$73/month before a single node.

**Karpenter over Cluster Autoscaler.** Run pods request 1 CPU / 2Gi and arrive
in bursts when an eval sweep starts. Karpenter provisions right-sized nodes in
under a minute rather than scaling fixed node groups, and consolidates them
when the sweep ends. With node groups, a nightly sweep either waits for scale-up
or keeps idle capacity all day.

**Spot for run pods, on-demand for the control plane.** Runs are checkpointed
and interruption-tolerant by construction — that is the entire product claim,
so refusing to run them on Spot would be an odd lack of confidence. A Spot
interruption is just another chaos event, and Karpenter's interruption handling
gives a 2-minute drain. Control plane and Redis stay on-demand: an interrupted
control plane is a blip; an interrupted Redis is a quota-coordination gap.

**RDS Postgres, Multi-AZ.** Holds checkpoints, ledger, approvals, skill library,
frozen criteria. Multi-AZ because an approval queue that loses a human decision
loses the audit trail, which is the one thing that must never be reconstructible
"approximately". `db.t4g.medium` is ample — this database is small and
write-light; it is durable, not hot.

**ElastiCache deliberately NOT used for quota.** Redis here holds ephemeral
5-minute TTL buckets. Paying for a managed, replicated, backed-up Redis to hold
data we are happy to lose would be spending money to solve a problem we do not
have. An in-cluster pod is right, and losing it costs one window of
over-permissiveness (ADR-011).

**S3 for ledger archive.** Ledgers are append-only JSONL and eval reports are
generated artifacts. Both are write-once, read-rarely, and want to be kept
indefinitely — that is S3 with a lifecycle policy to Glacier at 90 days.

**Secrets Manager over Parameter Store.** Rotation support, and the External
Secrets Operator integration is first-class. Access is via IRSA, so no static
AWS credentials exist in the cluster to be stolen.

---

## 3. Environments

| | dev | prod |
|---|---|---|
| Cluster | shared EKS, `swarmd-dev` namespace | dedicated EKS |
| Replicas | 1 each | 3 control plane, 2 frontend |
| Database | RDS single-AZ, `db.t4g.micro` | RDS Multi-AZ, `db.t4g.medium` |
| Paid overflow | **disabled** | enabled |
| Secrets | Secrets Manager, dev path | Secrets Manager, prod path |
| Chaos | on | on |

Chaos is on in production. Turning it off would mean production is the one
environment where the recovery guarantee is not continuously tested, which is
precisely backwards.

Paid overflow is off in dev because a runaway loop against a paid provider in
an environment nobody is watching is a bill discovered next month.

---

## 4. Progressive rollout

Deployment is not `kubectl apply` and hope.

1. **CI gate.** Lint, types, tests, the chaos integrity gate, kustomize build of
   both overlays, container scan (Trivy, fail on HIGH+), SBOM.
2. **Deploy to dev.** Automatic on merge to master.
3. **Smoke run in dev.** `swarm run --profile smoke` against a known task, with
   a known-good criterion hash. ~2 minutes. This is a real end-to-end assertion,
   not a health check: it proves the whole loop works against real providers.
4. **Promote the exact digest to prod.** Not a rebuild — the artifact that
   passed is the artifact that ships.
5. **Canary.** One control-plane pod on the new digest, 10 minutes, watching
   error rate and dashboard freshness.
6. **Roll forward** if the canary is clean; `rollout undo` if not.

**Cluster prerequisites, learned by applying these manifests to an empty one.**
`kubectl apply -k` exits non-zero on a cluster without the Prometheus Operator:
`ServiceMonitor`, `PodMonitor` and `PrometheusRule` are custom resources, and
their absence is reported as "ensure CRDs are installed first" after everything
else has already applied. Install `kube-prometheus-stack` before the first
apply, or expect a partial success with a failing exit code.

**A cluster with no provider credentials is healthy and NOT ready, by design.**
`/healthz` returns 200 and `/readyz` returns 503 listing zero providers, so the
pod is not restarted and takes no work. That is the intended signal, not a
fault: without credentials there is no capacity, and a pod that accepted runs
anyway would fail them one by one. Supply keys, or expect an unready
deployment.

**The control plane refuses to start bound off-host without an operator token**
(ADR-013). Prod reads one from Secrets Manager through an ExternalSecret; dev
patches in an obviously-fake local value. A deployment whose token is empty
CrashLoopBackOffs with the reason in its logs.

**Automatic rollback triggers**, agreed in advance:
- Any SLO-2 (chaos integrity) failure — no budget, no judgement call
- Error rate over 5% for 5 minutes
- Readiness failures on more than one third of pods

**Database migrations are expand/contract, always.** Add columns, deploy code
that writes both, backfill, deploy code that reads new, drop old — four
deploys, never one. A migration that requires code and schema to change
simultaneously makes rollback impossible, and the moment you most want to roll
back is the moment right after a migration.

---

## 5. Capacity and cost

Infrastructure, prod, monthly:

| Item | Est. |
|---|---|
| EKS control plane | $73 |
| Nodes (2× on-demand `t4g.medium` + Spot for runs) | ~$60 |
| RDS Multi-AZ `db.t4g.medium` | ~$120 |
| ALB | ~$20 |
| S3 + data transfer | ~$5 |
| **Total** | **~$280/month** |

**LLM spend is ~$0/month.** The workload rides free tiers by design and the
per-run ceiling is $0.05. The interesting number is that infrastructure costs
roughly 5,600× more than inference — which is exactly the observation that
makes Fargate worth revisiting if the goal ever shifts from demonstrating
Kubernetes competence to minimising cost.

Node sizing follows the capacity plan: provider quota caps useful concurrency
at ~45 requests/minute, so more than a handful of concurrent runs produces pods
that sit blocked on quota. The `ResourceQuota` caps concurrent Jobs at 20 for
this reason — a cluster limit protecting an external limit.

---

## 6. Failure domains

| Fails | Blast radius | Behaviour |
|---|---|---|
| One provider | none | Pool reroutes; run continues slower |
| All providers | new runs blocked | `NoCapacity`; in-flight runs abort cleanly with reports |
| Redis | quota degraded | Local buckets at 25% rate. Runs continue slowly. **Not an outage.** |
| RDS | new runs blocked | In-flight runs hold checkpoints in memory; recovery on restart |
| One AZ | reduced capacity | Multi-AZ RDS fails over; topology spread reschedules pods |
| Control plane | no new submissions | **In-flight runs are unaffected** — they are Jobs with their own pods |
| Frontend | no visibility | Runs unaffected. The dashboard is an observer. |

The last two rows are the payoff for making the control plane stateless and
runs into Jobs. "The API is down but the work is fine" is a property worth
designing for.

---

## 7. Azure mapping

Every AWS choice has a direct equivalent; the Kubernetes manifests are
unchanged, and only the overlay differs.

| AWS | Azure |
|---|---|
| EKS | AKS |
| Karpenter | AKS node autoprovisioning / cluster autoscaler |
| RDS Postgres Multi-AZ | Azure Database for PostgreSQL Flexible Server, zone-redundant HA |
| Secrets Manager + IRSA | Key Vault + Workload Identity |
| S3 | Blob Storage (cool → archive tier) |
| ECR | ACR |
| ALB | Application Gateway |
| CloudWatch | Azure Monitor |

The External Secrets Operator abstracts the secret backend, so switching is a
`SecretStore` provider block — which is the reason for using the operator
rather than a cloud-specific CSI driver in the first place.

---

## 8. What is deliberately not here

- **Multi-region.** A demo platform does not need it, and pretending otherwise
  would add cost and complexity for a requirement that does not exist.
- **Service mesh.** Four services with straightforward traffic patterns. A mesh
  would add a control plane, sidecar overhead, and a new failure mode to solve
  problems this system does not have.
- **GitOps (ArgoCD/Flux).** The right answer for a team; overhead for one
  person. The manifests are structured so adopting it later is a repo pointer,
  not a rewrite.
- **Distributed single-run execution.** A v1 non-goal (PRD §6). Adding it means
  a distributed scheduler, and claiming it without one would be dishonest.
