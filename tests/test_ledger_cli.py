"""Tests for the commands the runbook tells an on-call engineer to run.

A runbook naming a command that does not exist is worse than a runbook with a
gap: the gap is visible, and the missing command is found at 3am by someone who
now has to improvise. These tests also assert the runbook and the CLI have not
drifted apart again.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from swarmd import ledger_cli
from swarmd.ledger import CostAccount, JsonlLedger

REPO = Path(__file__).resolve().parents[1]
PAID = "z-ai/glm-5.3-flash"


@pytest.fixture
def ledger_file(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = JsonlLedger("run-abc", path)
    account = CostAccount(ledger, "run-abc", ceiling_usd=1.0)

    account.record(
        "criterion_frozen", stage="criterion",
        detail={"hash": "c0ffee1234567890", "attempts": 2},
    )
    account.record(
        "plan_selected", stage="plan",
        detail={"hash": "planhash00000000", "nodes": 3, "width": 2},
    )
    for i in range(4):
        account.charge_call(
            provider="openrouter", model=PAID, tokens_in=1000, tokens_out=200,
            agent_id=f"a{i:04d}", stage="solve",
        )
    account.charge_cache_hit(
        provider="openrouter", model=PAID, tokens_in=1000, tokens_out=200,
        agent_id="a0001", stage="solve",
    )
    account.record("success", agent_id="a0000", stage="solve")
    account.record(
        "containment", agent_id="a0003",
        detail={"pattern": "loop", "detail": "4 near-identical actions"},
    )
    ledger.close()
    return path


# --- the runbook contract --------------------------------------------------


def test_every_command_the_runbook_names_exists():
    """Docs drift is a bug (SPEC cross-cutting rule 7)."""
    runbook = (REPO / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    named = set(re.findall(r"swarmd (ledger|run|providers) (\w+)", runbook))

    result = subprocess.run(
        [sys.executable, "-m", "swarmd.cli", "--help"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    for group, sub in named:
        assert group in result.stdout, f"runbook names `swarmd {group}` which does not exist"
        help_text = subprocess.run(
            [sys.executable, "-m", "swarmd.cli", group, "--help"],
            capture_output=True, text=True, check=False, timeout=60,
        ).stdout
        assert sub in help_text, f"runbook names `swarmd {group} {sub}` which does not exist"


# --- report ----------------------------------------------------------------


def test_report_aggregates_from_rows(ledger_file):
    data = ledger_cli.report(ledger_file)
    assert data["llm_calls"] == 4
    assert data["cache_hits"] == 1
    assert data["total_usd"] == pytest.approx(4 * (0.000075 + 0.00005))
    assert data["by_stage"]["solve"] > 0


def test_report_uses_the_same_aggregation_as_a_live_run(ledger_file):
    """Two implementations of 'what did this cost' start disagreeing."""
    import inspect as _inspect

    source = _inspect.getsource(ledger_cli.report)
    assert "_account_for" in source  # goes through CostAccount, not a reimplementation


def test_report_surfaces_cache_savings(ledger_file):
    assert ledger_cli.report(ledger_file)["cache_savings_usd"] > 0


def test_a_missing_ledger_explains_why_it_is_missing(tmp_path):
    with pytest.raises(ledger_cli.LedgerNotFound, match="--ledger"):
        ledger_cli.report(tmp_path / "nope.jsonl")


# --- verify ----------------------------------------------------------------


def test_an_undamaged_ledger_verifies_intact(ledger_file):
    data = ledger_cli.verify(ledger_file)
    assert data["intact"] is True
    assert data["torn_lines"] == []


def test_a_torn_tail_is_distinguished_from_damage(ledger_file):
    """A hard kill mid-write truncates the tail; surviving rows are fine."""
    with ledger_file.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "run-abc", "seq": 99, "ki')

    data = ledger_cli.verify(ledger_file)
    assert data["intact"] is False
    assert data["tail_truncated"] is True
    assert data["torn_lines"]


def test_missing_rows_from_the_middle_are_reported_as_damage(tmp_path):
    """Aggregates from a gapped file understate the run — worse than a torn tail."""
    path = tmp_path / "gapped.jsonl"
    rows = [
        {"run_id": "r", "seq": 0, "ts": 1.0, "kind": "gate", "agent_id": "",
         "stage": "", "provider": "", "model": "", "tokens_in": 0,
         "tokens_out": 0, "cost_usd": 0.0, "would_have_cost": 0.0,
         "simulated": False, "detail": {}},
        {"run_id": "r", "seq": 3, "ts": 2.0, "kind": "gate", "agent_id": "",
         "stage": "", "provider": "", "model": "", "tokens_in": 0,
         "tokens_out": 0, "cost_usd": 0.0, "would_have_cost": 0.0,
         "simulated": False, "detail": {}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    data = ledger_cli.verify(path)
    assert data["intact"] is False
    assert data["tail_truncated"] is False
    assert data["gap_count"] == 2
    assert data["seq_gaps"] == [1, 2]


def test_verify_renders_the_distinction_for_a_human(ledger_file):
    with ledger_file.open("a", encoding="utf-8") as handle:
        handle.write('{"partial')
    rendered = ledger_cli.render_verify(ledger_cli.verify(ledger_file))
    assert "TAIL TRUNCATED" in rendered
    assert "trustworthy" in rendered


# --- inspect ---------------------------------------------------------------


def test_inspect_recovers_the_frozen_criterion(ledger_file):
    """The criterion hash is what a result was graded against."""
    data = ledger_cli.inspect(ledger_file)
    assert data["criterion"]["hash"] == "c0ffee1234567890"
    assert data["criterion"]["attempts"] == 2


def test_inspect_recovers_the_generated_plan(ledger_file):
    data = ledger_cli.inspect(ledger_file)
    assert data["plan"]["hash"] == "planhash00000000"
    assert data["plan"]["width"] == 2


def test_inspect_lists_containments_for_the_audit(ledger_file):
    data = ledger_cli.inspect(ledger_file)
    assert len(data["containments"]) == 1
    assert data["containments"][0]["agent_id"] == "a0003"
    assert data["containments"][0]["pattern"] == "loop"


def test_inspect_ranks_spenders(ledger_file):
    data = ledger_cli.inspect(ledger_file)
    assert data["top_spenders"]
    assert all(a["cost"] >= 0 for a in data["top_spenders"])
    contained = [a for a in data["top_spenders"] if a["contained"]]
    assert contained and contained[0]["agent_id"] == "a0003"


def test_inspect_flags_simulated_rows(tmp_path):
    path = tmp_path / "sim.jsonl"
    ledger = JsonlLedger("run-sim", path)
    CostAccount(ledger, "run-sim", ceiling_usd=1.0).charge_call(
        provider="simulated", model="simulated-v1",
        tokens_in=10, tokens_out=5, simulated=True,
    )
    ledger.close()

    data = ledger_cli.inspect(path)
    assert data["simulated"] is True
    assert "not evidence" in ledger_cli.render_inspect(data)


# --- the CLI surface -------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swarmd.cli", *args],
        capture_output=True, text=True, check=False, timeout=60,
    )


def test_ledger_report_runs_from_the_command_line(ledger_file):
    result = _cli("ledger", "report", str(ledger_file))
    assert result.returncode == 0, result.stderr
    assert "cost" in result.stdout


def test_ledger_verify_exits_zero_on_a_torn_tail(ledger_file):
    """A torn tail is what a hard kill looks like, not a failure."""
    with ledger_file.open("a", encoding="utf-8") as handle:
        handle.write("{broken")
    assert _cli("ledger", "verify", str(ledger_file)).returncode == 0


def test_ledger_verify_exits_nonzero_on_real_damage(tmp_path):
    """So a script can branch on it."""
    path = tmp_path / "gapped.jsonl"
    path.write_text(
        json.dumps({"run_id": "r", "seq": 0, "ts": 1.0, "kind": "gate",
                    "agent_id": "", "stage": "", "provider": "", "model": "",
                    "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                    "would_have_cost": 0.0, "simulated": False, "detail": {}})
        + "\n"
        + json.dumps({"run_id": "r", "seq": 5, "ts": 2.0, "kind": "gate",
                      "agent_id": "", "stage": "", "provider": "", "model": "",
                      "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                      "would_have_cost": 0.0, "simulated": False, "detail": {}}),
        encoding="utf-8",
    )
    assert _cli("ledger", "verify", str(path)).returncode == 1


def test_run_inspect_can_print_only_the_criterion(ledger_file):
    result = _cli("run", "inspect", str(ledger_file), "--criterion")
    assert result.returncode == 0
    assert json.loads(result.stdout)["hash"] == "c0ffee1234567890"


def test_run_inspect_can_print_only_the_containments(ledger_file):
    result = _cli("run", "inspect", str(ledger_file), "--containments")
    assert json.loads(result.stdout)[0]["pattern"] == "loop"


def test_a_missing_file_exits_two_with_an_explanation(tmp_path):
    result = _cli("ledger", "report", str(tmp_path / "absent.jsonl"))
    assert result.returncode == 2
    assert "no ledger at" in result.stdout
