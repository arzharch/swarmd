"""Learning a limit from a response we already paid for.

Every call comes back carrying how much allowance is left and when it refills.
Throwing that away means waiting for a 429 to learn the same fact -- one
rejected request later, with a backoff attached, and on several providers that
rejection is itself charged to the day that is already spent.
"""

from __future__ import annotations

import pytest

from swarmd.router.budget import (
    LONG_WINDOW_S,
    BudgetSpec,
    BudgetTracker,
    Limit,
    RateHeaders,
    UsageJournal,
    parse_duration,
    parse_rate_headers,
)
from swarmd.router.pool import RateLimited, _retry_after

T0 = 1_700_000_000.0


class FakeResponse:
    """Only the header mapping matters here."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


# --- durations ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7.66", 7.66),          # OpenRouter: a bare number
        ("60", 60.0),            # retry-after: documented as an integer
        ("2m59.56s", 179.56),    # Groq: compound, and the one that used to fail
        ("1h30m", 5400.0),
        ("500ms", 0.5),
        ("0", 0.0),
    ],
)
def test_every_shape_a_provider_sends_is_parsed(raw, expected):
    assert parse_duration(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "   ", "soon", "n/a"])
def test_an_unparseable_duration_is_none_not_an_exception(raw):
    """These are hints on a hot path. A provider that ships a malformed header
    must not be able to fail every call that carries it."""
    assert parse_duration(raw) is None


def test_an_unknown_unit_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="unknown duration unit"):
        parse_duration("5x")


# --- header parsing ----------------------------------------------------------


def test_the_full_header_set_is_read():
    headers = parse_rate_headers(
        {
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "994",
            "x-ratelimit-reset-requests": "2m59.56s",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "7200",
        }
    )
    assert headers.limit_requests == 1000
    assert headers.remaining_requests == 994
    assert headers.reset_requests_s == pytest.approx(179.56)
    assert headers.remaining_tokens == 0
    assert headers.reset_tokens_s == pytest.approx(7200)
    assert headers


def test_a_partial_header_set_yields_what_was_there():
    """Providers implement different subsets of the same de-facto set. A parser
    that required all six would learn nothing from any of them."""
    headers = parse_rate_headers({"x-ratelimit-remaining-requests": "3"})
    assert headers.remaining_requests == 3
    assert headers.limit_requests is None
    assert headers


def test_no_headers_at_all_is_falsy_rather_than_an_error():
    assert not parse_rate_headers({})
    assert not parse_rate_headers(None)


def test_a_header_mapping_that_raises_is_survived():
    class Hostile:
        def get(self, _name):
            raise RuntimeError("not a mapping")

    assert not parse_rate_headers(Hostile())


# --- retry-after -------------------------------------------------------------


def test_groqs_compound_reset_is_understood():
    """The regression this replaced: `float(raw.rstrip("s"))` turned "2m59.56s"
    into "2m59.56" and threw, so the provider that states its reset most
    precisely was the one whose word was discarded for a guessed backoff."""
    resp = FakeResponse({"x-ratelimit-reset-tokens": "2m59.56s"})
    assert _retry_after(resp) == pytest.approx(179.56)


def test_an_explicit_retry_after_wins():
    resp = FakeResponse(
        {"retry-after": "30", "x-ratelimit-reset-tokens": "2m59.56s"}
    )
    assert _retry_after(resp) == pytest.approx(30)


def test_the_exhausted_dimension_decides_when_no_retry_after_is_given():
    """A dimension with headroom left says nothing about when the one that ran
    out comes back."""
    resp = FakeResponse(
        {
            "x-ratelimit-remaining-requests": "900",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "2h",
        }
    )
    assert _retry_after(resp) == pytest.approx(7200)


def test_no_usable_header_returns_none_so_the_caller_backs_off():
    assert _retry_after(FakeResponse({})) is None


# --- daily versus per-minute -------------------------------------------------


def test_a_long_wait_is_read_as_the_day_being_spent():
    """Backing a provider off for four hours over a one-minute bucket throws
    away most of a day; retrying a spent day every two seconds earns a
    rejection per retry."""
    assert RateLimited("p", LONG_WINDOW_S + 1).daily


def test_a_short_wait_is_the_minute_bucket():
    assert not RateLimited("p", 30).daily


def test_a_429_with_no_stated_wait_is_not_assumed_to_be_daily():
    assert not RateLimited("p", None).daily


# --- acting on what a success said ------------------------------------------


def tracker(tmp_path) -> BudgetTracker:
    return BudgetTracker(
        journal=UsageJournal(str(tmp_path / "usage.jsonl")),
        budgets={
            "p": BudgetSpec(
                provider="p",
                kind="quota",
                limits=(Limit("day", requests=1000, tokens=200_000),),
                reset="rolling",
                source="test",
                checked="test",
            )
        },
    )


def test_a_success_saying_the_day_is_gone_blocks_the_provider(tmp_path):
    """The whole point: stop BEFORE the 429, not one rejection after it."""
    track = tracker(tmp_path)
    until = track.observe_headers(
        RateHeaders(remaining_tokens=0, reset_tokens_s=7200),
        provider="p", credential="p#0", now=T0,
    )
    assert until == pytest.approx(T0 + 7200)
    assert track.blocked("p", now=T0)


def test_a_success_with_headroom_left_changes_nothing(tmp_path):
    track = tracker(tmp_path)
    assert track.observe_headers(
        RateHeaders(remaining_tokens=500, reset_tokens_s=7200),
        provider="p", credential="p#0", now=T0,
    ) == 0.0
    assert not track.blocked("p", now=T0)


def test_an_empty_minute_bucket_is_not_mistaken_for_a_spent_day(tmp_path):
    """A near reset is the per-minute bucket, which quota.py already paces.
    Journalling it as a day would park a run for hours over something that
    refills in seconds."""
    track = tracker(tmp_path)
    assert track.observe_headers(
        RateHeaders(remaining_tokens=0, reset_tokens_s=30),
        provider="p", credential="p#0", now=T0,
    ) == 0.0
    assert not track.blocked("p", now=T0)


def test_the_furthest_exhausted_dimension_wins(tmp_path):
    track = tracker(tmp_path)
    until = track.observe_headers(
        RateHeaders(
            remaining_requests=0, reset_requests_s=3700,
            remaining_tokens=0, reset_tokens_s=7200,
        ),
        provider="p", credential="p#0", now=T0,
    )
    assert until == pytest.approx(T0 + 7200)


def test_the_observation_outranks_the_declared_table(tmp_path):
    """ADR-008. The table is a hypothesis; the account is the fact."""
    track = tracker(tmp_path)
    assert not track.blocked("p", now=T0)
    track.observe_headers(
        RateHeaders(remaining_requests=0, reset_requests_s=7200),
        provider="p", credential="p#0", now=T0,
    )
    assert "day spent" in track.blocked("p", now=T0)
    # And it lapses at the instant the provider named, not before or forever.
    assert not track.blocked("p", now=T0 + 7201)
