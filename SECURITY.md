# Security

**Status:** v1.0 · **Updated:** 2026-08-28

swarmd executes model-written code and holds credentials for several LLM
providers. Both of those deserve a written threat model rather than an
assumption, and this document is deliberately specific about what is *not*
defended as well as what is.

---

## 1. Posture

Single-tenant, operator-run. One operator turns the service on, submits work,
and turns it off. No user accounts, no roles, no per-user data — see
[ADR-013](docs/adr/ADR-013.md) for why that is a decision and what compensates
for it.

**The service is never given a public route.** The dashboard sits behind an
Ingress IP allowlist; `/api` is reachable only from the frontend pods and the
ingress controller; a default-deny NetworkPolicy covers everything else.

---

## 2. Assets, in order of what it would cost to lose them

| Asset | Exposure if lost | Control |
|---|---|---|
| Provider API keys | Someone else spends your quota; free tiers get revoked | Secrets Manager + IRSA; never in git, never in Terraform state, redacted from logs, stripped from the sandbox environment |
| Database credentials | Read/modify checkpoints, ledger, approvals | Secrets Manager; security group admits only cluster nodes; no egress rule on the database |
| The audit trail | Human decisions become unattributable; evidence value gone | Append-only tables, `INSERT` only, immutable once decided |
| The cost ledger | Every reported number becomes unverifiable | Append-only JSONL, fsynced per row, `verify()` compares memory against disk |
| The skill library | Poisoned skills are inherited by every future run | Human approval gate; red-team library-poisoning detector; control-arm check on consolidation |
| Operator token | Full API access | Secrets Manager; constant-time comparison; rotation documented in the runbook |

---

## 3. Executing model-written code

This is the sharpest edge in the system, and the threat is not hypothetical.
Agents are selected on passing a check, and deleting the check is cheaper than
satisfying it — selection pressure searches for exactly that.

**Controls, in the order they matter:**

1. **Separate process**, not a thread and not `exec`. A thread shares the
   interpreter, so `sys.exit`, a segfault, or a monkeypatched builtin takes the
   run with it.
2. **Process-tree timeout.** Killing only the parent leaves grandchildren
   holding CPU, and the symptom surfaces later as unexplained slowness
   elsewhere.
3. **Environment allowlist.** Seven variables survive. Inheriting the parent
   environment would hand generated code every provider key in the process — a
   test asserts they are not visible.
4. **Confined working directory**, per execution, deleted after. Supplied file
   paths are resolved and rejected if they escape.
5. **Resource limits** (CPU seconds, address space, file size, process count)
   via `setrlimit` on POSIX.
6. **Output truncation**, so a program printing forever exhausts nothing in the
   parent.
7. **Artifacts through a file**, never parsed out of stdout — otherwise any
   program that prints a number could claim success.

**What this is not.** It is defence in depth, not a security boundary against a
determined attacker with code execution. The real boundary is the container:
restricted Pod Security Standard, non-root, read-only root filesystem, dropped
capabilities, seccomp, and a NetworkPolicy that blocks the cloud metadata
endpoint specifically — `169.254.169.254` is the standard path from "code runs
in a pod" to "attacker holds cloud credentials".

On Windows `setrlimit` does not exist, so limits degrade to timeout plus
environment stripping. `SandboxResult.limits_enforced` reports that honestly
rather than implying protection that is absent.

---

## 4. Prompt injection

Task text and provider responses are untrusted input. They reach a model, and a
model's output reaches a sandbox.

**What is structurally defended:**

- **The criterion cannot be moved by injected text.** It is frozen and
  content-addressed before any solving begins, and it is a declarative check
  list rather than model-written code, so there is no path from "text that
  reached a model" to "code that decides correctness" (ADR-009).
- **Payment cannot be self-awarded.** An agent claiming success does not get
  paid; the frozen criterion decides.
- **Escape attempts are contained, not merely failed.** The red-team matches
  metadata-endpoint access, credential reads, path traversal, network egress
  and fork bombs against submitted code, and containment is recorded rather
  than silently retried.

**What is not defended:** an injection that produces plausible, criterion-passing
output which is nonetheless wrong. That is the residual risk of the whole
approach, and the mitigations are the adversarial pass before freezing, the
seeded weak-criterion fixtures in CI, and the externally-authored public eval
arm — a systematic criterion weakness shows up as a gap between the arms rather
than hiding inside our own numbers.

---

## 5. Data handling and retention

| Data | Contains | Where | Retention |
|---|---|---|---|
| Cost ledger | Prompts truncated to previews, token counts, costs | JSONL on disk; S3 archive | 90 days hot, then Glacier; kept indefinitely as evidence |
| Traces | Prompt previews, chain-of-thought, response previews | Jaeger | 7 days |
| Metrics | Aggregate counts only, no per-task identifiers | Prometheus | 7 days local, 15 days in cluster |
| Approvals + audit | Items awaiting review, decisions, actors | Postgres | Life of the deployment; append-only |
| Skill library | Distilled instructions from successful runs | JSON file / Postgres | Until retired; retirement keeps the record |
| Sandbox workdirs | Generated code and its outputs | `emptyDir`, per execution | Deleted at execution end; gone on pod exit |
| Logs | Redacted request lines | stdout → collector | 14 days |

**No personal data is processed.** Tasks are technical prompts. If that ever
changes, this table is where it has to be reconsidered first, and free-tier
providers become immediately inappropriate — see below.

**Free tiers train on submitted prompts.** Groq, Google AI Studio, Cerebras and
OpenRouter free tiers all reserve the right; Mistral's Experiment tier requires
explicitly consenting, which is why it sits behind `--allow-data-training` and
is off by default. **Do not send anything confidential through a free tier.**
That is stated in `.env.example`, in the PRD, and here, because it is the kind
of thing that is easy to forget once the system is convenient.

---

## 6. Supply chain

- Dependencies resolved from a committed lockfile; the image contains exactly
  what CI tested.
- Multi-stage build: no package manager, no build tools, no source in the
  runtime image.
- Trivy scans both images in CI and fails on HIGH or CRITICAL. MEDIUM findings
  in a base image are constant background noise, and failing on them trains
  people to add ignore entries — which is how a CRITICAL gets ignored too.
- SBOM generated per build.
- ECR tags are immutable; production deploys by digest.

---

## 7. Known gaps

Listed because a security document with no gaps has not been written honestly.

1. **Egress allows 443 to anywhere** except RFC1918 and the metadata endpoint.
   It should be narrowed to provider CIDRs. This is the widest control shipped
   and the first to tighten.
2. **The sandbox is not a security boundary on its own.** Covered above; the
   container is. A gVisor or Kata runtime class would close this properly.
3. **One credential, no revocation granularity.** Rotating the operator token
   logs out everything. Acceptable at one principal.
4. **The audit trail records a supplied actor string**, not a verified identity.
   It proves what was decided and when, not who.
5. **No live-provider validation at scale**, so the rate-limit and cost controls
   are proven against a simulated provider rather than a real one.

---

## 8. Reporting

This is a personal project with a single maintainer. Open a GitHub issue for
anything non-sensitive. For something that should not be public, email the
maintainer directly and expect a slower response than a funded project would
give — said plainly rather than implying a response SLA that does not exist.
