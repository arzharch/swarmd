"""Two processes share one usage journal, because in practice they do.

`swarmd serve` runs for hours. Somebody runs the CLI beside it. Both append to
`.swarmd/usage.jsonl`, and both ration against what they read from it. The
journal is the only thing that makes a daily budget survive a restart, so a
reader that never notices another writer is a budget that quietly doubles.

These are single-process tests using two `UsageJournal` objects over one file,
which is the same sharing without the scheduling noise. Cross-REPLICA
correctness still needs `RedisRation`: a stat cannot make read-then-write
atomic. What it can do is stop a long-lived reader from being wrong until it
restarts.
"""

from __future__ import annotations

import json

from swarmd.router.budget import (
    MONTH,
    BudgetSpec,
    BudgetTracker,
    Limit,
    UsageJournal,
)

T0 = 1_700_000_000.0


def spec() -> BudgetSpec:
    return BudgetSpec(
        provider="p",
        kind="quota",
        limits=(Limit("day", requests=1000),),
        reset="rolling",
        source="test",
        checked="test",
    )


def test_a_reader_sees_another_writers_spend(tmp_path):
    """The bug this closes: the cache was populated once and never revisited,
    so a server that started this morning rationed against this morning."""
    path = tmp_path / "usage.jsonl"
    reader, writer = UsageJournal(path), UsageJournal(path)

    reader.load()  # populate the cache before anything is written
    assert reader.rows_for(provider="p", since=0) == []

    for _ in range(5):
        writer.record(provider="p", credential="c", requests=1, ts=T0, kind="ok")

    assert len(reader.rows_for(provider="p", since=0)) == 5


def test_a_reader_does_not_re_read_when_nothing_changed(tmp_path):
    """The staleness check costs a stat per read on a hot path, so it has to
    actually avoid the reparse when the file is untouched."""
    path = tmp_path / "usage.jsonl"
    journal = UsageJournal(path)
    journal.record(provider="p", credential="c", requests=1, ts=T0, kind="ok")

    first = journal.load()
    assert journal.load() is first, "the row list was rebuilt for no reason"


def test_a_writers_own_append_is_visible_without_a_reload(tmp_path):
    path = tmp_path / "usage.jsonl"
    journal = UsageJournal(path)
    journal.record(provider="p", credential="c", requests=1, ts=T0, kind="ok")
    assert len(journal.rows_for(provider="p", since=0)) == 1


def test_compaction_does_not_delete_another_writers_rows(tmp_path):
    """Compaction rewrites the whole file from one process's view. Doing that
    from a STALE view deletes everything the other process appended since --
    data loss dressed as maintenance, and undetectable afterwards.
    """
    path = tmp_path / "usage.jsonl"
    old, fresh = UsageJournal(path), UsageJournal(path)

    # An ancient row, so compaction has something to drop and will rewrite.
    old.record(provider="p", credential="c", requests=1, ts=T0 - MONTH * 2, kind="ok")
    old.load()

    # Another process appends after `old` has taken its snapshot.
    for _ in range(3):
        fresh.record(provider="p", credential="c", requests=1, ts=T0, kind="ok")

    old.compact(now=T0)

    survivors = json.loads(
        "[" + ",".join(
            line for line in path.read_text(encoding="utf-8").splitlines() if line
        ) + "]"
    )
    assert len(survivors) == 3, "compaction destroyed the other writer's rows"


def test_two_trackers_over_one_file_agree_on_what_is_left(tmp_path):
    """The number that decides whether a run may start must not depend on
    which process is asked."""
    path = tmp_path / "usage.jsonl"
    a = BudgetTracker(journal=UsageJournal(path), budgets={"p": spec()})
    b = BudgetTracker(journal=UsageJournal(path), budgets={"p": spec()})

    a.window_state("p", "day", now=T0)  # warm A's cache
    for _ in range(40):
        b.record(provider="p", credential="c", requests=1, ts=T0, kind="ok")

    assert a.window_state("p", "day", now=T0).used_requests == 40
