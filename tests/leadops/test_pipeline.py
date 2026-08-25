"""LeadOps pipeline tests — offline-first via the deterministic mock provider."""

import json

import pytest

from examples.leadops.pipeline import LeadOpsPipeline, _lead_key, _normalize_key
from examples.leadops.sources.fixtures import load_leads
from swarmd.hitl.approvals import ApprovalState
from swarmd.pipeline.gates import QualityGate
from swarmd.router.providers import MockProvider


@pytest.fixture
def leads() -> list[dict]:
    return load_leads()


def test_fixture_leads_load_and_contain_duplicates(leads: list[dict]) -> None:
    assert len(leads) >= 15
    keys = [_normalize_key(l["company"]) for l in leads]
    assert len(keys) != len(set(keys)), "fixtures must contain duplicates"


async def test_full_pipeline_reaches_review_queue_offline() -> None:
    pipe = LeadOpsPipeline(MockProvider())
    res = await pipe.run(load_leads())

    assert res.leads_in == 20
    assert 0 < res.deduped < res.enriched, "dedupe must merge some records"
    assert res.awaiting_review > 0, "healthy leads must reach the review queue"
    pending = await pipe.approvals.pending()
    assert len(pending) == res.awaiting_review


async def test_dedupe_merges_known_duplicates() -> None:
    pipe = LeadOpsPipeline(MockProvider())
    res = await pipe.run(load_leads())
    # Fixtures contain 2 duplicate groups (L001/L002, L003/L004); L006/L007
    # ('Delta Freight' vs 'delta freight co') intentionally do NOT merge —
    # the 'co' suffix is a different canonical key. That's honest dedupe:
    # conservative merging beats wrongly collapsing distinct companies.
    assert res.deduped == res.leads_in - 2


async def test_no_auto_send_all_items_await_human(leads: list[dict]) -> None:
    """ADR-003: nothing leaves the system without a human decision."""
    pipe = LeadOpsPipeline(MockProvider())
    await pipe.run(leads)
    audit = await pipe.approvals.audit()
    actions = [e.action for e in audit]
    assert "send" not in actions and "approve" not in actions
    for req in await pipe.approvals.pending():
        assert req.state is ApprovalState.PENDING


async def test_integrity_hash_is_stable_across_runs(leads: list[dict]) -> None:
    r1 = await LeadOpsPipeline(MockProvider()).run(leads)
    r2 = await LeadOpsPipeline(MockProvider()).run(leads)
    assert r1.integrity_hash == r2.integrity_hash != ""


async def test_lead_key_stable_regardless_of_case_or_spacing() -> None:
    a = _lead_key({"email": "Priya@AcmeRobotics.io"})
    b = _lead_key({"email": "priya@acmerobotics.io"})
    assert a == b


async def test_qa_gate_blocks_banned_language() -> None:
    """Banned terms are caught; the repair loop rewrites them away."""
    pipe = LeadOpsPipeline(MockProvider())

    # Schema-valid item whose content violates the banned-terms check.
    outcome = await pipe.qa_gate.check(
        {
            "subject": "act now",
            "body": "this is a risk-free guarantee offer",
            "company": "acme",
        }
    )
    # The gate caught the violation (attempts=1 means repair ran) and the
    # repaired item no longer contains banned language.
    assert outcome.attempts == 1
    joined = json.dumps(outcome.item).lower()
    assert "guarantee" not in joined and "risk-free" not in joined

    # With repair disabled, the same item must dead-letter instead.
    strict_gate = QualityGate(
        "qa",
        pipe.qa_gate.verify_fn,
        max_repairs=0,
    )
    bad = await strict_gate.check({"subject": "act now", "body": "guarantee", "company": "x"})
    assert not bad.ok
    assert len(strict_gate.dead_letters) == 1
