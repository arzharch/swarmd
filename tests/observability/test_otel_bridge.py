"""Tests for the OTel bridge: graceful degradation, attribute mapping, factory."""

import pytest

from swarmd.observability.otel_bridge import OtelSink, make_sink
from swarmd.observability.tracing import (
    InMemoryTraceStore,
    JsonlTraceSink,
    record_thought,
    tracer,
)


def test_factory_modes() -> None:
    assert isinstance(make_sink("memory"), InMemoryTraceStore)
    s = make_sink("jsonl", path="x.jsonl")
    assert isinstance(s, JsonlTraceSink)
    with pytest.raises(ValueError, match="unknown sink mode"):
        make_sink("zipkin")


def test_otel_missing_dependency_degrades_gracefully() -> None:
    """Without the otel extra installed, make_sink('otel') falls back to memory."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401

        has_otel = True
    except ImportError:
        has_otel = False

    sink = make_sink("otel")
    if not has_otel:
        assert isinstance(sink, InMemoryTraceStore)
    else:
        assert isinstance(sink, OtelSink)


def test_composite_mode_includes_memory_always() -> None:
    comp = make_sink("composite")
    # CompositeSink wraps at least the in-memory store.
    assert hasattr(comp, "sinks") and len(comp.sinks) >= 1


def test_span_export_through_full_tracer_flow() -> None:
    """End-to-end: tracer -> span -> OtelSink.export must never raise, whether
    or not the OTel SDK is present (export errors are swallowed by design)."""
    store = InMemoryTraceStore()
    with tracer("stage", "run", sink=store):
        record_thought("decided", reasoning="because")
        with tracer("llm", "call", sink=store):
            pass

    assert len(store.spans) == 2
    # If OTel SDK happens to be installed, exercise the real mapping path.
    try:
        import logging as _logging

        _logging.getLogger("opentelemetry").setLevel(_logging.CRITICAL)
        sink = OtelSink(endpoint="http://127.0.0.1:1/v1/traces")  # closed port; fails fast
    except RuntimeError:
        return  # otel extra absent — degradation already covered above
    for span in store.spans:
        sink.export(span)  # batch processor swallows export failure


def test_attribute_mapping_covers_thoughts_and_scalars() -> None:
    store = InMemoryTraceStore()
    with tracer("llm", "call", sink=store, temperature=0.2) as t:
        t.set("tokens_in", 42)
        record_thought("picked", reasoning="fastest path")

    span = store.spans[0]
    attrs = OtelSink._attributes(span)
    assert attrs["swarmd.kind"] == "llm"
    assert attrs["temperature"] == 0.2
    assert attrs["tokens_in"] == 42
    assert attrs["swarmd.thought.0.decision"] == "picked"
    assert attrs["swarmd.thought.0.reasoning"] == "fastest path"
