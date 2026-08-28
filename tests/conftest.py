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
def _isolated_usage_journal(tmp_path, monkeypatch):
    """Point every BudgetTracker at a throwaway journal."""
    monkeypatch.setenv("SWARMD_USAGE_JOURNAL", str(tmp_path / "usage.jsonl"))
