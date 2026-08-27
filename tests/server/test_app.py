"""Control plane and event hub tests.

The two properties that matter operationally:
  1. A slow dashboard cannot slow a run. Ever.
  2. /healthz and /readyz answer different questions, so a saturated pod stops
     taking work without being killed and losing what it holds.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from swarmd.router.providers import LLMResponse
from swarmd.server.app import create_app
from swarmd.server.hub import EventHub

CRITERION = {
    "description": "structured artifact",
    "checks": [
        {"kind": "json_parses", "params": {"required_keys": ["summary", "count"]}},
        {"kind": "min_distinct_words", "params": {"min_distinct": 6}},
    ],
}
PLAN = {"nodes": [{"name": "step", "instruction": "produce out.json",
                   "depends_on": []}]}
OUTPUT = json.dumps({"summary": "did the work carefully and thoroughly",
                     "count": 7, "detail": ["a", "b"]})


class FakeProvider:
    name = "fake"

    def __init__(self, available=True):
        self._available = available

    def status(self):
        return [{"provider": "fake", "available": self._available}]

    async def probe(self):
        """Same surface as ProviderPool, so the fake exercises the real path."""
        return [
            {
                "provider": "fake",
                "tier": "free",
                "ok": self._available,
                "model": "fake-v1",
                "latency_s": 0.01,
            }
        ]

    async def complete(self, request):
        if "checks" in request.prompt and "schema" in request.prompt:
            text = json.dumps(CRITERION)
        elif "schema" in request.prompt:
            text = json.dumps(PLAN)
        else:
            text = OUTPUT
        return LLMResponse(text=text, provider="fake", model="m",
                           latency_s=0.001, tokens_in=1, tokens_out=1)


@pytest.fixture
def client(tmp_path):
    app = create_app(
        provider_factory=FakeProvider,
        skills_path=str(tmp_path / "skills.json"),
    )
    with TestClient(app) as c:
        yield c


# --- the hub: a slow client must never slow a run ---------------------------


async def test_publishing_never_blocks_even_when_a_subscriber_is_full():
    """One viewer on bad wifi must not apply backpressure into the agent loop."""
    hub = EventHub(queue_size=4)
    hub.subscribe(replay=False)   # never drained

    for i in range(1000):
        hub.publish({"kind": "tick", "i": i})   # must not raise or block

    assert hub.total_published == 1000
    assert hub.total_dropped > 0


async def test_a_stuck_subscriber_keeps_the_newest_events_not_the_oldest():
    """A dashboard showing stale events is worse than one with a visible gap."""
    hub = EventHub(queue_size=3)
    subscriber = hub.subscribe(replay=False)
    for i in range(10):
        hub.publish({"kind": "tick", "i": i})

    seen = [subscriber.queue.get_nowait()["i"] for _ in range(3)]
    assert seen == [7, 8, 9]


async def test_drops_are_counted_and_reported_not_hidden():
    """SLO-4 budgets for these; an unreported drop makes the budget fiction."""
    hub = EventHub(queue_size=2)
    hub.subscribe(replay=False)
    for i in range(20):
        hub.publish({"kind": "tick", "i": i})

    stats = hub.stats()
    assert stats["dropped"] > 0
    assert 0 < stats["drop_rate"] <= 1.0


async def test_a_late_joining_dashboard_replays_recent_history():
    """Arriving at minute 10 of an 18-minute run must not show a blank page."""
    hub = EventHub()
    for i in range(5):
        hub.publish({"kind": "node_finished", "i": i})

    subscriber = hub.subscribe(replay=True)
    assert subscriber.queue.qsize() == 5


async def test_replay_can_be_declined():
    hub = EventHub()
    hub.publish({"kind": "x"})
    assert hub.subscribe(replay=False).queue.qsize() == 0


async def test_every_event_is_sequenced():
    """Ordering must be reconstructible after a drop."""
    hub = EventHub()
    hub.publish({"kind": "a"})
    hub.publish({"kind": "b"})
    assert [e["seq"] for e in hub.history()] == [1, 2]


async def test_unsubscribing_stops_delivery():
    hub = EventHub()
    subscriber = hub.subscribe(replay=False)
    hub.unsubscribe(subscriber)
    hub.publish({"kind": "x"})
    assert subscriber.queue.qsize() == 0
    assert hub.subscriber_count == 0


def test_publish_is_synchronous_by_design():
    """An async publish would invite `await`, which reintroduces backpressure."""
    assert not asyncio.iscoroutinefunction(EventHub().publish)


# --- probes ----------------------------------------------------------------


def test_healthz_reports_the_build_it_is_running(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "git_sha" in body and "build_time" in body


def test_readyz_is_green_with_available_providers(client):
    assert client.get("/readyz").status_code == 200


def test_readyz_goes_red_when_every_provider_is_backed_off(tmp_path):
    """New runs go elsewhere; the pod is NOT killed and keeps what it holds."""
    app = create_app(provider_factory=lambda: FakeProvider(available=False))
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503
        assert c.get("/healthz").status_code == 200   # still alive


def test_readyz_reports_503_when_no_providers_exist_at_all(tmp_path):
    def no_providers():
        raise RuntimeError("no usable providers")

    app = create_app(provider_factory=no_providers)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503
        assert c.get("/healthz").status_code == 200


def test_metrics_are_served_in_the_prometheus_text_format(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "swarmd_" in response.text


# --- runs ------------------------------------------------------------------


def test_submitting_a_run_returns_immediately(client):
    """An 18-minute run must not hold an HTTP connection open."""
    response = client.post(
        "/api/runs",
        json={"task": "summarise the records", "profile": "smoke", "chaos": False},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["stream"] == "/api/stream"


def test_an_unknown_profile_is_refused(client):
    response = client.post(
        "/api/runs", json={"task": "t", "profile": "enormous"}
    )
    assert response.status_code in (400, 422)


def test_an_empty_task_is_refused(client):
    assert client.post("/api/runs", json={"task": ""}).status_code == 422


def test_an_absurd_ceiling_is_refused(client):
    """A typo in the ceiling should not authorise a hundred-dollar run."""
    response = client.post(
        "/api/runs", json={"task": "t", "profile": "smoke", "ceiling_usd": 1000}
    )
    assert response.status_code == 422


def test_submitted_runs_are_listable(client):
    run_id = client.post(
        "/api/runs", json={"task": "t", "profile": "smoke", "chaos": False}
    ).json()["run_id"]
    runs = client.get("/api/runs").json()["runs"]
    assert any(r["run_id"] == run_id for r in runs)


def test_the_agent_count_is_selectable_per_run(client):
    """The profile encodes a wall-clock target, not the right population size."""
    run_id = client.post(
        "/api/runs",
        json={"task": "t", "profile": "smoke", "chaos": False, "agents": 12},
    ).json()["run_id"]
    run = next(
        r for r in client.get("/api/runs").json()["runs"] if r["run_id"] == run_id
    )
    assert run["agents"] == 12


def test_an_absurd_agent_count_is_refused(client):
    """Over HTTP, from a client that may have a typo in it."""
    response = client.post(
        "/api/runs", json={"task": "t", "profile": "smoke", "agents": 100_000}
    )
    assert response.status_code == 422


def test_a_zero_agent_run_is_refused(client):
    response = client.post(
        "/api/runs", json={"task": "t", "profile": "smoke", "agents": 0}
    )
    assert response.status_code == 422


def test_rogues_can_be_seeded_over_the_api(client):
    """SPEC Phase 8 is runnable as a service call, not only as a CLI flag."""
    response = client.post(
        "/api/runs",
        json={
            "task": "t", "profile": "smoke", "chaos": False, "seed_rogues": "all",
        },
    )
    assert response.status_code == 202


def test_a_misspelled_rogue_pattern_is_refused(client):
    """400, not a clean run.

    A typo that seeds nothing produces a run with zero containments, which is
    indistinguishable from a red-team gate that passed.
    """
    response = client.post(
        "/api/runs",
        json={"task": "t", "profile": "smoke", "seed_rogues": "loops"},
    )
    assert response.status_code == 400
    assert "unknown rogue pattern" in response.json()["detail"]


def test_an_unknown_run_is_404(client):
    assert client.get("/api/runs/nope").status_code == 404


def test_submitting_a_run_with_no_provider_returns_503(tmp_path):
    def no_providers():
        raise RuntimeError("no usable providers")

    app = create_app(provider_factory=no_providers)
    with TestClient(app) as c:
        response = c.post("/api/runs", json={"task": "t", "profile": "smoke"})
        assert response.status_code == 503


# --- streaming -------------------------------------------------------------


def test_the_websocket_replays_history_to_a_late_joiner(tmp_path):
    """A viewer connecting mid-run must see the run so far.

    Driven through the hub rather than by issuing an HTTP POST inside the
    websocket context: TestClient serves both over one portal thread, so a
    concurrent request there deadlocks the socket rather than testing it.
    """
    hub = EventHub()
    app = create_app(provider_factory=FakeProvider, hub=hub,
                     skills_path=str(tmp_path / "s.json"))
    hub.publish({"kind": "run_started", "run_id": "r1", "task": "t"})
    hub.publish({"kind": "criterion_frozen", "run_id": "r1", "hash": "abc"})

    with TestClient(app) as c, c.websocket_connect("/api/stream") as socket:
        assert socket.receive_json()["kind"] == "run_started"
        assert socket.receive_json()["kind"] == "criterion_frozen"


def test_the_websocket_carries_events_published_after_connecting(tmp_path):
    hub = EventHub()
    app = create_app(provider_factory=FakeProvider, hub=hub,
                     skills_path=str(tmp_path / "s.json"))
    with TestClient(app) as c, c.websocket_connect("/api/stream") as socket:
        hub.publish({"kind": "thought", "run_id": "r1", "decision": "planning"})
        received = socket.receive_json()
        assert received["kind"] == "thought"
        assert received["seq"] >= 1


def test_recent_events_are_available_without_a_websocket(client):
    client.post(
        "/api/runs", json={"task": "t", "profile": "smoke", "chaos": False}
    )
    assert "events" in client.get("/api/events").json()


def test_stream_stats_expose_the_drop_rate(client):
    stats = client.get("/api/stream/stats").json()
    assert "drop_rate" in stats and "subscribers" in stats


# --- human gates -----------------------------------------------------------


def test_approvals_are_listable(client):
    assert "pending" in client.get("/api/approvals").json()


def test_deciding_an_unknown_approval_is_404(client):
    assert client.post("/api/approvals/nope/approve").status_code == 404


def test_an_invalid_approval_action_is_refused(client):
    assert client.post("/api/approvals/x/maybe").status_code == 400


def test_skills_are_listable_with_pending_separated(client):
    body = client.get("/api/skills").json()
    assert "pending" in body and "approved" in body and "stats" in body


def test_deciding_an_unknown_skill_is_404(client):
    assert client.post("/api/skills/nope/approve").status_code == 404


def test_an_invalid_skill_action_is_refused(client):
    assert client.post("/api/skills/x/maybe").status_code == 400


# --- traceability ----------------------------------------------------------


def _completed_run(client):
    run_id = client.post(
        "/api/runs",
        json={"task": "summarise the records", "profile": "smoke", "chaos": False},
    ).json()["run_id"]
    # The run executes as a background task; poll until the report lands.
    for _ in range(80):
        body = client.get(f"/api/runs/{run_id}").json()
        if body.get("status") not in (None, "running"):
            return run_id
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_the_ledger_is_exposed_for_traceability(client):
    """Every reported number is an aggregate over these rows (ADR-007), so
    serving them is what makes a figure checkable rather than trusted."""
    run_id = _completed_run(client)
    body = client.get(f"/api/runs/{run_id}/ledger").json()
    assert body["total"] > 0
    assert body["rows"]
    # The decisions that gate everything downstream must be traceable to a row.
    assert "criterion_frozen" in body["kinds"]
    assert "plan_selected" in body["kinds"]


def test_ledger_rows_can_be_filtered_by_kind(client):
    run_id = _completed_run(client)
    body = client.get(f"/api/runs/{run_id}/ledger?kind=gate").json()
    assert body["rows"]
    assert {r["kind"] for r in body["rows"]} == {"gate"}


def test_the_ledger_carries_its_reconciliation_status(client):
    """A mismatch between memory and disk means a torn write, and the
    traceability view must be able to say so rather than imply completeness."""
    run_id = _completed_run(client)
    assert "verify" in client.get(f"/api/runs/{run_id}/ledger").json()


def test_the_run_summary_excludes_the_ledger(client):
    """It is the largest field and the summary is polled far more often."""
    run_id = _completed_run(client)
    assert "ledger" not in client.get(f"/api/runs/{run_id}").json()


def test_the_ledger_of_an_unknown_run_is_404(client):
    assert client.get("/api/runs/nope/ledger").status_code == 404
