"""One Idempotency-Key, one run -- across retries, restarts and double-clicks.

WHY THIS MATTERS ENOUGH TO TEST HARD. `POST /api/runs` answers 202 and does the
work in the background, so a client that loses the response cannot tell whether
its request arrived. The safe-looking move -- retry -- currently buys a second
population, a second criterion, a second plan and a second run's worth of
provider quota for one question. Against a measured ~1,146 requests/day
(docs/CAPACITY.md) a duplicated `standard` run is most of an afternoon.

Every test here is written against the OBSERVABLE contract (status code, body,
header, and how many runs actually got constructed) rather than against the
store's internals, because the contract is what a client depends on.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from swarmd.observability import metrics
from swarmd.server import app as app_module
from swarmd.server.app import create_app
from swarmd.server.idempotency import IDEMPOTENCY_TTL_S, IdempotencyStore
from swarmd.swarm.runstore import RunState, RunStore
from tests.server.test_app import FakeProvider

KEY = "idem-key-0001"
BODY = {"task": "count the records", "profile": "smoke", "chaos": False}


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    """One RunStore root for the whole test, shared by every app built in it.

    Idempotency records live under this root, which is what makes the
    "survives a restart" tests honest: a second `create_app` is as close to a
    new process as a single-process test can get, exactly as
    tests/router/test_journal_sharing.py does with two journals over one file.
    """
    root = tmp_path / "runs"
    monkeypatch.setenv("SWARMD_RUN_STORE", str(root))
    return root


@pytest.fixture
def make_app(tmp_path, store_root):
    def build():
        return create_app(
            provider_factory=FakeProvider, skills_path=str(tmp_path / "skills.json")
        )

    return build


@pytest.fixture
def client(make_app):
    with TestClient(make_app()) as c:
        yield c


@pytest.fixture
def constructed(monkeypatch):
    """Counts SwarmRun constructions. The number a duplicate must not move."""
    seen: list[str] = []
    real = app_module.SwarmRun

    class Counting(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen.append(self.run_id)

    monkeypatch.setattr(app_module, "SwarmRun", Counting)
    return seen


# --- the core promise -------------------------------------------------------


def test_the_same_key_and_body_replays_the_first_run(client, constructed):
    """The whole point: a retry gets the ORIGINAL run id and starts nothing.

    Answered 200 rather than 202 so a client that never reads headers can still
    tell "this was accepted just now" from "this was accepted earlier", and
    marked `Idempotent-Replay` for one that does.
    """
    first = client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})
    second = client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(constructed) == 1, f"a second run was constructed: {constructed}"


def test_a_reused_key_with_a_different_body_is_refused(client, constructed):
    """A key names ONE request. Answering a different body with the first
    run's id would hand a caller a run that does not do what they asked for --
    so this is refused rather than replayed, and refused BEFORE anything starts.
    """
    first = client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})
    conflict = client.post(
        "/api/runs",
        json={**BODY, "task": "a completely different question"},
        headers={"Idempotency-Key": KEY},
    )

    assert conflict.status_code == 422
    assert len(constructed) == 1
    # The other run's id is deliberately NOT disclosed: a client that reused a
    # key by accident has no claim on the run that key already names, and
    # leaking it would turn key-guessing into run disclosure.
    assert first.json()["run_id"] not in conflict.text


def test_no_key_still_means_a_new_run_every_time(client, constructed):
    """The regression guard on default behaviour. Re-running an identical task
    deliberately -- an A/B arm, a flake hunt, a chaos comparison -- must always
    produce a second run, and no environment flag may change that.
    """
    before = metrics.sample("swarmd_runs_submitted_total", idempotency="absent") or 0.0

    a = client.post("/api/runs", json=BODY)
    b = client.post("/api/runs", json=BODY)

    assert a.status_code == b.status_code == 202
    assert a.json()["run_id"] != b.json()["run_id"]
    assert len(constructed) == 2
    after = metrics.sample("swarmd_runs_submitted_total", idempotency="absent") or 0.0
    assert after - before == 2, "submissions without a key must be countable"


async def test_two_simultaneous_duplicates_produce_one_run(make_app, constructed):
    """The window the per-key lock exists for.

    Check-then-create is not atomic, so two requests arriving together could
    both find no record and both start a run -- which is exactly what a
    double-clicked button or a client retrying into the same pod produces.
    """
    app = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        first, second = await asyncio.gather(
            http.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY}),
            http.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY}),
        )

    assert {first.json()["run_id"], second.json()["run_id"]} == set(constructed)
    assert len(constructed) == 1, f"concurrent duplicates started {len(constructed)}"
    assert sorted([first.status_code, second.status_code]) == [200, 202]

    for task in list(app.state.registry.tasks.values()):
        task.cancel()
    await asyncio.gather(*app.state.registry.tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("key", ["short", "has spaces in it", "bad/char", "!" * 12])
def test_a_malformed_key_is_a_400(client, constructed, key):
    """400, not 422: the BODY is fine. What is wrong is a header, and a client
    cannot fix that by changing its payload.

    The 8-character floor is not cosmetic -- a client that "just picks
    something" short collides across unrelated requests, and a collision here
    means one caller is handed another caller's run.
    """
    response = client.post("/api/runs", json=BODY, headers={"Idempotency-Key": key})
    assert response.status_code == 400
    assert constructed == []


# --- durability -------------------------------------------------------------


def test_a_key_survives_a_restart(make_app, constructed, store_root):
    """The retry worth deduplicating is the one that arrives AFTER a deploy.

    An in-memory table would deduplicate only the cases that did not matter:
    the client that retries within one process is the client whose first
    request probably succeeded anyway.
    """
    with TestClient(make_app()) as first_pod:
        original = first_pod.post(
            "/api/runs", json=BODY, headers={"Idempotency-Key": KEY}
        ).json()["run_id"]

    with TestClient(make_app()) as second_pod:
        replay = second_pod.post(
            "/api/runs", json=BODY, headers={"Idempotency-Key": KEY}
        )

    assert replay.status_code == 200
    assert replay.json()["run_id"] == original
    assert len(constructed) == 1, "the second pod started a duplicate run"


def test_records_live_beside_the_run_documents(client, store_root):
    """Under the RunStore root deliberately: one `SWARMD_RUN_STORE` moves the
    working set, the memos and the keys together, so an operator clearing one
    cannot leave keys pointing at runs that are gone."""
    client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})
    assert list((store_root / "idempotency").glob("*.json"))


def test_an_expired_key_is_treated_as_unseen(client, constructed, store_root):
    """A key is a retry window, not a permanent lease. Reusing one next week
    for a different question must start a run rather than replay something
    unrelated."""
    first = client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})

    store = IdempotencyStore(store_root / "idempotency")
    path = store.path_for(KEY)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["created_ts"] -= IDEMPOTENCY_TTL_S * 2
    path.write_text(json.dumps(record), encoding="utf-8")

    second = client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})
    assert second.status_code == 202
    assert second.json()["run_id"] != first.json()["run_id"]
    assert len(constructed) == 2


def test_a_key_ages_out_with_the_run_it_points_at(store_root):
    """A record promises to keep answering with one run id. Once that run's
    document is swept, the promise is to a run nothing can describe, so the key
    goes with it rather than replaying an id no endpoint resolves."""
    runs = RunStore(store_root)
    runs.save(RunState(run_id="run-kept", task="t", profile="smoke"))
    store = IdempotencyStore(store_root / "idempotency")
    store.reserve("key-for-kept-run", "print")
    store.complete("key-for-kept-run", run_id="run-kept", status_code=202, body={})
    store.reserve("key-for-gone-run", "print")
    store.complete("key-for-gone-run", run_id="run-gone", status_code=202, body={})

    assert store.prune(run_store=runs) == 1
    assert store.get("key-for-kept-run") is not None
    assert store.get("key-for-gone-run") is None


# --- failure paths ----------------------------------------------------------


def test_a_failed_construction_releases_its_key(client, monkeypatch, store_root):
    """A reservation that is never released is worse than no idempotency at
    all: the client that retries after a transient failure is told its run is
    already being created -- forever, for a run that does not exist."""
    real = app_module.SwarmRun

    class Exploding(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            raise RuntimeError("provider wiring blew up")

    monkeypatch.setattr(app_module, "SwarmRun", Exploding)
    with pytest.raises(RuntimeError):
        client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})

    monkeypatch.setattr(app_module, "SwarmRun", real)
    retry = client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})
    assert retry.status_code == 202, "the key was left holding a phantom run"


def test_a_key_is_scoped_to_what_it_was_used_for(client, store_root):
    """One key used against submit and then against resume is a CONFLICT, not
    a replay: answering a resume with a submit's response would tell the caller
    a run was accepted that this call never touched."""
    RunStore(store_root).save(
        RunState(run_id="run-parked", task="count the records", profile="smoke")
    )
    client.post("/api/runs", json=BODY, headers={"Idempotency-Key": KEY})
    crossed = client.post(
        "/api/runs/run-parked/resume", headers={"Idempotency-Key": KEY}
    )
    assert crossed.status_code == 422


# --- resume -----------------------------------------------------------------


def test_a_double_resume_with_one_key_returns_the_same_run(client, store_root):
    """Two resumes of one parked run must not become two live runs.

    Without a key the second call is refused (409) because two runs sharing an
    id interleave their writes into one document and one ledger. With a key the
    retry is ANSWERED with the run the first call accepted, which is what a
    client that lost the first response actually needs.
    """
    RunStore(store_root).save(
        RunState(
            run_id="run-parked", task="count the records", profile="smoke",
            agents=15, status="paused",
        )
    )
    first = client.post(
        "/api/runs/run-parked/resume", headers={"Idempotency-Key": "resume-key-01"}
    )
    second = client.post(
        "/api/runs/run-parked/resume", headers={"Idempotency-Key": "resume-key-01"}
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["run_id"] == first.json()["run_id"] == "run-parked"


def test_one_resume_key_names_one_run(client, store_root):
    """The same conflict rule as submit: a key that has resumed run A must not
    silently resume run B."""
    store = RunStore(store_root)
    for run_id in ("run-a", "run-b"):
        store.save(
            RunState(run_id=run_id, task="count the records", profile="smoke",
                     agents=15, status="paused")
        )
    client.post("/api/runs/run-a/resume", headers={"Idempotency-Key": "resume-key-02"})
    crossed = client.post(
        "/api/runs/run-b/resume", headers={"Idempotency-Key": "resume-key-02"}
    )
    assert crossed.status_code == 422


def test_resuming_without_a_key_behaves_exactly_as_before(client, store_root):
    """The regression guard on the existing contract: no header, no change."""
    RunStore(store_root).save(
        RunState(run_id="run-plain", task="count the records", profile="smoke",
                 agents=15, status="paused")
    )
    assert client.post("/api/runs/run-plain/resume").status_code == 202
