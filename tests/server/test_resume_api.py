"""Resuming a rationed run over HTTP.

A run parked on a spent session ration waits hours. That is longer than a pod
lives through a deploy, so the service has to be able to pick a run back up
from disk -- and to say, while it is parked, that it is waiting rather than
hung.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from swarmd.server.app import create_app
from swarmd.swarm.runstore import RunState, RunStore
from tests.server.test_app import FakeProvider


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setenv("SWARMD_RUN_STORE", str(root))
    return root


@pytest.fixture
def client(tmp_path, store_root):
    app = create_app(
        provider_factory=FakeProvider,
        skills_path=str(tmp_path / "skills.json"),
    )
    with TestClient(app) as c:
        yield c


def wait_for(client, run_id, status="completed", tries=200):
    for _ in range(tries):
        body = client.get(f"/api/runs/{run_id}").json()
        if body.get("status") == status:
            return body
        # TestClient drives the loop on the calling thread; another request is
        # what lets the background task advance.
    raise AssertionError(f"run {run_id} never reached {status}")


def test_a_run_is_persisted_where_a_resume_can_find_it(client, store_root):
    body = client.post("/api/runs", json={"task": "count the records",
                                          "profile": "smoke", "chaos": False}).json()
    wait_for(client, body["run_id"])

    listed = client.get("/api/runs/resumable").json()["runs"]
    assert body["run_id"] in {r["run_id"] for r in listed}


def test_resumable_lists_disk_not_the_in_process_registry(client, store_root):
    """The distinction the endpoint exists for: a restart empties the registry,
    and a parked run is exactly the one most likely to outlive its pod."""
    store = RunStore(store_root)
    store.save(
        RunState(
            run_id="run-fromdisk",
            task="left over from a pod that died",
            profile="smoke",
            status="paused",
            paused_reason="session_ration",
        )
    )
    listed = client.get("/api/runs/resumable").json()["runs"]
    row = next(r for r in listed if r["run_id"] == "run-fromdisk")
    assert row["status"] == "paused"
    assert row["paused_reason"] == "session_ration"
    assert client.get("/api/runs/run-fromdisk").status_code == 404


def test_resuming_an_unknown_run_is_404(client, store_root):
    assert client.post("/api/runs/run-nope/resume").status_code == 404


def test_resuming_a_document_this_build_cannot_read_is_409(client, store_root):
    """Not a 500: the request is well formed and the file is intact. What is
    wrong is that no retry will help."""
    store_root.mkdir(parents=True, exist_ok=True)
    (store_root / "run-old.json").write_text(
        json.dumps({"run_id": "run-old", "schema_version": 0}), encoding="utf-8"
    )
    assert client.post("/api/runs/run-old/resume").status_code == 409


def test_resuming_a_run_with_no_task_recorded_says_why(client, store_root):
    RunStore(store_root).save(
        RunState(run_id="run-notask", task="", profile="smoke")
    )
    response = client.post("/api/runs/run-notask/resume")
    assert response.status_code == 422
    assert "no task recorded" in response.json()["detail"]


def test_a_resumed_run_reports_a_terminal_status(client, store_root):
    """Submit and resume share one launcher for this reason: a second copy
    drifted into never recording the finish, leaving resumed runs permanently
    'running' in the dashboard."""
    first = client.post("/api/runs", json={"task": "count the records",
                                           "profile": "smoke",
                                           "chaos": False}).json()
    wait_for(client, first["run_id"])

    response = client.post(f"/api/runs/{first['run_id']}/resume")
    assert response.status_code == 202
    body = wait_for(client, first["run_id"])
    assert body["status"] == "completed"
    assert body["resumed_from_nodes"] >= 1


def test_resuming_a_live_run_is_refused(client, store_root):
    """Two live runs on one id interleave their writes into one document and
    one ledger, and the report then describes neither.

    The live task is planted rather than raced: a real run finishes in
    milliseconds against a fake provider, so racing it would make this pass or
    skip depending on scheduling.
    """
    import asyncio

    RunStore(store_root).save(
        RunState(run_id="run-live", task="already going", profile="smoke")
    )

    async def plant():
        app = client.app
        app.state.registry.tasks["run-live"] = asyncio.create_task(
            asyncio.sleep(30)
        )

    client.portal.call(plant)  # type: ignore[attr-defined]
    try:
        response = client.post("/api/runs/run-live/resume")
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
    finally:
        client.portal.call(  # type: ignore[attr-defined]
            lambda: client.app.state.registry.tasks["run-live"].cancel()
        )


def test_no_wait_reaches_the_run_rather_than_being_accepted_and_dropped(
    client, store_root, monkeypatch
):
    """The worst outcome is a flag that validates and does nothing: CI would
    hang for hours on a run it explicitly asked not to wait."""
    from swarmd.server import app as app_module

    seen: dict[str, object] = {}
    real = app_module.SwarmRun

    class Capturing(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            seen.update(kw)
            super().__init__(*a, **kw)

    monkeypatch.setattr(app_module, "SwarmRun", Capturing)

    response = client.post(
        "/api/runs",
        json={"task": "count the records", "profile": "smoke",
              "chaos": False, "no_wait": True},
    )
    assert response.status_code == 202
    assert seen.get("no_wait") is True
    assert seen.get("store") is not None, "the run was given no durable store"
    wait_for(client, response.json()["run_id"])


def test_waiting_is_the_default(client, store_root, monkeypatch):
    """A run that dies on a spent ration throws away everything it paid for,
    so the default has to be to wait."""
    from swarmd.server import app as app_module

    seen: dict[str, object] = {}
    real = app_module.SwarmRun

    class Capturing(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            seen.update(kw)
            super().__init__(*a, **kw)

    monkeypatch.setattr(app_module, "SwarmRun", Capturing)
    client.post("/api/runs", json={"task": "count the records",
                                   "profile": "smoke", "chaos": False})
    assert seen.get("no_wait") is False


def test_pace_says_whether_the_pool_is_waiting(client):
    """A parked run looks exactly like a hung one from outside -- no events, no
    errors, nothing finishing. This is what separates them."""
    body = client.get("/api/pace").json()
    assert "paused" in body
