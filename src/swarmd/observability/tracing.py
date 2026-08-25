"""Tracing: provider-agnostic observability for agent reasoning and LLM calls.

Design notes:

- One TraceSink protocol; multiple backends. OTel (Jaeger) is the default, but
  the same events feed Langfuse-style LLM-observability backends — the caller
  composes sinks, so traces can go to BOTH without code changes.
- Spans carry a `kind` (chain/tool/llm/gate/approval) plus attributes. The
  llm kind records prompt/response/token counts — that's the CoT-level data
  Langfuse-class tools need: which prompt produced which completion at what cost.
- Chain-of-thought capture: agents emit `chain` spans recording their reasoning
  steps as structured attributes (step name, input summary, decision, output
  summary). Debugging = reading the span tree; no print-statement archaeology.
- Context propagation via contextvars: any code inside a traced section gets the
  current trace/span IDs automatically — no plumbing through every signature.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "swarmd_current_span", default=None
)
_current_trace: contextvars.ContextVar[str] = contextvars.ContextVar(
    "swarmd_current_trace", default=""
)
_seq_counter = iter(range(1 << 62))  # monotonic entry-order sequence


@dataclass(slots=True)
class Span:
    """A single traced operation. kind: chain|tool|llm|gate/approval|stage."""

    trace_id: str
    span_id: str
    parent_id: str | None
    kind: str
    name: str
    start_ts: float
    seq: int = 0  # entry order — exports happen at exit, so sort by this
    end_ts: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_ts is None:
            return -1.0
        return round((self.end_ts - self.start_ts) * 1000, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
        }


class TraceSink(Protocol):
    def export(self, span: Span) -> None: ...


class InMemoryTraceStore:
    """Collects spans for tests and local inspection (`swarmd trace dump`)."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def export(self, span: Span) -> None:
        self.spans.append(span)

    def by_trace(self, trace_id: str) -> list[Span]:
        return [s for s in self.spans if s.trace_id == trace_id]

    def render_tree(self, trace_id: str) -> str:
        """ASCII span tree — the debugging view of one run."""
        spans = sorted(self.by_trace(trace_id), key=lambda s: s.start_ts)
        if not spans:
            return "(no spans)"
        lines: list[str] = []
        by_parent: dict[str | None, list[Span]] = {}
        for s in spans:
            by_parent.setdefault(s.parent_id, []).append(s)

        def walk(parent: str | None, depth: int) -> None:
            for s in by_parent.get(parent, []):
                attrs = ""
                if s.kind == "llm":
                    attrs = (
                        f" tokens_in={s.attributes.get('tokens_in')}"
                        f" tokens_out={s.attributes.get('tokens_out')}"
                        f" model={s.attributes.get('model')}"
                    )
                elif s.kind == "chain":
                    attrs = f" decision={s.attributes.get('decision', '')!r}"
                lines.append(
                    f"{'  ' * depth}{s.kind}:{s.name} {s.duration_ms}ms{attrs}"
                )
                walk(s.span_id, depth + 1)

        walk(None, 0)
        return "\n".join(lines)


class JsonlTraceSink:
    """Append-only JSONL sink — the drop-in format Langfuse/others ingest."""

    def __init__(self, path: str) -> None:
        self.path = path

    def export(self, span: Span) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(span.to_dict(), default=str) + "\n")


class CompositeSink:
    """Fan-out to multiple backends (e.g. OTel + Langfuse-style JSONL)."""

    def __init__(self, *sinks: TraceSink) -> None:
        self.sinks = sinks

    def export(self, span: Span) -> None:
        for s in self.sinks:
            s.export(span)


def current_trace_id() -> str:
    return _current_trace.get()


logger = logging.getLogger(__name__)


def _emit(sink: TraceSink | None, span: Span) -> None:
    if sink is not None:
        try:
            sink.export(span)
        except Exception as exc:  # noqa: BLE001 - observability must never break the run
            logger.warning("trace sink export failed: %s", exc)


class tracer:
    """Context manager creating a span; nestable; async-safe.

    Usage:
        with tracer("llm", "score_lead", sink=sink, model="llama-3.3") as t:
            ... call ...
            t.set("tokens_in", 120)
    """

    def __init__(
        self,
        kind: str,
        name: str,
        sink: TraceSink | None = None,
        **attrs: Any,
    ) -> None:
        self._sink = sink
        self._kind = kind
        self._name = name
        self._attrs = dict(attrs)
        self._span: Span | None = None
        self._token: Any = None

    def __enter__(self) -> Self:
        parent = _current_span.get()
        trace_id = _current_trace.get() or uuid.uuid4().hex[:16]
        self._span = Span(
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:12],
            parent_id=parent.span_id if parent else None,
            kind=self._kind,
            name=self._name,
            start_ts=time.monotonic(),
            seq=next(_seq_counter),
            attributes=self._attrs,
        )
        self._token_trace = _current_trace.set(trace_id)
        self._token = _current_span.set(self._span)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        assert self._span is not None
        self._span.end_ts = time.monotonic()
        if exc_type is not None and exc is not None:
            self._span.attributes["error"] = f"{exc_type.__name__}: {exc}"
        _current_span.reset(self._token)
        _current_trace.reset(self._token_trace)
        _emit(self._sink, self._span)

    def set(self, key: str, value: Any) -> None:
        if self._span is not None:
            self._span.attributes[key] = value

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.__exit__(exc_type, exc, tb)


def record_thought(decision: str, *, reasoning: str = "", **data: Any) -> None:
    """Record a reasoning step on the current span (CoT capture).

    Call from anywhere inside a traced section: the thought lands as an attribute
    on the active span, keeping the decision trail attached to its operation.
    Each thought carries a global monotonic tick so thoughts from DIFFERENT spans
    can be interleaved into one true chronological chain (iter_thoughts).
    """
    span = _current_span.get()
    if span is None:
        return
    thoughts = span.attributes.setdefault("thoughts", [])
    entry: dict[str, Any] = {
        "decision": decision,
        "reasoning": reasoning,
        "tick": next(_seq_counter),
    }
    if data:
        entry["data"] = data
    thoughts.append(entry)


def instrument_llm(provider: ProviderLike, sink: TraceSink | None) -> ProviderLike:
    """Wrap a Provider so every complete() emits an llm span.

    This is how LLM calls become visible in Jaeger AND Langfuse-style tools:
    prompt, system prompt hash, model, latency, token counts, response preview.
    """

    class TracedProvider:
        name = getattr(provider, "name", "traced")

        def __init__(self) -> None:
            self.inner = provider

        async def complete(self, request: Any) -> Any:
            import hashlib

            sys_hash = hashlib.sha256((request.system or "").encode()).hexdigest()[:8]
            with tracer(
                "llm",
                f"{getattr(provider, 'name', 'provider')}.complete",
                sink=sink,
                model=getattr(request, "model", None) or "router-decided",
                temperature=request.temperature,
                prompt_chars=len(request.prompt),
                system_hash=sys_hash,
            ) as t:
                resp = await provider.complete(request)
                t.set("tokens_in", resp.tokens_in)
                t.set("tokens_out", resp.tokens_out)
                t.set("model", resp.model)
                t.set("response_preview", resp.text[:200])
                record_thought(
                    "llm_response_received",
                    reasoning=f"{resp.model} returned {len(resp.text)} chars",
                    latency_ms=round(resp.latency_s * 1000, 1),
                )
                return resp

    return TracedProvider()


# Structural type to avoid a circular import with router.providers.
from typing import Protocol as _Protocol
from typing import runtime_checkable


@runtime_checkable
class ProviderLike(_Protocol):
    name: str

    async def complete(self, request: Any) -> Any: ...


def iter_thoughts(spans: list[Span]) -> list[dict[str, Any]]:
    """Flatten all recorded thoughts across a trace's spans in true order.

    This is the CoT view: what did the system decide, and why, in sequence.
    Thoughts carry a global tick stamped at record time — per-span lists alone
    cannot interleave a parent's post-child thought correctly.
    """
    out: list[dict[str, Any]] = []
    for span in spans:
        for t in span.attributes.get("thoughts", []):
            out.append({"span": f"{span.kind}:{span.name}", **t})
    out.sort(key=lambda f: f.get("tick", 0))
    return out
