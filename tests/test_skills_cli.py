"""`swarmd skills merge`.

A library written before ADR-014 holds one record per PHRASING: the instruction
is written by a model, so the same approach distilled from two runs never
hashed the same, and each copy carried evidence from a single task shape. That
is why `promotable` -- two distinct shapes -- was unreachable, why nothing was
ever queued for review, and why the treatment arm of every ablation had an
empty library to retrieve from.

New proposals now land on the existing record. This is how a library written
before that gets the same treatment without re-running the sessions that paid
for it.
"""

from __future__ import annotations

import json

from swarmd.cli import main
from swarmd.swarm.skills import SkillLibrary, make_skill_id


def _write_pre_merge_library(path):
    """A library file as the old code would have written one: two records for
    one approach, one evidence shape each."""
    def _record(instruction, pattern, shape):
        # The id is the real content hash: the loader verifies it, so a
        # hand-written stand-in would be rejected as an edited file rather
        # than read as the old code's output.
        name = "approach: produce diagnosis, fix"
        return {
            "skill_id": make_skill_id(name, instruction),
            "name": name,
            "task_pattern": pattern,
            "instruction": instruction,
            "evidence_tasks": [shape],
        }

    records = [
        _record(
            "Name the failing mechanism, then the smallest edit.",
            "determine why the slot_term failed json_parses contains_all",
            "shape-permissions",
        ),
        _record(
            "State the mechanism that fails, then the minimal edit.",
            "determine why the slot_term broke json_parses contains_all",
            "shape-timezones",
        ),
    ]
    path.write_text(json.dumps({"skills": records}), encoding="utf-8")
    return path


def test_merge_reports_what_would_collapse_and_writes_nothing(tmp_path, capsys):
    """A library is evidence. A migration that runs before anyone has looked at
    it is a migration nobody checked, so the default changes no file."""
    path = _write_pre_merge_library(tmp_path / "skills.json")
    before = path.read_text(encoding="utf-8")

    assert main(["skills", "merge", "--skills", str(path)]) == 0

    out = capsys.readouterr().out
    assert "records in     : 2" in out
    assert "approaches out : 1" in out
    assert "promotable     : 1" in out
    assert "nothing written" in out
    assert path.read_text(encoding="utf-8") == before


def test_merge_applied_collapses_the_copies_and_reaches_the_bar(tmp_path, capsys):
    """The point of the exercise: one record carrying both shapes is
    promotable, and two records carrying one each never were."""
    path = _write_pre_merge_library(tmp_path / "skills.json")

    assert main(["skills", "merge", "--skills", str(path), "--apply"]) == 0

    merged = SkillLibrary(str(path)).all()
    assert len(merged) == 1
    assert set(merged[0].evidence_tasks) == {"shape-permissions", "shape-timezones"}
    assert merged[0].promotable
    assert "written:" in capsys.readouterr().out


def test_merge_in_place_leaves_the_original_beside_it(tmp_path):
    """Overwriting the only copy of a library that took provider quota to build
    is not a thing to do without a way back."""
    path = _write_pre_merge_library(tmp_path / "skills.json")

    main(["skills", "merge", "--skills", str(path), "--apply"])

    backup = tmp_path / "skills.json.pre-merge"
    assert backup.exists()
    assert len(json.loads(backup.read_text(encoding="utf-8"))["skills"]) == 2


def test_merge_to_a_new_path_leaves_the_input_alone(tmp_path):
    path = _write_pre_merge_library(tmp_path / "skills.json")
    before = path.read_text(encoding="utf-8")
    out = tmp_path / "merged.json"

    main(["skills", "merge", "--skills", str(path), "--out", str(out), "--apply"])

    assert path.read_text(encoding="utf-8") == before
    assert len(SkillLibrary(str(out)).all()) == 1


def test_merge_says_so_on_an_empty_library(tmp_path, capsys):
    path = tmp_path / "skills.json"
    SkillLibrary(str(path))
    assert main(["skills", "merge", "--skills", str(path)]) == 0
    assert "empty" in capsys.readouterr().out
