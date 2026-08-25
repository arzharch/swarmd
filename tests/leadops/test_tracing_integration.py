"""Tests for LeadOps tracing: span tree, CoT chain, LLM spans, JSONL export."""

from examples.leadops.pipeline import LeadOpsPipeline
from examples.leadops.sources.fixtures import load_leads
from swarmd.observability.tracing import (
    InMemoryTraceStore,
    iter_thoughts,
)
from swarmd.router.providers import MockProvider


async def test_pipeline_run_produces_full_span_tree() -> None:
    store = InMemoryTraceStore()
    pipe = LeadOpsPipeline(MockProvider(), trace_sink=store)
    res = await pipe.run(load_leads())

    assert res.trace_id != ""
    spans = store.by_trace(res.trace_id)
    kinds = {s.kind for s in spans}
    assert "stage" in kinds and "llm" in kinds and "approval" in kinds

    # Tree structure: root is leadops.run; stage spans are its children.
    root = next(s for s in spans if s.name == "leadops.run")
    child_names = {s.name for s in spans if s.parent_id == root.span_id}
    assert {"enrich", "dedupe", "score", "draft", "qa"} <= child_names


async def test_llm_spans_carry_prompt_and_token_data() -> None:
    """The Langfuse-style contract: every LLM call is inspectable."""
    store = InMemoryTraceStore()
    pipe = LeadOpsPipeline(MockProvider(), trace_sink=store)
    await pipe.run(load_leads()[:5])

    llm_spans = [s for s in store.spans if s.kind == "llm"]
    assert len(llm_spans) >= 5  # enrich + score + draft per lead at minimum
    for s in llm_spans:
        assert "prompt_chars" in s.attributes
        assert "tokens_in" in s.attributes and "tokens_out" in s.attributes
        assert "response_preview" in s.attributes


async def test_chain_of_thought_is_complete_and_ordered() -> None:
    store = InMemoryTraceStore()
    pipe = LeadOpsPipeline(MockProvider(), trace_sink=store)
    res = await pipe.run(load_leads())

    thoughts = iter_thoughts(store.by_trace(res.trace_id))
    decisions = [t["decision"] for t in thoughts]
    # The full reasoning chain, in execution order:
    assert decisions.index("ingest") < decisions.index("enrich_done")
    assert decisions.index("enrich_done") < decisions.index("dedupe_done")
    assert decisions.index("dedupe_done") < decisions.index("score_done")
    assert decisions.index("score_done") < decisions.index("draft_done")
    assert decisions.index("draft_done") < decisions.index("qa_done")
    assert decisions[-1] == "queued_for_human"
    # Every thought explains WHY.
    assert all(t.get("reasoning") for t in thoughts)


async def test_trace_renders_readable_tree() -> None:
    store = InMemoryTraceStore()
    pipe = LeadOpsPipeline(MockProvider(), trace_sink=store)
    res = await pipe.run(load_leads()[:3])

    tree = store.render_tree(res.trace_id)
    assert "stage:leadops.run" in tree
    assert "llm:" in tree  # LLM calls visible inline
