"""Tests for tracing: span trees, CoT capture, LLM instrumentation, sinks."""

from swarmd.observability.tracing import (
    CompositeSink,
    InMemoryTraceStore,
    JsonlTraceSink,
    instrument_llm,
    iter_thoughts,
    record_thought,
    tracer,
)
from swarmd.router.providers import LLMRequest, MockProvider


def test_nested_spans_form_tree() -> None:
    store = InMemoryTraceStore()
    with tracer("stage", "enrich", sink=store):
        with tracer("llm", "normalize", sink=store):
            pass
        with tracer("tool", "fetch", sink=store):
            pass

    assert len(store.spans) == 3
    root = next(s for s in store.spans if s.parent_id is None)
    children = [s for s in store.spans if s.parent_id == root.span_id]
    assert len(children) == 2
    tree = store.render_tree(root.trace_id)
    assert "stage:enrich" in tree and "llm:normalize" in tree


async def test_async_traced_sections_propagate_context() -> None:
    store = InMemoryTraceStore()
    async with tracer("chain", "outer", sink=store) as t:
        await asyncio_sleep()
        record_thought("picked_path_a", reasoning="cheaper and sufficient")
        t.set("custom", 42)

    span = store.spans[0]
    assert span.attributes["custom"] == 42
    thoughts = span.attributes["thoughts"]
    assert thoughts[0]["decision"] == "picked_path_a"


async def asyncio_sleep() -> None:
    import asyncio

    await asyncio.sleep(0)


def test_llm_instrumentation_records_prompt_and_response() -> None:
    store = InMemoryTraceStore()
    provider = instrument_llm(MockProvider(), store)

    import asyncio

    resp = asyncio.run(provider.complete(LLMRequest(prompt="hello world")))
    assert resp.text  # real response returned through the wrapper

    llm_spans = [s for s in store.spans if s.kind == "llm"]
    assert len(llm_spans) == 1
    attrs = llm_spans[0].attributes
    assert attrs["prompt_chars"] == len("hello world")
    assert "tokens_out" in attrs and "response_preview" in attrs
    # CoT thought recorded on the same span.
    assert any(
        t["decision"] == "llm_response_received"
        for t in attrs.get("thoughts", [])
    )


def test_composite_sink_fans_out() -> None:
    s1, s2 = InMemoryTraceStore(), InMemoryTraceStore()
    comp = CompositeSink(s1, s2)
    with tracer("gate", "qa", sink=comp):
        pass
    assert len(s1.spans) == 1 and len(s2.spans) == 1


def test_jsonl_sink_writes_lines(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(str(path))
    with tracer("approval", "review", sink=sink):
        pass
    content = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    assert '"kind": "approval"' in content[0]


def test_iter_thoughts_flattens_across_spans_in_order() -> None:
    store = InMemoryTraceStore()
    with tracer("stage", "run", sink=store):
        record_thought("first", reasoning="r1")
        with tracer("llm", "call", sink=store):
            record_thought("second", reasoning="r2")
        record_thought("third", reasoning="r3")

    flat = iter_thoughts(store.spans)
    decisions = [f["decision"] for f in flat]
    assert decisions == ["first", "second", "third"]
    assert all("span" in f for f in flat)


def test_sink_errors_never_break_the_run() -> None:
    class Broken:
        def export(self, span):
            raise RuntimeError("backend down")

    with tracer("tool", "x", sink=Broken()):  # must not raise
        pass


def test_trace_ids_shared_within_tree() -> None:
    store = InMemoryTraceStore()
    with tracer("stage", "a", sink=store), tracer("tool", "b", sink=store):
        pass
    ids = {s.trace_id for s in store.spans}
    assert len(ids) == 1
