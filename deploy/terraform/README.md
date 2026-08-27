# Terraform — swarmd on AWS

```bash
terraform init -backend-config="bucket=YOUR_STATE_BUCKET"
terraform plan  -var environment=dev
terraform apply -var environment=dev

# Provider keys are set OUT OF BAND. Terraform never holds them, because
# state is a file people copy around and a key in state is a key in every
# copy of it.
aws secretsmanager put-secret-value \
  --secret-id swarmd/dev/providers \
  --secret-string '{"groq_api_key":"...","google_api_key":"..."}'

$(terraform output -raw configure_kubectl)
kubectl apply -k ../k8s/overlays/dev
```

## What this creates, and what it costs

| | | monthly |
|---|---|---|
| VPC, 2 AZs, single NAT | provider APIs are reached through NAT | ~$35 |
| EKS control plane | | $73 |
| Node group: system (on-demand) | control plane + Redis; neither belongs on Spot | ~$25 |
| Node group: runs (Spot) | runs are checkpointed, so interruption is just chaos | ~$20 |
| RDS Postgres | Multi-AZ in prod only | ~$120 |
| S3 + ECR | ledger archive, images | ~$5 |
| **Total** | | **~$280** |

LLM spend is ~$0: the workload rides free tiers and the per-run ceiling is
$0.05. Infrastructure therefore costs about 5,600× more than inference. That is
the honest number, and it is the one that would justify moving to Fargate if
the goal were cost rather than demonstrating operations.

## Choices worth defending

**Single NAT gateway.** Per-AZ NAT roughly doubles network cost for
availability this workload already recovers from — checkpoints live in RDS,
which is not behind NAT, so losing NAT costs a run rather than data.

**Two AZs, not three.** Multi-AZ RDS needs two. A third adds cross-AZ transfer
and another NAT for redundancy that a checkpointed, resumable workload does not
need.

**Spot for run pods.** The product claim is that agents can be killed without
losing work. Refusing Spot would be an odd lack of confidence in it.

**Immutable ECR tags.** A tag that can be repointed is not a pin, and the prod
overlay deploys by digest for the same reason.

**No `aws_secretsmanager_secret_version` for provider keys.** Terraform state
would hold them in plaintext.

## What is deliberately absent

Multi-region, service mesh, ElastiCache, GitOps. Reasons in
[../../docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md) section 8 — each was
considered and declined, rather than forgotten.
