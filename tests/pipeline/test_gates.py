"""Tests for quality gates: repair loop, dead-letter, taxonomy."""


from swarmd.harnesses.verify import VerifyHarness, schema_check
from swarmd.pipeline.gates import QualityGate, classify_failure


def make_gate(max_repairs: int = 1, repair_fn=None) -> QualityGate:
    verifier = VerifyHarness("v").add_check(schema_check(["company", "email"]))
    return QualityGate(
        "enrich", verifier.verify, max_repairs=max_repairs, repair_fn=repair_fn
    )


async def test_passing_item_flows_through() -> None:
    gate = make_gate()
    out = await gate.check({"company": "acme", "email": "a@b.c"})
    assert out.ok and out.attempts == 0
    assert gate.summary()["passed"] == 1


async def test_failing_item_without_repair_dead_letters_immediately() -> None:
    gate = make_gate(max_repairs=0)
    out = await gate.check({"company": "acme"})
    assert not out.ok
    assert len(gate.dead_letters) == 1
    assert gate.dead_letters[0].reason is not None


async def test_bounded_repair_recovers_bad_item() -> None:
    async def add_email(item: dict, reason: str) -> dict:
        return {**item, "email": "recovered@acme.io"}

    gate = make_gate(max_repairs=2, repair_fn=add_email)
    out = await gate.check({"company": "acme"})
    assert out.ok and out.attempts == 1
    assert gate.summary()["repaired"] == 1


async def test_unfixable_item_exhausts_repairs_then_dead_letters() -> None:
    async def useless_repair(item: dict, reason: str) -> dict:
        return item  # never fixes anything

    gate = make_gate(max_repairs=2, repair_fn=useless_repair)
    out = await gate.check({})
    assert not out.ok and out.attempts == 2
    assert len(gate.dead_letters) == 1
    s = gate.summary()
    assert s["dead_lettered"] == 1 and s["passed"] == 0


async def test_verifier_exception_counts_as_failure_not_crash() -> None:
    async def broken(item: dict):
        raise RuntimeError("verifier bug")

    gate = QualityGate("s", broken)
    out = await gate.check({"x": 1})
    assert not out.ok
    assert "verifier exception" in (out.reason or "")


def test_failure_taxonomy_classification() -> None:
    assert classify_failure("missing/empty fields: ['email']") == "schema"
    assert classify_failure("field score=11 outside [0, 10]") == "range"
    assert classify_failure("banned term 'free money' in note") == "content"
    assert classify_failure("something weird") == "other"


async def test_taxonomy_accumulates_across_items() -> None:
    gate = make_gate(max_repairs=0)
    await gate.check({})
    await gate.check({"email": "x@y.z"})  # missing company
    assert gate.taxonomy["schema"] == 2
