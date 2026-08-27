"""Metrics tests.

Two properties are worth testing here and the rest is plumbing:

1. The cardinality policy holds -- no metric accepts an unbounded label. A
   single run_id label on a busy counter kills a Prometheus instance, and the
   damage is not recoverable without dropping the series.
2. Absence of prometheus_client degrades to no-ops rather than exploding. An
   observability import must never be why a run refuses to start.
"""

from __future__ import annotations

import importlib

import pytest

from swarmd.observability import metrics as m

FORBIDDEN_LABELS = {"run_id", "task_id", "agent_id", "lead_id", "trace_id", "id"}


def _value(name: str, **labels) -> float:
    """Read one sample, treating an absent series as zero."""
    return m.sample(name, **labels) or 0.0


# --- the policy that actually matters --------------------------------------


def test_no_metric_accepts_an_unbounded_label():
    """Cardinality policy, enforced rather than documented."""
    assert m.enabled(), "prometheus_client should be installed in dev"
    offenders = []
    for key, metric in m._METRICS.items():
        names = set(getattr(metric, "_labelnames", ()) or ())
        bad = names & FORBIDDEN_LABELS
        if bad:
            offenders.append((key, sorted(bad)))
    assert offenders == [], f"unbounded labels found: {offenders}"


def test_every_metric_is_namespaced():
    for key, metric in m._METRICS.items():
        assert getattr(metric, "_name", "").startswith(
            m.NAMESPACE
        ), f"{key} is not namespaced"


def test_latency_buckets_cover_slow_free_tier_calls():
    """Default client buckets top out at 10s; free-tier calls routinely exceed it."""
    assert max(m.LLM_LATENCY_BUCKETS) >= 60.0
    assert m.LLM_LATENCY_BUCKETS == tuple(sorted(m.LLM_LATENCY_BUCKETS))


def test_cost_buckets_are_ordered_and_start_at_zero():
    """Free-tier calls cost exactly zero and must land in a real bucket."""
    assert m.COST_BUCKETS[0] == 0.0
    assert m.COST_BUCKETS == tuple(sorted(m.COST_BUCKETS))


# --- degradation -----------------------------------------------------------


def test_metrics_degrade_to_no_ops_without_prometheus(monkeypatch):
    """A missing observability dependency must not stop a run."""
    import builtins

    real_import = builtins.__import__

    def fail_prometheus(name, *args, **kwargs):
        if name.startswith("prometheus_client"):
            raise ImportError("simulated: prometheus_client absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_prometheus)
    reloaded = importlib.reload(m)
    try:
        assert reloaded.enabled() is False
        # Every helper must still be callable and silent.
        reloaded.record_llm_call(
            provider="groq", model="m", latency_s=1.0, cost_usd=0.0
        )
        reloaded.record_rate_limited(provider="groq")
        reloaded.set_queue_depth(stage="solve", depth=3)
        reloaded.record_gate(stage="solve", outcome="pass")
        reloaded.record_containment(pattern="loop")
        reloaded.set_approvals_pending(2)
        assert reloaded.render() == b""
        assert reloaded.start_exporter(port=0) is False
    finally:
        monkeypatch.undo()
        importlib.reload(m)


# --- recording -------------------------------------------------------------


def test_llm_call_records_traffic_latency_and_cost():
    labels = {"provider": "groq", "model": "llama", "outcome": "ok"}
    before = _value("swarmd_llm_calls_total", **labels)
    m.record_llm_call(provider="groq", model="llama", latency_s=1.5, cost_usd=0.0001)
    after = _value("swarmd_llm_calls_total", **labels)
    assert after == pytest.approx(before + 1)
    assert _value("swarmd_cost_usd_total", provider="groq") >= 0.0001


def test_free_calls_do_not_increment_cost():
    before = _value("swarmd_cost_usd_total", provider="cerebras")
    m.record_llm_call(provider="cerebras", model="x", latency_s=0.5, cost_usd=0.0)
    assert _value("swarmd_cost_usd_total", provider="cerebras") == pytest.approx(before)


def test_rate_limits_are_counted_separately_from_errors():
    """Conflating them makes an error alert fire during normal throttling."""
    err_before = _value("swarmd_llm_errors_total", provider="groq", reason="timeout")
    lim_before = _value("swarmd_rate_limited_total", provider="groq")

    m.record_rate_limited(provider="groq", model="llama")

    assert _value("swarmd_rate_limited_total", provider="groq") == pytest.approx(
        lim_before + 1
    )
    assert _value(
        "swarmd_llm_errors_total", provider="groq", reason="timeout"
    ) == pytest.approx(err_before)


def test_error_records_both_the_call_outcome_and_the_reason():
    m.record_llm_error(provider="cerebras", model="x", reason="timeout")
    assert (
        _value("swarmd_llm_errors_total", provider="cerebras", reason="timeout") >= 1
    )
    assert (
        _value(
            "swarmd_llm_calls_total",
            provider="cerebras",
            model="x",
            outcome="error",
        )
        >= 1
    )


def test_cache_hit_records_savings_priced_at_what_it_would_have_cost():
    m.record_cache_hit(stage="solve", saved_usd=0.002)
    assert _value("swarmd_cache_hits_total", stage="solve") >= 1
    assert _value("swarmd_cache_savings_usd_total", stage="solve") >= 0.002


def test_containment_also_counts_as_a_kill():
    """Red-team containment uses the chaos kill path, so it is a kill too."""
    kills_before = _value("swarmd_agent_kills_total", source="redteam")
    m.record_containment(pattern="budget_siphon")
    assert _value("swarmd_containments_total", pattern="budget_siphon") >= 1
    assert _value("swarmd_agent_kills_total", source="redteam") == pytest.approx(
        kills_before + 1
    )


def test_gauges_are_set_not_incremented():
    m.set_queue_depth(stage="plan", depth=10)
    m.set_queue_depth(stage="plan", depth=4)
    assert _value("swarmd_queue_depth", stage="plan") == 4


def test_pool_status_syncs_availability_gauges():
    m.sync_pool_status(
        [
            {"provider": "groq", "available": True},
            {"provider": "cerebras", "available": False},
        ]
    )
    assert _value("swarmd_provider_available", provider="groq") == 1
    assert _value("swarmd_provider_available", provider="cerebras") == 0


def test_render_emits_prometheus_text_format():
    m.record_llm_call(provider="groq", model="m", latency_s=0.1, cost_usd=0.0)
    body = m.render()
    assert b"swarmd_llm_calls_total" in body
    assert b"# TYPE" in body
