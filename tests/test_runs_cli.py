"""`swarmd runs list` and `swarmd runs resume`.

The local counterpart to the resume API. A run parked on a spent ration
outlives the terminal that started it, so there has to be a way to find it
again that does not involve reading JSON out of a dot-directory by hand.
"""

from __future__ import annotations

import json

import pytest

from swarmd.cli import main
from swarmd.swarm.runstore import RunState, RunStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setenv("SWARMD_RUN_STORE", str(root))
    return RunStore(root)


def test_list_says_so_when_there_is_nothing_to_resume(store, capsys):
    assert main(["runs", "list"]) == 0
    assert "no stored runs" in capsys.readouterr().out


def test_list_shows_a_parked_run_and_when_it_comes_back(store, capsys):
    import time

    store.save(
        RunState(
            run_id="run-parked",
            task="extract the numeric claims from the abstract",
            profile="smoke",
            status="paused",
            paused_reason="session_ration",
            resumes_at=time.time() + 3 * 3600,
        )
    )
    assert main(["runs", "list"]) == 0
    out = capsys.readouterr().out
    assert "run-parked" in out
    assert "paused" in out
    assert "session_ration" in out
    # The number an operator actually needs: not that it is paused, but when it
    # stops being paused.
    assert "3.0h" in out


def test_list_json_is_machine_readable(store, capsys):
    store.save(RunState(run_id="run-a", task="t", profile="smoke"))
    assert main(["runs", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in rows] == ["run-a"]


def test_resuming_an_unknown_run_fails_loudly(store, capsys, monkeypatch):
    monkeypatch.setenv("SWARMD_SIMULATED_PROVIDER", "true")
    assert main(["runs", "resume", "run-nope"]) == 2
    assert "no stored run" in capsys.readouterr().out


def test_resuming_a_run_with_no_task_says_why(store, capsys, monkeypatch):
    monkeypatch.setenv("SWARMD_SIMULATED_PROVIDER", "true")
    store.save(RunState(run_id="run-notask", task="", profile="smoke"))
    assert main(["runs", "resume", "run-notask"]) == 2
    assert "no task recorded" in capsys.readouterr().out


def test_a_document_from_another_build_is_refused_rather_than_misread(
    store, capsys, monkeypatch
):
    monkeypatch.setenv("SWARMD_SIMULATED_PROVIDER", "true")
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "run-old.json").write_text(
        json.dumps({"run_id": "run-old", "schema_version": 0}), encoding="utf-8"
    )
    assert main(["runs", "resume", "run-old"]) == 2
    assert "schema" in capsys.readouterr().out


def test_swarm_run_takes_no_wait():
    """A flag the runbook names has to exist. Parsed here rather than run,
    because reaching the pause needs a spent ration."""
    import contextlib
    import io

    # argparse exits 0 after printing help; the assertion is that --no-wait
    # appears in it, which fails if the flag was never registered.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
        main(["swarm", "run", "--help"])
    assert "--no-wait" in buffer.getvalue()


def test_runs_resume_takes_no_wait():
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
        main(["runs", "resume", "--help"])
    assert "--no-wait" in buffer.getvalue()


def test_resume_completes_a_stored_run_end_to_end(store, capsys, monkeypatch):
    """The whole point, exercised: interrupt a run, resume it from the command
    line, and get a finished report."""
    monkeypatch.setenv("SWARMD_SIMULATED_PROVIDER", "true")

    assert main(["swarm", "run", "count the records", "--profile", "smoke",
                 "--agents", "4"]) in (0, 1)
    capsys.readouterr()

    stored = store.list_runs()
    assert stored, "the run left nothing on disk to resume"
    run_id = stored[0].run_id

    assert main(["runs", "resume", run_id]) in (0, 1)
    out = capsys.readouterr().out
    assert f"resuming {run_id}" in out
    assert "integrity_hash=" in out
