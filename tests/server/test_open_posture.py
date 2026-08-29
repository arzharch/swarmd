"""Running without user auth, deliberately, and never by accident.

User authentication is out of MVP scope. That is a defensible decision for an
operator tool whose API is not a multi-tenant surface -- and it is a different
thing from an unguarded one. The property these tests hold is narrow and
specific: you can run open, but only by SAYING so, and you are told what you
opened every time you start.

The compensating control in deployment is a default-deny NetworkPolicy, not the
absence of a token. `tests/test_deploy_guards.py` is where that is pinned.
"""

from __future__ import annotations

import pytest

from swarmd.server.middleware import (
    ENV_ALLOW_OPEN,
    ENV_TOKEN,
    InsecureConfiguration,
    exposure_warning,
    require_safe_configuration,
)

PUBLIC = "0.0.0.0"


# --- the accident this prevents ---------------------------------------------


def test_a_public_bind_with_no_token_refuses_to_start(monkeypatch):
    """Fatal at startup, not at first request. A service that only complains
    when someone finds it has already failed."""
    monkeypatch.delenv(ENV_ALLOW_OPEN, raising=False)
    with pytest.raises(InsecureConfiguration, match="refusing to bind"):
        require_safe_configuration(PUBLIC, "")


def test_the_refusal_names_every_way_forward(monkeypatch):
    """An error that says no without saying what to do instead gets worked
    around by the fastest available means, which is usually the worst one."""
    monkeypatch.delenv(ENV_ALLOW_OPEN, raising=False)
    with pytest.raises(InsecureConfiguration) as excinfo:
        require_safe_configuration(PUBLIC, "")
    message = str(excinfo.value)
    assert "127.0.0.1" in message
    assert ENV_TOKEN in message
    assert ENV_ALLOW_OPEN in message


# --- the three legitimate ways to start --------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_needs_nothing(host, monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_OPEN, raising=False)
    require_safe_configuration(host, "")


def test_a_token_permits_a_public_bind(monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_OPEN, raising=False)
    require_safe_configuration(PUBLIC, "a-real-token")


def test_the_env_opt_out_permits_a_public_bind(monkeypatch):
    """What the Kubernetes ConfigMap sets while auth is out of scope."""
    monkeypatch.setenv(ENV_ALLOW_OPEN, "1")
    require_safe_configuration(PUBLIC, "")


def test_the_flag_permits_a_public_bind(monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_OPEN, raising=False)
    require_safe_configuration(PUBLIC, "", allow_open=True)


@pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
def test_a_falsy_opt_out_does_not_count(value, monkeypatch):
    """`SWARMD_ALLOW_OPEN=false` must not read as consent. Anything that
    treats mere presence as true turns a disabled setting into an enabled one.
    """
    monkeypatch.setenv(ENV_ALLOW_OPEN, value)
    with pytest.raises(InsecureConfiguration):
        require_safe_configuration(PUBLIC, "")


# --- being told what you opened ----------------------------------------------


def test_an_open_bind_produces_a_warning():
    warning = exposure_warning(PUBLIC, "")
    assert PUBLIC in warning
    assert "run submission" in warning


def test_a_protected_bind_produces_no_warning():
    assert exposure_warning(PUBLIC, "a-real-token") == ""
    assert exposure_warning("127.0.0.1", "") == ""


def test_the_serve_command_prints_the_warning(capsys, monkeypatch):
    """Printed rather than logged. A warning that only reaches the log is a
    warning that scrolls past during startup."""
    import swarmd.cli as cli

    monkeypatch.setenv(ENV_ALLOW_OPEN, "1")
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.setenv("SWARMD_NO_DOTENV", "1")

    started: dict[str, object] = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: started.update(kw)
    )

    import argparse

    cli._serve_command(
        argparse.Namespace(host=PUBLIC, port=8000, skills=None, allow_open=False)
    )
    out = capsys.readouterr().out
    assert "OPEN" in out
    assert PUBLIC in out
    assert started, "the server never started"
