# Service Level Objectives

**Status:** v1.0 · **Updated:** 2026-08-28 · Owner: Arsh Zakee Chowhan
**Review cadence:** monthly, and after any incident that consumed >25% of an error budget

## Why these and not others

An SLO is a promise you are willing to be woken up for. Most systems pick
availability and latency because that is what web services promise. `swarmd`
promises something different, so two of the four SLIs below are unusual:

- **Correctness under chaos** is an SLI here because the entire product claim
  is that agents can die without work being lost. If that degrades, nothing
  else about the system matters.
- **Cost per run** is an SLI because the system exists to operate inside a hard
  budget. A run that succeeded at 10x the cost did not succeed.

Latency is deliberately *not* a top-level SLO. The dominant term is provider
response time, which we do not control, and promising a number we cannot
influence produces an SLO that gets waived every time it is breached — which
teaches everyone that SLOs get waived.

---

## SLI/SLO definitions

All SLIs are computed from the append-only ledger (ADR-007), not from
Prometheus. Prometheus is for alerting; the ledger is for accounting. A scrape
gap must not be able to improve a reported number.

### SLO-1 · Run completion

| | |
|---|---|
| **SLI** | Proportion of submitted runs that reach a terminal state (`completed` or `aborted_with_report`) rather than hanging or crashing |
| **SLO** | **99%** over a rolling 28 days |
| **Error budget** | 1% ≈ 1 failed run per 100 |

A run that aborts cleanly on the cost ceiling **counts as a success**. It
reached a terminal state and produced a report saying why. Counting it as a
failure would create pressure to raise the ceiling, which inverts the control
into a formality.

### SLO-2 · Correctness under chaos

| | |
|---|---|
| **SLI** | Proportion of chaos runs whose integrity hash matches the equivalent zero-chaos run |
| **SLO** | **100%** — no error budget |
| **Measured by** | `swarm run --chaos` in CI, every merge to master |

The only SLO here at 100%, and the only one without a budget. A budget implies
an acceptable rate of silently losing work, and there isn't one: the guarantee
is either true or the system does not do what it says. A single failure is a
release blocker, not a budget draw.

### SLO-3 · Cost per run

| | |
|---|---|
| **SLI** | Proportion of runs completing at or under the $0.05 ceiling |
| **SLO** | **99.9%** over a rolling 28 days |
| **Error budget** | 0.1% |

Near-100% because the ceiling is enforced in code at the harness boundary. A
breach means the enforcement itself failed — an unpriced model slipping through
(`UnpricedModel` should have raised), or a bypassed call path. Both are bugs,
not capacity events.

### SLO-4 · Dashboard freshness

| | |
|---|---|
| **SLI** | Proportion of run events visible in the dashboard within 2 seconds of occurring |
| **SLO** | **95%** over a rolling 7 days |
| **Error budget** | 5% |

Loosest budget here, deliberately. The dashboard is an observer: it must never
be able to slow a run down. The WebSocket sink drops for slow consumers rather
than applying backpressure, so a saturated browser degrades this SLI and
nothing else. That trade is correct and this SLO is written to permit it.

---

## Error budget policy

What actually happens when a budget is spent, agreed in advance so it is not
negotiated during an incident:

| Budget consumed | Response |
|---|---|
| < 50% | Normal. Ship features. |
| 50–75% | Reliability work is prioritised alongside features in the next planning cycle. |
| 75–100% | Feature work on the affected component **stops**. Only reliability fixes, tests, and rollbacks merge. |
| Exhausted | Freeze on that component. Written postmortem required before the freeze lifts, regardless of whether any single incident was large enough to warrant one. |

The point of writing the thresholds down is that the decision is made when
nobody is under pressure. A policy invented mid-incident always says "ship it".

---

## What is explicitly NOT an SLO

Stated so their absence reads as a decision rather than an oversight.

**Provider availability.** Not ours. The pool routes around it and the capacity
plan sizes for it. Alerting on it would page us about Groq's uptime.

**Agent success rate on tasks.** This is a *capability* metric, not a
reliability one, and it belongs in `swarmd eval` with a control arm. Making it
an SLO would create pressure to weaken the frozen criterion — the system would
hit its SLO by grading itself more generously, which is precisely the failure
ADR-009 exists to prevent. **This is the most important line in this document.**

**LLM latency.** Dominated by provider behaviour. Tracked, alerted on when it
moves sharply, never promised.

**Time to first result.** A run that is throttled is behaving correctly; a
deadline here would push toward paid overflow as a reflex.

---

## Monitoring the SLIs

| SLO | Source | Alert |
|---|---|---|
| SLO-1 | Ledger: terminal-state rows per submitted run | Burn-rate alert at 14.4x (2% budget in 1h) → page |
| SLO-2 | CI chaos gate | Any failure → release blocker, no alerting needed |
| SLO-3 | Ledger: `SUM(cost_usd)` per `run_id` | `SwarmdCeilingAbort` → ticket |
| SLO-4 | WebSocket sink drop counter vs emitted events | Burn-rate at 6x over 6h → ticket |

Burn-rate alerting rather than threshold alerting: a threshold on a 28-day
window either fires far too late to act or fires on noise. Multi-window
burn-rate is what makes "you will exhaust this budget by Thursday" actionable
on Monday.
