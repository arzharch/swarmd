"""Edge hardening tests.

swarmd has no user auth by design (ADR-013): it is single-tenant and
operator-run. That is only defensible if the compensating controls actually
work, so they are tested rather than described.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from swarmd.observability.logs import JsonFormatter, PlainFormatter, redact
from swarmd.server.app import create_app
from swarmd.server.middleware import (
    MAX_BODY_BYTES,
    InsecureConfiguration,
    RateLimit,
    extract_token,
    require_safe_configuration,
    token_matches,
)
from tests.server.test_app import FakeProvider

TOKEN = "operator-token-value"


@pytest.fixture
def secured(tmp_path):
    app = create_app(
        provider_factory=FakeProvider,
        skills_path=str(tmp_path / "skills.json"),
        api_token=TOKEN,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def open_client(tmp_path):
    app = create_app(
        provider_factory=FakeProvider,
        skills_path=str(tmp_path / "skills.json"),
        api_token="",
    )
    with TestClient(app) as client:
        yield client


# --- refusing to be wide open ----------------------------------------------


def test_binding_off_host_without_a_token_refuses_to_start():
    """Failing at startup, before exposure, is the whole value of the check."""
    with pytest.raises(InsecureConfiguration, match="SWARMD_API_TOKEN"):
        require_safe_configuration("0.0.0.0", None)


def test_loopback_without_a_token_is_allowed():
    """`swarmd serve` on a laptop must stay frictionless."""
    require_safe_configuration("127.0.0.1", None)
    require_safe_configuration("localhost", "")


def test_binding_off_host_with_a_token_is_allowed():
    require_safe_configuration("0.0.0.0", TOKEN)


# --- the operator token ----------------------------------------------------


def test_starting_a_run_without_the_token_is_refused(secured):
    """The endpoint that spends money is the one that must be gated."""
    response = secured.post("/api/runs", json={"task": "t", "profile": "smoke"})
    assert response.status_code == 401
    assert response.json()["error"] == "operator token required"


def test_starting_a_run_with_the_token_is_accepted(secured):
    response = secured.post(
        "/api/runs",
        json={"task": "summarise the records", "profile": "smoke", "chaos": False},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 202


def test_the_alternate_header_is_accepted(secured):
    """A browser cannot set Authorization on a websocket handshake."""
    response = secured.post(
        "/api/runs",
        json={"task": "t", "profile": "smoke", "chaos": False},
        headers={"X-Swarmd-Token": TOKEN},
    )
    assert response.status_code == 202


def test_a_wrong_token_is_refused(secured):
    response = secured.post(
        "/api/runs", json={"task": "t"}, headers={"X-Swarmd-Token": "wrong"}
    )
    assert response.status_code == 401


def test_approving_a_skill_requires_the_token(secured):
    """A human decision recorded by an anonymous caller is not a human decision."""
    assert secured.post("/api/skills/abc/approve").status_code == 401


def test_probes_stay_open_so_kubernetes_needs_no_secret(secured):
    """Giving every scraper the token that can also start runs is too wide."""
    assert secured.get("/healthz").status_code == 200
    assert secured.get("/readyz").status_code == 200
    assert secured.get("/metrics").status_code == 200


def test_reads_stay_open_behind_the_ingress_allowlist(secured):
    assert secured.get("/api/runs").status_code == 200
    assert secured.get("/api/events").status_code == 200


def test_token_comparison_is_constant_time():
    """`==` on a secret leaks length and prefix through timing."""
    import inspect

    from swarmd.server import middleware

    assert "compare_digest" in inspect.getsource(middleware.token_matches)


def test_token_matching_rejects_empty_and_none():
    assert not token_matches(None, TOKEN)
    assert not token_matches("", TOKEN)
    assert token_matches(f"  {TOKEN}  ", TOKEN)   # whitespace tolerated


def test_bearer_and_plain_headers_both_parse():
    assert extract_token({"authorization": f"Bearer {TOKEN}"}) == TOKEN
    assert extract_token({"x-swarmd-token": TOKEN}) == TOKEN
    assert extract_token({}) is None


# --- websocket gate --------------------------------------------------------


def test_the_event_stream_requires_the_token(tmp_path):
    """The stream carries every prompt, thought and artifact a run produces."""
    app = create_app(provider_factory=FakeProvider, api_token=TOKEN,
                     skills_path=str(tmp_path / "s.json"))
    with TestClient(app) as client:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/stream"):
            pass


def test_the_event_stream_accepts_a_query_parameter_token(tmp_path):
    app = create_app(provider_factory=FakeProvider, api_token=TOKEN,
                     skills_path=str(tmp_path / "s.json"))
    with TestClient(app) as client, client.websocket_connect(
        f"/api/stream?token={TOKEN}"
    ):
        pass  # handshake accepted


def test_an_unsecured_stream_stays_open(open_client):
    with open_client.websocket_connect("/api/stream"):
        pass


# --- rate limiting ---------------------------------------------------------


def test_the_rate_limit_stops_a_runaway_caller():
    """A quota defence: a loop can burn a day's free tier in a minute."""
    limiter = RateLimit(max_requests=3, window_s=60)
    assert all(limiter.check("1.2.3.4", now=100.0)[0] for _ in range(3))
    allowed, retry_after = limiter.check("1.2.3.4", now=100.0)
    assert not allowed
    assert retry_after > 0


def test_the_window_slides_rather_than_resetting():
    """A fixed window permits twice the rate across its boundary."""
    limiter = RateLimit(max_requests=2, window_s=60)
    limiter.check("c", now=0.0)
    limiter.check("c", now=59.0)
    assert not limiter.check("c", now=59.5)[0]
    assert limiter.check("c", now=61.0)[0]      # the first has aged out


def test_clients_are_limited_independently():
    limiter = RateLimit(max_requests=1, window_s=60)
    assert limiter.check("a", now=0.0)[0]
    assert limiter.check("b", now=0.0)[0]
    assert not limiter.check("a", now=0.0)[0]


def test_idle_clients_are_pruned():
    """An unbounded client table is a slow memory leak."""
    limiter = RateLimit(max_requests=5, window_s=60)
    limiter.check("gone", now=0.0)
    limiter.prune(now=200.0)
    assert limiter._hits == {}


def test_the_api_enforces_the_rate_limit(tmp_path):
    app = create_app(provider_factory=FakeProvider, api_token="",
                     skills_path=str(tmp_path / "s.json"))
    from swarmd.server import middleware

    middleware.install(app, token="", rate_limit=RateLimit(max_requests=2))
    with TestClient(app) as client:
        payload = {"task": "t", "profile": "smoke", "chaos": False}
        codes = [client.post("/api/runs", json=payload).status_code for _ in range(6)]
        assert 429 in codes
        assert "Retry-After" in client.post("/api/runs", json=payload).headers


# --- body size -------------------------------------------------------------


def test_an_oversized_body_is_refused_before_parsing(open_client):
    """Accepting 50MB to then reject it is memory amplification with no upside."""
    response = open_client.post(
        "/api/runs",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


# --- response hygiene ------------------------------------------------------


def test_security_headers_are_present(open_client):
    headers = open_client.get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_every_response_carries_a_request_id(open_client):
    """An incident needs something to reconstruct from."""
    assert open_client.get("/healthz").headers["X-Request-Id"]


def test_a_supplied_request_id_is_preserved(open_client):
    response = open_client.get("/healthz", headers={"X-Request-Id": "trace-me"})
    assert response.headers["X-Request-Id"] == "trace-me"


def test_interactive_docs_are_disabled_outside_dev(tmp_path, monkeypatch):
    """Docs are a live client for every endpoint, served without the token."""
    monkeypatch.setenv("SWARMD_ENV", "prod")
    app = create_app(provider_factory=FakeProvider, api_token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


# --- log redaction ---------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "failed with key gsk_abcdefghijklmnop12345",
        "Authorization: Bearer abcdefghijklmnop",
        "postgres://swarmd:supersecret@db:5432/swarmd",
        "redis://user:hunter2hunter2@cache:6379/0",
    ],
)
def test_credentials_are_redacted_from_log_text(text):
    """Exception messages and request echoes are where keys escape."""
    cleaned = redact(text)
    for secret in ("gsk_abcdefghijklmnop12345", "abcdefghijklmnop",
                   "supersecret", "hunter2hunter2"):
        assert secret not in cleaned


def test_the_json_formatter_promotes_extras_to_fields():
    """`status:500` as a query, not a substring search."""
    record = logging.LogRecord(
        "swarmd", logging.INFO, __file__, 1, "request", None, None
    )
    record.status = 500
    record.duration_ms = 12.5
    payload = json.loads(JsonFormatter().format(record))
    assert payload["status"] == 500
    assert payload["duration_ms"] == 12.5
    assert payload["message"] == "request"


def test_the_json_formatter_redacts_sensitive_field_names():
    record = logging.LogRecord(
        "swarmd", logging.INFO, __file__, 1, "config", None, None
    )
    record.api_key = "gsk_live_value_here"
    record.database_url = "postgres://u:p@h/db"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["api_key"] == "<redacted>"
    assert payload["database_url"] == "<redacted>"


def test_the_json_formatter_never_raises_on_odd_objects():
    """Losing the line you needed to debug the thing that produced it is bad."""
    record = logging.LogRecord(
        "swarmd", logging.INFO, __file__, 1, "odd", None, None
    )
    record.weird = object()
    json.loads(JsonFormatter().format(record))


def test_plain_logs_are_redacted_too():
    """Redaction is not a production-only concern."""
    record = logging.LogRecord(
        "swarmd", logging.INFO, __file__, 1,
        "connecting to postgres://u:secretpw@h/db", None, None,
    )
    assert "secretpw" not in PlainFormatter().format(record)
