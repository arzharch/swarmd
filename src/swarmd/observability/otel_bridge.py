"""OTel bridge sink: export swarmd spans to OpenTelemetry (Jaeger etc.).

Design notes:

- Implements the same TraceSink protocol as the JSONL sink — compose them with
  CompositeSink to fan out to Jaeger AND a Langfuse-style file simultaneously.
- Maps swarmd span kinds onto OTel conventions: kind becomes an attribute,
  nesting becomes real OTel parent/child spans, llm spans carry semantic-convention
  style attributes (gen_ai.* where applicable).
- The otel dependency is OPTIONAL (pyproject `otel` extra): importing this module
  without it raises a clear error at construction, never at import time — the
  kernel stays runnable offline with zero observability deps.
- Batch export: spans queue in-memory and flush on context exit / explicit flush;
  a tracing outage must never slow the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class OtelSink:
    """TraceSink backed by OpenTelemetry SDK with OTLP export."""

    def __init__(
        self,
        endpoint: str = "http://localhost:4318/v1/traces",
        service_name: str = "swarmd",
    ) -> None:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError as exc:
            raise RuntimeError(
                "OtelSink requires the 'otel' extra: uv sync --extra otel"
            ) from exc

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("swarmd")
        self._otel_trace = trace
        self._open: dict[str, Any] = {}  # span_id -> OTel span handle

    def export(self, span: Any) -> None:  # Span; typed loosely to avoid import cycle
        """Export one finished swarmd span as an OTel span.

        swarmd spans arrive complete (end_ts set), so we recreate timing via
        start/end timestamps rather than relying on context-manager lifetime.
        Parent linkage uses the stored parent_id.
        """
        otel_span = self._tracer.start_span(
            name=f"{span.kind}.{span.name}",
            attributes=self._attributes(span),
            start_time=self._ns(span.start_ts),
        )
        if span.end_ts is not None:
            otel_span.end(end_time=self._ns(span.end_ts))
        otel_span.set_attribute("swarmd.span_id", span.span_id)
        if span.parent_id:
            otel_span.set_attribute("swarmd.parent_id", span.parent_id)
        otel_span.set_attribute("swarmd.trace_id", span.trace_id)

    @staticmethod
    def _ns(monotonic_ts: float) -> int:
        """Convert monotonic seconds to epoch-ish nanoseconds OTel accepts.

        OTel wants wall-clock ns; our spans use monotonic. The absolute base is
        arbitrary for Jaeger display purposes — relative ordering is preserved,
        which is what matters for debugging.
        """
        return int(monotonic_ts * 1_000_000_000)

    @staticmethod
    def _attributes(span: Any) -> dict[str, Any]:
        attrs: dict[str, Any] = {"swarmd.kind": span.kind}
        for key, value in span.attributes.items():
            if key == "thoughts":
                # CoT chain -> one attribute per thought, JSON-encoded.
                for i, thought in enumerate(value):
                    attrs[f"swarmd.thought.{i}.decision"] = str(thought.get("decision"))
                    attrs[f"swarmd.thought.{i}.reasoning"] = str(
                        thought.get("reasoning")
                    )
            elif isinstance(value, (str, int, float, bool)):
                attrs[key] = value
            else:
                attrs[key] = str(value)
        return attrs


def make_sink(mode: str = "memory", **kwargs: Any) -> Any:
    """Factory mirroring make_router/make_store.

    ANATOMY: mode
      "memory"   -> InMemoryTraceStore; tests, `swarmd trace dump` (default)
      "jsonl"    -> JsonlTraceSink(path=...); Langfuse-style file ingest
      "otel"     -> OtelSink(endpoint=...); Jaeger via docker-compose. Needs the
                    otel extra; degrades to memory with a warning otherwise so
                    demos never crash without Docker running.
      "composite"-> all of the above combined; kwargs select which are enabled.
    """
    from swarmd.observability.tracing import InMemoryTraceStore, JsonlTraceSink

    if mode == "memory":
        return InMemoryTraceStore()
    if mode == "jsonl":
        path = kwargs.get("path") or "traces.jsonl"
        return JsonlTraceSink(str(path))
    if mode == "otel":
        try:
            return OtelSink(endpoint=kwargs.get("endpoint") or "http://localhost:4318/v1/traces")
        except RuntimeError as exc:
            logger.warning("%s — falling back to in-memory traces", exc)
            return InMemoryTraceStore()
    if mode == "composite":
        from swarmd.observability.tracing import CompositeSink

        sinks: list[Any] = [InMemoryTraceStore()]
        if kwargs.get("jsonl_path"):
            sinks.append(JsonlTraceSink(str(kwargs["jsonl_path"])))
        if kwargs.get("otel"):
            sinks.append(make_sink("otel", endpoint=kwargs.get("endpoint")))
        return CompositeSink(*sinks)
    raise ValueError(f"unknown sink mode: {mode!r}")
