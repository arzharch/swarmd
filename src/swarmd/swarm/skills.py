"""Skill library: what "learning" means here, stated narrowly.

Learning in this system is retrieval plus prompt consolidation. **No weights
are updated.** Calling that "training" would be the exact slippage the ledger
and the control arm exist to prevent, so it is labelled precisely everywhere.

The loop:

  DISTILL   A verified success becomes a candidate skill: what worked, on what
            kind of task, with what evidence.
  APPROVE   A HUMAN decides whether it enters the library. This is the most
            important gate in the system and the least obvious one -- a skill
            is inherited by every future run, so an unreviewed library poisons
            forward rather than failing locally.
  RETRIEVE  Later tasks find matching skills and start from them.
  SCORE     Every use updates the skill's record. A skill that stops working
            loses standing without anyone intervening.
  PRUNE     Consolidation retires skills that no longer earn their place.

WHY RETRIEVAL IS LEXICAL, not embedding-based. An embedding call per retrieval
costs provider quota, which is the binding constraint (docs/CAPACITY.md), on an
operation that happens many times per run. Token-overlap scoring with IDF
weighting is free, deterministic -- so chaos-integrity hashes stay comparable
-- and explainable, which matters when a human is reviewing why a skill was
suggested. The trade is real: genuinely synonymous phrasings will miss.
Embeddings become worth it when a measured miss rate says so, not before.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

_TOKEN = re.compile(r"[a-z0-9_]+")

# Words carrying no discriminative signal for task matching. Deliberately tiny:
# an aggressive stoplist throws away the domain vocabulary that retrieval
# depends on.
STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "of", "to", "in", "for", "on", "with", "is", "are", "be", "by", "from", "that", "this", "it", "as", "at"]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


@dataclass(slots=True)
class Skill:
    """A reusable approach, with the evidence that it worked.

    `provenance` is not decoration. When a skill turns out to be harmful, the
    question is immediately "what else came from that run", and without a chain
    back to the originating run and criterion hash the answer is unavailable.
    """

    skill_id: str
    name: str
    task_pattern: str          # the kind of task this applies to
    instruction: str           # what to do, injected into a worker's prompt
    provenance_run: str = ""
    provenance_criterion: str = ""
    created_ts: float = field(default_factory=time.time)
    approved: bool = False
    approved_by: str = ""
    uses: int = 0
    successes: int = 0
    retired: bool = False
    retired_reason: str = ""

    @property
    def success_rate(self) -> float:
        """Unused skills report 0.0, not 1.0.

        An optimistic default would make a brand-new skill outrank a proven one
        on its first retrieval, which is how an unvalidated approach spreads
        through a population before anyone notices.
        """
        return self.successes / self.uses if self.uses else 0.0

    @property
    def usable(self) -> bool:
        return self.approved and not self.retired

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_skill_id(name: str, instruction: str) -> str:
    """Content-addressed, so the same skill proposed twice is one skill."""
    payload = f"{name.strip().lower()}|{instruction.strip()}"
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# A distilled instruction has to be an INSTRUCTION. The distiller reads model
# output, and model output is not trustworthy input just because this system
# produced it -- the library once filled with entries whose "instruction" was a
# JSON blob of a previous run's OUTPUT summary, which taught nothing and cost a
# retrieval slot every run.
# Low deliberately. The failure that actually happened was serialised OUTPUT,
# not brevity, and real distilled instructions are legitimately terse -- "use
# csv.DictReader with an explicit dialect" is a good skill. This floor only
# rejects the degenerate case; hallucinated-but-plausible content is what the
# human approval gate and success-rate pruning are for.
MIN_INSTRUCTION_CHARS = 8
MAX_INSTRUCTION_CHARS = 2_000
MAX_NAME_CHARS = 120


def validate_instruction(instruction: str) -> str:
    """Reject what cannot be a reusable instruction. Raises, never repairs.

    Repairing would be worse: a silently rewritten skill is one nobody
    reviewed, and the human approval gate is the only thing standing between a
    hallucinated approach and every future run's prompt.
    """
    text = (instruction or "").strip()
    if not text:
        raise SkillLibraryError("a skill with no instruction teaches nothing")
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        # THE POISONING THAT HAPPENED. Distillation stored the longest OUTPUT
        # as the instruction, so the library filled with serialised artifacts
        # describing one run's answer rather than any reusable approach.
        raise SkillLibraryError(
            "instruction looks like serialised output, not an approach; "
            "distillation must record the SHAPE of what worked, not the answer"
        )
    if len(text) < MIN_INSTRUCTION_CHARS:
        raise SkillLibraryError(
            f"instruction is {len(text)} chars; too short to be an approach"
        )
    if len(text) > MAX_INSTRUCTION_CHARS:
        # An unbounded instruction is a prompt-budget leak: it is injected into
        # every worker prompt that retrieves it.
        raise SkillLibraryError(
            f"instruction is {len(text)} chars, over the {MAX_INSTRUCTION_CHARS} "
            f"limit; it would be injected into every retrieving worker's prompt"
        )
    return text


class SkillLibraryError(RuntimeError):
    pass


class SkillLibrary:
    """Durable, human-gated store of learned approaches.

    JSON-file backed for the same reason the ledger is JSONL: a library small
    enough to read is a library a reviewer will actually read, and the human
    approval gate only means something if the human can see what they are
    approving.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._skills: dict[str, Skill] = {}
        if self.path and self.path.exists():
            self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        assert self.path is not None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillLibraryError(f"library at {self.path} is corrupt: {exc}") from exc
        known = {f.name for f in fields(Skill)}
        for index, entry in enumerate(payload.get("skills", [])):
            if not isinstance(entry, dict):
                raise SkillLibraryError(
                    f"library at {self.path}: entry {index} is not an object"
                )
            unknown = set(entry) - known
            if unknown:
                # A raw TypeError from Skill(**entry) took the whole run down
                # over a file this module already tries hard to degrade around.
                raise SkillLibraryError(
                    f"library at {self.path}: entry {index} has unknown "
                    f"field(s) {sorted(unknown)}; written by a different build"
                )
            try:
                skill = Skill(**entry)
            except TypeError as exc:
                raise SkillLibraryError(
                    f"library at {self.path}: entry {index} is malformed: {exc}"
                ) from exc

            # THE ID IS THE INTEGRITY CHECK, and it was never checked.
            # `skill_id` is a content hash of name+instruction, so verifying it
            # on load is what makes editing the file detectable. Without this a
            # skills.json could carry any instruction under any id -- including
            # `approved: true`, which is the entire human gate expressed as a
            # boolean in a file anyone who can reach the disk can write.
            expected = make_skill_id(skill.name, skill.instruction)
            if skill.skill_id != expected:
                raise SkillLibraryError(
                    f"library at {self.path}: entry {index} ({skill.name!r}) "
                    f"has id {skill.skill_id} but its contents hash to "
                    f"{expected}; the file has been edited since it was written"
                )
            self._skills[skill.skill_id] = skill

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write must not leave a truncated
        # library, because the failure mode is silently losing every skill.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"skills": [s.to_dict() for s in self._skills.values()]}, indent=2
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # -- proposal and approval ---------------------------------------------

    def propose(
        self,
        *,
        name: str,
        task_pattern: str,
        instruction: str,
        run_id: str = "",
        criterion_hash: str = "",
    ) -> Skill:
        """Record a candidate. It is NOT usable until a human approves it."""
        validate_instruction(instruction)
        skill_id = make_skill_id(name, instruction)
        existing = self._skills.get(skill_id)
        if existing is not None:
            return existing
        skill = Skill(
            skill_id=skill_id,
            name=name.strip(),
            task_pattern=task_pattern.strip(),
            instruction=instruction.strip(),
            provenance_run=run_id,
            provenance_criterion=criterion_hash,
        )
        self._skills[skill_id] = skill
        self.save()
        return skill

    def approve(self, skill_id: str, *, actor: str) -> Skill:
        skill = self._require(skill_id)
        skill.approved = True
        skill.approved_by = actor
        self.save()
        return skill

    def reject(self, skill_id: str, *, actor: str, reason: str = "") -> Skill:
        """Rejection retires rather than deletes.

        Deleting would let the same proposal reappear next run and be reviewed
        again forever. A retired record is a decision that persists.
        """
        skill = self._require(skill_id)
        skill.approved = False
        skill.retired = True
        skill.retired_reason = reason or f"rejected by {actor}"
        self.save()
        return skill

    def pending(self) -> list[Skill]:
        return [s for s in self._skills.values() if not s.approved and not s.retired]

    def approved(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.usable]

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def _require(self, skill_id: str) -> Skill:
        skill = self._skills.get(skill_id)
        if skill is None:
            raise SkillLibraryError(f"unknown skill: {skill_id}")
        return skill

    # -- retrieval ----------------------------------------------------------

    def _idf(self) -> dict[str, float]:
        """Inverse document frequency across approved skills.

        Without it, a term appearing in every skill contributes as much as a
        distinctive one, and retrieval degenerates to "whichever skill has the
        most words".
        """
        usable = self.approved()
        if not usable:
            return {}
        counts: Counter[str] = Counter()
        for skill in usable:
            counts.update(set(tokenize(f"{skill.task_pattern} {skill.name}")))
        total = len(usable)
        return {
            term: math.log((total + 1) / (count + 1)) + 1.0
            for term, count in counts.items()
        }

    def retrieve(self, task: str, *, limit: int = 3, min_score: float = 0.15) -> list[Skill]:
        """Find skills plausibly applicable to a task.

        ANATOMY: min_score
          Similarity floor below which a skill is not offered. Why 0.15: low
          enough that a genuinely related skill with different phrasing still
          surfaces, high enough that an unrelated one does not. Zero would
          attach an arbitrary skill to every task, which is worse than none --
          a wrong skill actively misleads a worker, while no skill just leaves
          it to reason from the task.

        ANATOMY: limit
          Why 3: skills are injected into a worker prompt, and prompt length is
          token cost on a quota-bound system. Three gives the worker options
          without crowding out the task itself.
        """
        usable = self.approved()
        if not usable:
            return []

        idf = self._idf()
        query = set(tokenize(task))
        if not query:
            return []

        scored: list[tuple[float, Skill]] = []
        for skill in usable:
            terms = set(tokenize(f"{skill.task_pattern} {skill.name}"))
            if not terms:
                continue
            overlap = query & terms
            if not overlap:
                continue
            weight = sum(idf.get(t, 1.0) for t in overlap)
            denominator = math.sqrt(len(query) * len(terms)) or 1.0
            similarity = weight / denominator
            # Proven skills outrank speculative ones at equal similarity, but
            # the bonus is deliberately small: a strong topical match with no
            # track record should still beat a weak match that once worked.
            score = similarity * (1.0 + 0.3 * skill.success_rate)
            if score >= min_score:
                scored.append((score, skill))

        scored.sort(key=lambda pair: (-pair[0], pair[1].skill_id))
        return [skill for _, skill in scored[:limit]]

    # -- outcomes -----------------------------------------------------------

    def record_use(self, skill_id: str, *, success: bool) -> None:
        """Update a skill's record. A skill that stops working loses standing."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return
        skill.uses += 1
        if success:
            skill.successes += 1
        self.save()

    def prune(self, *, min_uses: int = 5, min_success_rate: float = 0.3) -> list[Skill]:
        """Retire skills with a demonstrated poor record.

        ANATOMY: min_uses
          Evidence required before retiring. Why 5: a skill that failed twice
          may have met two hard tasks. Retiring on thin evidence throws away
          approaches that were fine, and the human already paid attention to
          approve them.

        ANATOMY: min_success_rate
          Why 0.3: well below the level at which a skill is useful, so only
          clearly-harmful entries are retired. Pruning aggressively would shrink
          the library toward whatever the current task distribution favours,
          which is overfitting by another name.
        """
        retired = []
        for skill in self.approved():
            if skill.uses >= min_uses and skill.success_rate < min_success_rate:
                skill.retired = True
                skill.retired_reason = (
                    f"pruned: {skill.successes}/{skill.uses} successes "
                    f"({skill.success_rate:.0%}) below {min_success_rate:.0%}"
                )
                retired.append(skill)
        if retired:
            self.save()
        return retired

    def stats(self) -> dict[str, Any]:
        skills = list(self._skills.values())
        usable = [s for s in skills if s.usable]
        used = [s for s in usable if s.uses]
        return {
            "total": len(skills),
            "approved": len(usable),
            "pending": len(self.pending()),
            "retired": sum(1 for s in skills if s.retired),
            "total_uses": sum(s.uses for s in skills),
            "mean_success_rate": (
                round(sum(s.success_rate for s in used) / len(used), 4) if used else 0.0
            ),
        }
