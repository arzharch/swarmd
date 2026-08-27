"""Prometheus metrics: the operational view of a run.

The distinction that matters, and the one to defend under questioning:

    Prometheus is for OPERATING the system. The ledger is for CLAIMS about it.

They are allowed to disagree. A scrape can be missed, a counter resets when a
pod restarts, and a histogram bucket is an approximation by construction. That
is fine for alerting -- an alert wants to know "is cost climbing abnormally
right now", not "what exactly did this run cost". Any number that ends up in a
report, a benchmark, or an improvement claim comes from the append-only ledger
(ADR-007), which does not lose rows and does not reset.

Consequence: nothing here is the source of truth for anything. If a dashboard
and BENCHMARKS.md disagree, the ledger is right and the dashboard is stale.

CARDINALITY POLICY (the rule that keeps a metrics bill from exploding):
labels are bounded sets only -- provider, model, stage, outcome, pattern. Never
run_id, task_id, agent_id, or anything else that grows with usage. High-
cardinality identity belongs in traces and the ledger, both of which are built
to hold it. A single unbounded label on a busy counter is how a Prometheus
instance dies, and it is not recoverable without dropping the series.

The prometheus_client dependency is optional. Without it every call here is a
no-op, because an observability import failure must never be the reason a run
does not start.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

NAMESPACE = "swarmd"

# Latency buckets tuned for LLM calls, not web requests. A free-tier provider
# under load routinely takes seconds; the default prometheus_client buckets top
# out at 10s and would collapse everything interesting into +Inf.
LLM_LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0, 120.0)

# Cost buckets in USD. A single call at free tier is 0; paid overflow calls land
# in the 1e-4 range. The spread is deliberately logarithmic because the
# interesting question is order of magnitude, not precision.
COST_BUCKETS = (0.0, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 0.05, 0.1)


class _NullMetric:
    """Stand-in used when prometheus_client is absent. Every method is a no-op."""

    def labels(self, *_: Any, **__: Any) -> _NullMetric:
        return self

    def inc(self, *_: Any, **__: Any) -> None: ...
    def dec(self, *_: Any, **__: Any) -> None: ...
    def set(self, *_: Any, **__: Any) -> None: ...
    def observe(self, *_: Any, **__: Any) -> None: ...


def _build() -> tuple[Any, dict[str, Any]]:
    """Construct swarmd's own registry and metrics, or a full set of no-ops.

    Deliberately NOT the prometheus_client default registry. swarmd is a library
    as much as a service, and a host application that also uses prometheus_client
    would collide with us on the global registry -- a duplicate-timeseries error
    at import time, which is the worst place to discover an observability
    conflict. Owning a registry also makes the module reloadable, which is what
    lets the no-prometheus fallback path be tested rather than assumed.

    Process, platform and GC collectors are registered explicitly, because
    dropping the default registry also drops the freebies it carries.
    """
    try:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
    except ImportError:
        logger.info(
            "prometheus_client not installed; metrics disabled. "
            "Install the 'metrics' extra to enable."
        )
        return None, {}

    registry = CollectorRegistry(auto_describe=True)

    import prometheus_client as pc

    for collector in ("PlatformCollector", "ProcessCollector", "GCCollector"):
        try:
            getattr(pc, collector)(registry=registry)
        except Exception:  # noqa: BLE001 - platform-dependent, never fatal
            # ProcessCollector is a no-op off Linux. Losing a freebie collector
            # must not stop the metrics that actually matter from registering.
            logger.debug("optional collector %s unavailable", collector)

    def counter(name: str, doc: str, labels: list[str]) -> Any:
        return Counter(f"{NAMESPACE}_{name}", doc, labels, registry=registry)

    def gauge(name: str, doc: str, labels: list[str]) -> Any:
        return Gauge(f"{NAMESPACE}_{name}", doc, labels, registry=registry)

    def histogram(name: str, doc: str, labels: list[str], buckets: Any) -> Any:
        return Histogram(
            f"{NAMESPACE}_{name}", doc, labels, buckets=buckets, registry=registry
        )

    return registry, {
        # --- golden signal: traffic ---------------------------------------
        "llm_calls": counter(
            "llm_calls_total",
            "LLM calls attempted, by provider and outcome",
            ["provider", "model", "outcome"],
        ),
        "cache_hits": counter(
            "cache_hits_total", "Requests served from the semantic cache", ["stage"]
        ),
        # --- golden signal: errors ----------------------------------------
        "llm_errors": counter(
            "llm_errors_total",
            "LLM call failures, by provider and reason",
            ["provider", "reason"],
        ),
        "rate_limited": counter(
            "rate_limited_total",
            "Provider 429 responses. The primary saturation signal for a "
            "system whose bottleneck is someone else's quota.",
            ["provider"],
        ),
        # --- golden signal: latency ---------------------------------------
        "llm_latency": histogram(
            "llm_latency_seconds",
            "End-to-end LLM call latency",
            ["provider"],
            LLM_LATENCY_BUCKETS,
        ),
        # --- golden signal: saturation ------------------------------------
        "agents_alive": gauge("agents_alive", "Agents currently alive", ["state"]),
        "queue_depth": gauge("queue_depth", "Scheduler queue depth", ["stage"]),
        "provider_available": gauge(
            "provider_available",
            "1 when a provider is accepting requests, 0 while backed off",
            ["provider"],
        ),
        "quota_remaining": gauge(
            "quota_remaining_requests",
            "Requests believed remaining in a provider's window",
            ["provider", "window"],
        ),
        # --- cost, treated as a first-class signal -------------------------
        "cost_usd": counter(
            "cost_usd_total", "Cumulative spend, by provider", ["provider"]
        ),
        "cache_savings_usd": counter(
            "cache_savings_usd_total",
            "Spend avoided by cache hits, priced at what the call would have cost",
            ["stage"],
        ),
        "call_cost": histogram(
            "call_cost_usd", "Per-call cost distribution", ["provider"], COST_BUCKETS
        ),
        "ceiling_aborts": counter(
            "ceiling_aborts_total", "Runs aborted for breaching the cost ceiling", []
        ),
        # --- domain: correctness and safety --------------------------------
        "gate_outcomes": counter(
            "gate_outcomes_total",
            "Quality-gate decisions, by stage and outcome",
            ["stage", "outcome"],
        ),
        "containments": counter(
            "containments_total",
            "Agents contained by the red-team organ, by detected pattern",
            ["pattern"],
        ),
        "agent_kills": counter(
            "agent_kills_total",
            "Agent kills, by what did the killing (chaos vs red-team vs lease)",
            ["source"],
        ),
        "requeues": counter(
            "requeues_total", "Tasks requeued with their checkpoint intact", ["stage"]
        ),
        # --- domain: human-in-the-loop -------------------------------------
        "approvals_pending": gauge(
            "approvals_pending", "Items waiting on a human decision", []
        ),
        "approval_wait": histogram(
            "approval_wait_seconds",
            "Time from queueing to human decision",
            ["decision"],
            (1, 10, 60, 300, 1800, 7200, 86400),
        ),
    }


REGISTRY, _METRICS = _build()
_NULL = _NullMetric()


def metric(name: str) -> Any:
    """Fetch a metric, or a no-op if metrics are unavailable."""
    return _METRICS.get(name, _NULL)


def enabled() -> bool:
    return bool(_METRICS)


# --- recording helpers -----------------------------------------------------
#
# Call sites use these rather than touching metrics directly, so the label
# cardinality policy is enforced in one place instead of at every call site.


def record_llm_call(
    *, provider: str, model: str, latency_s: float, cost_usd: float
) -> None:
    metric("llm_calls").labels(provider=provider, model=model, outcome="ok").inc()
    metric("llm_latency").labels(provider=provider).observe(latency_s)
    metric("call_cost").labels(provider=provider).observe(cost_usd)
    if cost_usd:
        metric("cost_usd").labels(provider=provider).inc(cost_usd)


def record_llm_error(*, provider: str, model: str, reason: str) -> None:
    metric("llm_calls").labels(provider=provider, model=model, outcome="error").inc()
    metric("llm_errors").labels(provider=provider, reason=reason).inc()


def record_rate_limited(*, provider: str, model: str = "-") -> None:
    """A 429 is saturation, not an error, and is counted separately.

    Conflating them makes an error-rate alert fire during normal throttling,
    which trains people to ignore it.
    """
    metric("llm_calls").labels(
        provider=provider, model=model, outcome="rate_limited"
    ).inc()
    metric("rate_limited").labels(provider=provider).inc()


def record_cache_hit(*, stage: str, saved_usd: float) -> None:
    metric("cache_hits").labels(stage=stage).inc()
    if saved_usd:
        metric("cache_savings_usd").labels(stage=stage).inc(saved_usd)


def set_provider_available(*, provider: str, available: bool) -> None:
    metric("provider_available").labels(provider=provider).set(1 if available else 0)


def set_agents_alive(*, state: str, count: int) -> None:
    metric("agents_alive").labels(state=state).set(count)


def set_queue_depth(*, stage: str, depth: int) -> None:
    metric("queue_depth").labels(stage=stage).set(depth)


def record_gate(*, stage: str, outcome: str) -> None:
    metric("gate_outcomes").labels(stage=stage, outcome=outcome).inc()


def record_containment(*, pattern: str) -> None:
    metric("containments").labels(pattern=pattern).inc()
    metric("agent_kills").labels(source="redteam").inc()


def record_kill(*, source: str) -> None:
    metric("agent_kills").labels(source=source).inc()


def record_requeue(*, stage: str) -> None:
    metric("requeues").labels(stage=stage).inc()


def record_ceiling_abort() -> None:
    metric("ceiling_aborts").inc()


def set_approvals_pending(count: int) -> None:
    metric("approvals_pending").set(count)


def record_approval_decision(*, decision: str, waited_s: float) -> None:
    metric("approval_wait").labels(decision=decision).observe(waited_s)


def sync_pool_status(status: list[dict[str, Any]]) -> None:
    """Mirror ProviderPool.status() into gauges.

    Pull-shaped rather than push-shaped: the pool owns its state and the
    exporter samples it, so metrics can never be the reason the pool holds a
    reference to an observability module.
    """
    for row in status:
        set_provider_available(
            provider=str(row["provider"]), available=bool(row["available"])
        )


# --- exposition ------------------------------------------------------------


def start_exporter(port: int = 9464, addr: str = "0.0.0.0") -> bool:
    """Start the Prometheus scrape endpoint.

    Binds all interfaces because the intended deployment is a container whose
    port is exposed to an in-cluster scraper; a localhost bind would be
    unreachable from the Prometheus pod. Network exposure is controlled by the
    NetworkPolicy in deploy/, not by the bind address.

    Returns False rather than raising when unavailable -- a run must not fail
    because its metrics endpoint could not start.
    """
    if not enabled():
        return False
    try:
        from prometheus_client import start_http_server

        start_http_server(port, addr, registry=REGISTRY)
        logger.info("metrics exporter listening on %s:%d", addr, port)
        return True
    except OSError as exc:
        logger.warning("metrics exporter failed to bind %s:%d: %s", addr, port, exc)
        return False


def render() -> bytes:
    """Render the current metrics as Prometheus text format.

    Used by the in-process HTTP surface (the control plane serves /metrics on
    its own port rather than opening a second listener in the same pod).
    """
    if not enabled():
        return b""
    from prometheus_client import generate_latest

    return generate_latest(REGISTRY)


def sample(name: str, **labels: str) -> float | None:
    """Read one sample back. Used by tests and the /debug surface."""
    if REGISTRY is None:
        return None
    value = REGISTRY.get_sample_value(name, labels or None)
    return None if value is None else float(value)
