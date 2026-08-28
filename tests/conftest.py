"""Test-wide isolation from the operator's real state.

The usage journal is deliberately durable and machine-wide: a monthly budget
has to survive restarts and span every run on the box. That is exactly what
makes it dangerous in a test suite -- a `ProviderPool` built with defaults
writes to the real `.swarmd/usage.jsonl`, so running the tests silently spends
budget the operator is planning against.

It happened. A test run put 18 groq and 18 cerebras requests into the live
journal, and `swarmd providers budget` reported them as consumed quota. The
numbers were wrong in the direction that makes you stop early, which is the
better direction and still wrong.

Autouse and session-scoped, so no test can opt out by forgetting.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_durable_state(tmp_path, monkeypatch):
    """Point every durable store at a throwaway directory.

    The usage journal came first; the run store joined it for the same reason
    and by the same route -- `SwarmRun` gained a default `RunStore()` and the
    suite immediately started writing real run documents into the operator's
    `.swarmd/runs/`. Durable-by-default is right for the product and wrong for
    a test process, so every such path is redirected here rather than in each
    test that happens to remember.
    """
    monkeypatch.setenv("SWARMD_USAGE_JOURNAL", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("SWARMD_RUN_STORE", str(tmp_path / "runs"))
