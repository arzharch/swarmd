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


def test_merge_does_not_silently_un_approve_the_library(tmp_path):
    """Found by running it on a real library: two skills approved on their own
    evidence came back pending, and their use counts came back zero.

    `propose` mints CANDIDATES, so a replay that only proposes destroys exactly
    what the migration was meant to preserve -- the reviews, and the history
    that pruning reads. Approval is matched on `skill_id`, the hash of the
    surviving instruction, so the decision follows the text a human looked at.
    """
    path = _write_pre_merge_library(tmp_path / "skills.json")

    # Approve one of the two phrasings and give it a use record, the way a
    # reviewed and exercised library looks.
    library = SkillLibrary(str(path))
    first = library.all()[0]
    library.approve(first.skill_id, actor="reviewer", force=True)
    library.record_use(first.skill_id, success=True)

    main(["skills", "merge", "--skills", str(path), "--apply"])

    merged = SkillLibrary(str(path)).all()
    assert len(merged) == 1
    assert merged[0].approved, "the approval survived the merge"
    assert merged[0].approved_by == "reviewer"
    assert merged[0].uses == 1, "and so did the history pruning reads"
    assert merged[0].successes == 1


def test_merge_keeps_a_pruning_verdict(tmp_path, capsys):
    """The one that cost 53 retrievals to learn and was erased twice.

    A skill retrieved during training and pruned for failing carries a verdict
    the economy paid for. The merge dropped every retired record outright, so
    the next session re-proposed the approach as new and it went round again.
    """
    path = _write_pre_merge_library(tmp_path / "skills.json")
    library = SkillLibrary(str(path))
    doomed = library.all()[0]
    for _ in range(6):
        library.record_use(doomed.skill_id, success=False)
    library.reject(doomed.skill_id, actor="consolidator", reason="pruned: 0/6")

    main(["skills", "merge", "--skills", str(path), "--apply"])

    merged = SkillLibrary(str(path)).all()
    assert len(merged) == 1
    assert merged[0].retired, "a rejection of one phrasing rejects the approach"
    assert "pruned" in merged[0].retired_reason
    assert merged[0].uses >= 6, "and the record of how it performed survives"


def test_merge_says_when_an_approval_could_not_be_carried(tmp_path, capsys):
    """Approval is granted for specific text. When a different phrasing of the
    same approach survives, there is nothing a human read to carry it to -- so
    it is dropped and said out loud rather than transferred silently."""
    path = _write_pre_merge_library(tmp_path / "skills.json")
    library = SkillLibrary(str(path))
    # Approve the SECOND phrasing; the first is the one the merge keeps.
    second = library.all()[1]
    library.approve(second.skill_id, actor="reviewer", force=True)

    main(["skills", "merge", "--skills", str(path), "--apply"])

    out = capsys.readouterr().out
    assert "approvals LOST : 1" in out
    assert not SkillLibrary(str(path)).all()[0].approved
