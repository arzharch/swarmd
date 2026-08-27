"""Ledger tests.

The load-bearing property being tested is ADR-007: every reported number is an
aggregate over rows, and no counter exists that could drift from them.
"""

from __future__ import annotations

import json

import pytest

from swarmd.ledger import (
    FREE,
    PRICES,
    CeilingExceeded,
    CostAccount,
    InMemoryLedger,
    JsonlLedger,
    LedgerRow,
    ModelPrice,
    UnpricedModel,
    price_for,
)

PAID = "z-ai/glm-5.3-flash"


# --- pricing ---------------------------------------------------------------


def test_price_math_is_per_million_tokens():
    price = ModelPrice(input_per_m=1.0, output_per_m=2.0)
    assert price.cost(1_000_000, 0) == pytest.approx(1.0)
    assert price.cost(0, 1_000_000) == pytest.approx(2.0)
    assert price.cost(500_000, 500_000) == pytest.approx(1.5)


def test_free_models_resolve_to_zero():
    assert price_for("openrouter", "meta-llama/llama-3.3-70b-instruct:free") is FREE
    assert price_for("groq", "llama-3.3-70b-versatile") is FREE
    assert price_for("cerebras", "anything") is FREE


def test_known_paid_model_resolves_to_its_table_entry():
    assert price_for("openrouter", PAID) is PRICES[PAID]


def test_unknown_model_refuses_rather_than_assuming_free():
    """A model silently priced at zero disables the ceiling entirely."""
    with pytest.raises(UnpricedModel):
        price_for("some-provider", "some/unknown-model")


# --- ledgers ---------------------------------------------------------------


def test_in_memory_ledger_assigns_monotonic_seq():
    led = InMemoryLedger("run-1")
    for i in range(5):
        assert led.next_seq() == i
        led.append(LedgerRow(run_id="run-1", seq=i, ts=0.0, kind="gate"))
    assert [r.seq for r in led.rows()] == [0, 1, 2, 3, 4]


def test_jsonl_ledger_survives_process_boundary(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = JsonlLedger("run-1", path)
    led.append(LedgerRow(run_id="run-1", seq=0, ts=1.0, kind="llm_call", cost_usd=0.001))
    led.append(LedgerRow(run_id="run-1", seq=1, ts=2.0, kind="llm_call", cost_usd=0.002))
    led.close()

    # A different process would see exactly this.
    reopened = JsonlLedger("run-1", path)
    disk = reopened.read_durable()
    assert [r.seq for r in disk] == [0, 1]
    assert sum(r.cost_usd for r in disk) == pytest.approx(0.003)


def test_jsonl_ledger_tolerates_a_torn_final_line(tmp_path):
    """A hard kill mid-write leaves a partial line; earlier rows must survive."""
    path = tmp_path / "ledger.jsonl"
    led = JsonlLedger("run-1", path)
    led.append(LedgerRow(run_id="run-1", seq=0, ts=1.0, kind="llm_call", cost_usd=0.001))
    led.close()
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"run_id": "run-1", "seq": 1, "ts": 2.0, "ki')

    rows = JsonlLedger("run-1", path).read_durable()
    assert len(rows) == 1
    assert rows[0].cost_usd == pytest.approx(0.001)


# --- accounting ------------------------------------------------------------


def _account(ceiling=0.05, run_id="run-1"):
    return CostAccount(InMemoryLedger(run_id), run_id, ceiling_usd=ceiling)


def test_total_cost_is_a_sum_over_rows_not_a_counter():
    """Appending directly to the ledger must move the reported total.

    If total_cost() were an internal counter incremented by charge_call, this
    row would be invisible to it -- which is exactly the drift ADR-007 forbids.
    """
    acct = _account()
    acct.ledger.append(
        LedgerRow(run_id="run-1", seq=0, ts=0.0, kind="llm_call", cost_usd=0.01)
    )
    assert acct.total_cost() == pytest.approx(0.01)
    assert acct.report()["total_usd"] == pytest.approx(0.01)


def test_free_calls_cost_nothing_but_still_write_rows():
    acct = _account()
    acct.charge_call(
        provider="groq", model="llama-3.3-70b-versatile", tokens_in=1000, tokens_out=500
    )
    assert acct.total_cost() == 0.0
    assert acct.report()["llm_calls"] == 1
    assert acct.report()["tokens_out"] == 500


def test_paid_call_is_charged_at_table_rates():
    acct = _account(ceiling=1.0)
    cost = acct.charge_call(
        provider="openrouter", model=PAID, tokens_in=1_000_000, tokens_out=1_000_000
    )
    # 0.075 in + 0.25 out
    assert cost == pytest.approx(0.325)


def test_ceiling_breach_raises_with_an_itemised_report():
    acct = _account(ceiling=0.001)
    with pytest.raises(CeilingExceeded) as exc:
        acct.charge_call(
            provider="openrouter", model=PAID, tokens_in=1_000_000, tokens_out=0
        )
    report = exc.value.report
    assert report["total_usd"] == pytest.approx(0.075)
    assert report["by_provider"]["openrouter"] == pytest.approx(0.075)
    assert report["by_model"][PAID] == pytest.approx(0.075)


def test_breaching_call_is_still_recorded():
    """The row must exist even though the call broke the ceiling.

    Dropping it would make the ledger disagree with what was actually spent --
    the abort report would understate the bill.
    """
    acct = _account(ceiling=0.001)
    with pytest.raises(CeilingExceeded):
        acct.charge_call(
            provider="openrouter", model=PAID, tokens_in=1_000_000, tokens_out=0
        )
    assert len(acct.ledger.rows()) == 1
    assert acct.total_cost() == pytest.approx(0.075)


def test_precheck_refuses_a_call_that_cannot_fit():
    acct = _account(ceiling=0.001)
    with pytest.raises(CeilingExceeded):
        acct.precheck("openrouter", PAID, est_tokens=1_000_000)


def test_precheck_allows_a_call_that_fits():
    acct = _account(ceiling=0.05)
    acct.precheck("openrouter", PAID, est_tokens=1000)  # ~$0.00025


def test_precheck_never_blocks_free_models():
    acct = _account(ceiling=0.0)
    acct.precheck("groq", "llama-3.3-70b-versatile", est_tokens=10_000_000)


def test_reserve_keeps_headroom_below_the_nominal_ceiling():
    """The abort path itself needs to afford to run."""
    acct = _account(ceiling=1.0)
    assert acct.reserve_usd == pytest.approx(0.02)
    assert acct.remaining() == pytest.approx(0.98)


def test_cache_hits_are_zero_cost_rows_that_make_savings_queryable():
    acct = _account()
    acct.charge_cache_hit(
        provider="openrouter", model=PAID, tokens_in=1_000_000, tokens_out=1_000_000
    )
    assert acct.total_cost() == 0.0
    assert acct.cache_savings() == pytest.approx(0.325)
    assert acct.report()["cache_hit_rate"] == 1.0


def test_cache_hit_rate_is_over_attempts_not_calls():
    acct = _account()
    acct.charge_call(provider="groq", model="m", tokens_in=1, tokens_out=1)
    acct.charge_cache_hit(provider="groq", model="m", tokens_in=1, tokens_out=1)
    acct.charge_cache_hit(provider="groq", model="m", tokens_in=1, tokens_out=1)
    assert acct.report()["cache_hit_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_report_itemises_by_provider_model_and_stage():
    acct = _account()
    acct.charge_call(
        provider="openrouter", model=PAID, tokens_in=100_000, tokens_out=0, stage="plan"
    )
    acct.charge_call(
        provider="groq", model="free-m", tokens_in=100_000, tokens_out=0, stage="solve"
    )
    rep = acct.report()
    assert set(rep["by_provider"]) == {"openrouter", "groq"}
    assert set(rep["by_stage"]) == {"plan", "solve"}
    assert rep["by_stage"]["solve"] == 0.0
    assert rep["by_stage"]["plan"] == pytest.approx(0.0075)


def test_non_billable_facts_are_recorded_alongside_calls():
    acct = _account()
    acct.record("containment", agent_id="a-7", detail={"pattern": "loop"})
    acct.record("success", agent_id="a-2", stage="solve")
    kinds = [r.kind for r in acct.ledger.rows()]
    assert kinds == ["containment", "success"]
    assert acct.report()["llm_calls"] == 0


def test_verify_confirms_memory_matches_disk(tmp_path):
    led = JsonlLedger("run-1", tmp_path / "l.jsonl")
    acct = CostAccount(led, "run-1")
    acct.charge_call(provider="openrouter", model=PAID, tokens_in=1000, tokens_out=100)
    v = acct.verify()
    assert v["durable"] is True
    assert v["match"] is True
    assert v["cost_on_disk"] == pytest.approx(v["cost_in_memory"])


def test_verify_reports_rather_than_raises_for_non_durable_ledgers():
    acct = _account()
    assert acct.verify()["durable"] is False


def test_rows_serialise_to_one_json_object_per_line(tmp_path):
    path = tmp_path / "l.jsonl"
    led = JsonlLedger("run-1", path)
    CostAccount(led, "run-1").charge_call(
        provider="groq", model="m", tokens_in=1, tokens_out=1, agent_id="a-1"
    )
    led.close()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["agent_id"] == "a-1"
