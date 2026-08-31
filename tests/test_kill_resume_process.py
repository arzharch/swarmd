"""A run killed mid-execution resumes in a NEW PROCESS without re-buying work.

Every other resume test runs in one process, so none of them cross the boundary
the run store exists for. A ration pause lasts hours -- long enough to cross a
laptop sleep, a deploy or a Ctrl-C -- so "paused" only means something if the
run survives the process that started it.

The assertions are COUNTED PROVIDER CALLS and the integrity hash, because a
resume that silently re-bought everything still produces a correct answer.
A control run establishes what the task costs once; the kill/resume pair must
cost about the same in total, not twice.

Marked slow: it spawns three real processes. Deselect with `-k "not process"`.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

CHILD = '''
import asyncio, json, os, sys
sys.path.insert(0, r"{repo}/src")
from swarmd.router.budget import BudgetSpec, BudgetTracker, Limit, UsageJournal
from swarmd.router.ration import Ration
from swarmd.router.pacer import Pacer
from swarmd.router.pool import ProviderPool, ProviderSpec, _Slot
from swarmd.router.simulated import SimulatedProvider
from swarmd.swarm.run import SwarmRun
from swarmd.swarm.runstore import RunStore

MODE, RUN_ID = sys.argv[1], sys.argv[2]
BUDGETS = {{"simulated": BudgetSpec(provider="simulated", kind="quota",
    limits=(Limit("day", requests={day_limit}),), reset="rolling",
    source="test", checked="test")}}

async def main():
    tracker = BudgetTracker(journal=UsageJournal(os.environ["SWARMD_USAGE_JOURNAL"]),
                            budgets=BUDGETS)
    spec = ProviderSpec(name="simulated", base_url="", api_key_env="",
                        models=("simulated-v1",), tier="simulated",
                        hint_rpm=100000, hint_tpm=100000000)
    pool = ProviderPool([_Slot(SimulatedProvider(), spec, credential_id="simulated#0")],
                        budget=tracker, ration=Ration(tracker),
                        pacer=Pacer(heartbeat_s=1.0), max_wait_s=0.5)
    store = RunStore(os.environ["SWARMD_RUN_STORE"])
    task = "extract the numeric claims from: accuracy was 94.3 and baseline 82.1"
    run = (SwarmRun.resume(RUN_ID, pool, store=store) if MODE == "resume"
           else SwarmRun(pool, profile="smoke", agents=15, run_id=RUN_ID, store=store))
    result = await run.run(task)
    print("RESULT " + json.dumps({{"status": result.status,
        "nodes": len(result.results), "hash": result.integrity_hash()}}), flush=True)
    await pool.aclose()

asyncio.run(main())
'''


def _calls(journal: pathlib.Path) -> int:
    """Provider calls, read from the journal both processes append to.

    Counted here rather than in the run because neither process can rewrite it,
    so a resume cannot flatter itself.
    """
    if not journal.exists():
        return 0
    return sum(
        1 for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("kind") != "reserve"
    )


def _env(journal: pathlib.Path, runs: pathlib.Path) -> dict[str, str]:
    return {
        **os.environ,
        "SWARMD_SIMULATED_PROVIDER": "true",
        "SWARMD_USAGE_JOURNAL": str(journal),
        "SWARMD_RUN_STORE": str(runs),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _result(out: str) -> dict:
    for line in out.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return {}


@pytest.mark.slow
def test_a_kill_mid_execution_resumes_without_re_buying_the_work(tmp_path):
    child = tmp_path / "child.py"

    # Control: what this task costs uninterrupted.
    cj, cr = tmp_path / "c.jsonl", tmp_path / "c-runs"
    child.write_text(
        CHILD.format(repo=REPO.as_posix(), day_limit=100000), encoding="utf-8"
    )
    done = subprocess.run(
        [sys.executable, str(child), "start", "run-control"], cwd=str(REPO),
        env=_env(cj, cr), capture_output=True, text=True, timeout=300, check=False,
    )
    control = _result(done.stdout + done.stderr)
    assert control, f"control run produced nothing: {(done.stdout + done.stderr)[-500:]}"
    control_calls = _calls(cj)

    # Experiment: a ration small enough that the run parks with nodes already
    # finished. Parking during synthesis proves the pause works but stores
    # almost nothing, so it cannot show that a resume avoids re-buying.
    ej, er = tmp_path / "e.jsonl", tmp_path / "e-runs"
    # 35, halved from 70 when the pool stopped charging each call to the day
    # TWICE (a ration reserve/settle pair AND a second full-cost usage row, both
    # in the same journal). The number is "small enough that this task parks
    # partway through", so a fix that made every call cost half as much had to
    # move it or the run would finish without ever parking -- which is exactly
    # how this test failed, and it failed LOUDLY rather than passing vacuously.
    child.write_text(
        CHILD.format(repo=REPO.as_posix(), day_limit=35), encoding="utf-8"
    )
    first = subprocess.Popen(
        [sys.executable, str(child), "start", "run-drop"], cwd=str(REPO),
        env=_env(ej, er), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    doc = er / "run-drop.json"
    # 600s, not 120s. The child has to spend a whole session ration -- roughly
    # fifteen real provider round trips through the simulated stack -- before it
    # parks, and on a loaded machine that overran 120s often enough that this
    # test failed about half the time. A gate that flaky is worse than no gate:
    # it trains people to re-run rather than read. The number is a CEILING on
    # patience, not an expected duration; the run parks in a few seconds when
    # the box is idle, and nothing waits longer than it needs to.
    parked, deadline = False, time.time() + 600
    while time.time() < deadline and first.poll() is None:
        if doc.exists():
            try:
                state = json.loads(doc.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if state.get("status") == "paused":
                parked = True
                break
        time.sleep(0.4)

    before = _calls(ej)
    if first.poll() is None:
        # SIGKILL equivalent: no cleanup handler runs, which is the point.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(first.pid)] if os.name == "nt"
            else ["kill", "-9", str(first.pid)],
            capture_output=True, check=False,
        )
        first.wait(timeout=30)
    if not parked:
        pytest.fail(
            "the run never parked; child said: "
            + (first.stdout.read() if first.stdout else "")[-800:]
        )

    state = json.loads(doc.read_text(encoding="utf-8"))
    assert state["criterion"], "criterion was not persisted before the kill"
    assert state["plan"], "plan was not persisted before the kill"
    assert state["results"] or state["drafts"], (
        "the run parked during synthesis, not mid-execution, so this proves "
        "nothing about reusing finished work"
    )

    child.write_text(
        CHILD.format(repo=REPO.as_posix(), day_limit=100000), encoding="utf-8"
    )
    second = subprocess.run(
        [sys.executable, str(child), "resume", "run-drop"], cwd=str(REPO),
        env=_env(ej, er), capture_output=True, text=True, timeout=300, check=False,
    )
    resumed = _result(second.stdout + second.stderr)
    assert resumed, f"resume produced nothing: {(second.stdout + second.stderr)[-500:]}"

    assert resumed["nodes"] >= control["nodes"], "the resumed run lost work"
    # The number that makes it the same work rather than merely finished work.
    assert resumed["hash"] == control["hash"]
    # Total cost across BOTH processes, near one run rather than two.
    assert _calls(ej) <= control_calls * 1.35, (
        f"{_calls(ej)} calls across the kill against {control_calls} "
        f"uninterrupted: the resume re-bought work"
    )
    assert _calls(ej) > before, "the resumed run did nothing"
