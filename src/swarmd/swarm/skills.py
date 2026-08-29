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

WHY RETRIEVAL MATCHES ON A SHAPE, not on the task text. A skill used to be
indexed by the raw task it came from, so it was offered to a later task because
that task LOOKED LIKE its source -- memorisation through the index rather than
through the advice. Both sides of the comparison now go through
`generalise.abstract` first: `"3 pens at 1.25 each"` and `"7 pencils at 40c
each"` are both `slot_number ... at slot_money each`, so what matches is the
kind of question, and a stored literal cannot be what attracts a hit.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from swarmd.swarm.generalise import (
    METHOD_LEXICON,
    abstract,
    render_pattern,
    shared_literals,
)

_TOKEN = re.compile(r"[a-z0-9_]+")

# Words carrying no discriminative signal for task matching. Deliberately tiny:
# an aggressive stoplist throws away the domain vocabulary that retrieval
# depends on.
STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "of", "to", "in", "for", "on", "with", "is", "are", "be", "by", "from", "that", "this", "it", "as", "at"]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


@functools.lru_cache(maxsize=4096)
def index_text(text: str) -> str:
    """The form both sides of retrieval are compared in: literals removed.

    Applied to the stored pattern AND to the incoming task, because a shape on
    one side and raw text on the other would simply never match. Idempotent --
    `slot_number` contains no literal to abstract -- so a pattern that was
    already abstracted at proposal time passes through unchanged.
    """
    return render_pattern(abstract(text).template)


# ANATOMY: MIN_DISTINCT_TASKS
#   Distinct task SHAPES that must have been solved before a candidate skill is
#   offered to a human. Why 2 and not "2 successes": the old rule counted two
#   agents passing the same node of the same run, which is evidence the work is
#   repeatable, not that the approach transfers -- those two draws share a task,
#   a criterion and a prompt, so they are one observation counted twice. Two
#   distinct shapes is the smallest number that can distinguish "this worked"
#   from "this works on more than the thing it came from".
#
#   The bar is only as good as what counts as "distinct", which is
#   `generalise.task_signature` -- a task's subject matter plus the kinds of
#   literal it carries, not its sentence. Number-swapped near-duplicates count
#   as ONE shape (`"3 pens at 1.25"`, `"7 pens at 2.50"`), and so does the same
#   question reworded (`"please compute ... for me"`), reordered or re-typed in
#   capitals. Keying this on the abstracted SENTENCE is what made the bar
#   farmable: a paraphrase produced a second fingerprint, and one task asked
#   twice reached a human as if two tasks had agreed.
MIN_DISTINCT_TASKS = 2

# Provenance, not a ledger. Enough shapes to see the spread a skill has been
# proven over; bounded because it is written into every worker prompt's
# approval record and read by a human.
MAX_EVIDENCE_TASKS = 8

# ANATOMY: MIN_SHAPE_SLOTS
#   Below this many literal kinds, the shape rule is not applied at all. One
#   placeholder is not a shape: a pattern whose only slot is `slot_number` says
#   no more than "a number appears somewhere", and gating on it would refuse a
#   plainly-related task for lacking a digit. It is the COMBINATION of literal
#   kinds that says what kind of question a pattern is about.
MIN_SHAPE_SLOTS = 2

_SLOT_PREFIX = "slot_"

# ANATOMY: _SLOT_TERM
#   Excluded from shape agreement on BOTH sides, which is the tightening a
#   measured false hit forced. `slot_term` is not a kind of literal like a
#   price or a date -- it is what `strip_source_terms` leaves behind where a
#   noun was, and on the query side `abstract` emits it for any capitalised
#   word at all. Counted, it let the pen skill (quantity AND price AND noun)
#   agree with `"Compute the total headcount of 5 teams in the Boston office"`
#   and `"Compute the score of 5 players named Alice"` at 2 of 3 kinds: a
#   number, plus a proper noun that happened to be in the sentence. Neither is
#   a unit-price question. A proper noun is evidence about a task's subject,
#   never about the kind of work it asks for.
_SLOT_TERM = "slot_term"


@dataclass(slots=True)
class Skill:
    """A reusable approach, with the evidence that it worked.

    `provenance` is not decoration. When a skill turns out to be harmful, the
    question is immediately "what else came from that run", and without a chain
    back to the originating run and criterion hash the answer is unavailable.

    `evidence_tasks` holds abstract task FINGERPRINTS, never task text. A
    fingerprint answers "how many different kinds of task has this worked on"
    without giving anything that reads it a literal to leak -- the same
    minimisation that keeps literals out of `instruction` and `task_pattern`.
    """

    skill_id: str
    name: str
    task_pattern: str          # the SHAPE of task this applies to, not its text
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
    # Distinct task shapes this approach has been verified on. Defaulted so a
    # library written by an older build still loads; an older build reading a
    # file with these fields refuses it, which is the one-way direction
    # `_load`'s unknown-field check already enforces.
    evidence_tasks: tuple[str, ...] = ()
    # How much of the ORIGINATING step was method vocabulary rather than the
    # task's own words, before literals were stripped. A reviewer signal, not a
    # gate: a low score says "this step was mostly the task restated", which is
    # worth seeing next to the advice it produced.
    generality: float = 0.0

    def __post_init__(self) -> None:
        # JSON has no tuples. Coerced here rather than at the load site so the
        # invariant holds for every construction path, including a test's.
        if not isinstance(self.evidence_tasks, tuple):
            self.evidence_tasks = tuple(self.evidence_tasks)

    @property
    def promotable(self) -> bool:
        """Has this approach worked on more than the task it came from?

        The question the human queue asks. A candidate below this bar is kept
        and accrues evidence; it is simply not worth a reviewer's attention
        yet, because there is nothing in it a second task has confirmed.
        """
        return len(self.evidence_tasks) >= MIN_DISTINCT_TASKS

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


def validate_instruction(instruction: str, *, source_task: str = "") -> str:
    """Reject what cannot be a reusable instruction. Raises, never repairs.

    Repairing would be worse: a silently rewritten skill is one nobody
    reviewed, and the human approval gate is the only thing standing between a
    hallucinated approach and every future run's prompt.

    ANATOMY: source_task
      The task the instruction was distilled from, when there is one. Supplied,
      it enables the GENERALITY check: an instruction may not share a literal
      -- a number, a price, a date, a quoted string -- with its own source.
      That is the poisoning that actually happened, one level subtler than
      serialised output: `"Compute the total cost of 3 pens at 1.25 dollars
      each"` is shaped like advice and reads like a method, but the numbers in
      it are one task's answer, and a later run about pencils is handed them
      and told they worked.

      Compared as whole literals, never as substrings, so `"2 decimal places"`
      in an instruction is not a leak of `"1.25"` in the task merely because a
      digit appears in both. Default empty, so every existing caller keeps
      exactly its current behaviour -- a check that cannot see the task cannot
      claim anything about the task.
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
    leaked = shared_literals(text, source_task)
    if leaked:
        raise SkillLibraryError(
            f"instruction shares the literal(s) {sorted(leaked)} with the task "
            f"it was distilled from; that is one task's answer presented as a "
            f"general method, and the next task's numbers will differ"
        )
    return text


def _slot_kinds(terms: set[str]) -> set[str]:
    """The literal kinds in a term set, less the stripped-noun placeholder."""
    return {t for t in terms if t.startswith(_SLOT_PREFIX)} - {_SLOT_TERM}


def _stem(word: str) -> str:
    """Fold the plural forms English actually produces. Deterministic, no data.

    ANATOMY: why this exists at all
      Shape agreement compares METHOD VOCABULARY as a set, and set comparison
      is exact. A skill distilled from `"parse csv tabular files"` therefore
      refused `"parse a csv file of tabular data"` -- `files` and `file` are
      different strings, so the subset test failed and a correct, on-topic
      skill was withheld. A rule that a plural can defeat is not a rule about
      meaning, and this is the shape of failure that only appears in front of
      real traffic.

    ANATOMY: three rules, not a stemmer library
      Porter and friends are heavier than the job and, more to the point, they
      also fold verb tense and derivational endings -- which would start
      merging `"compute"` with `"computation"` and quietly widen retrieval.
      This narrows to plurals and stops:

        queries -> query      (`ies` is never a plural `y` word's own ending)
        boxes   -> box        (`es` only after a sibilant, so `files` is safe)
        files   -> file       (bare `s`, guarded below)

      `ss` is excluded because `process`, `address` and `class` are singular,
      and short words are excluded because `css`, `abs` and `ops` are whole
      words rather than plurals.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and word[:-2].endswith(
        ("s", "x", "z", "ch", "sh")
    ):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _method_terms(words: set[str]) -> set[str]:
    """The method vocabulary of a phrase, folded to singular forms."""
    return {_stem(w) for w in words & METHOD_LEXICON}


def _shapes_agree(terms: set[str], pattern: str, query: set[str]) -> bool:
    """Is the incoming task the same KIND of question the pattern is about?

    Two conditions, and both are about the abstract template rather than about
    words that happen to be shared.

    ANATOMY: every literal kind, not a fraction of them
      A pattern naming a quantity AND a price is about what those two do to
      each other, so a task carrying only one of them is asking something else.
      The rule used to accept half the kinds, which is how `"...5 teams in the
      Boston office"` -- a number and a capitalised word -- agreed with a
      unit-price pattern. Requiring all of them refuses those and still admits
      the transfer this feature exists for: `"7 pencils at 40c each"` carries
      both a quantity and a price.

      Vacuously true below `MIN_SHAPE_SLOTS`, because a pattern naming one kind
      makes no claim about the shape of its task, and a rule with nothing to
      check must not refuse anything.

    ANATOMY: the method has to agree too
      Slot kinds alone cannot separate `"total cost of <n> <x> at <money>"`
      from `"sort <n> <x> by price into <money> bands"`. So the METHOD
      VOCABULARY of the pattern -- its verbs and nouns, the words that say what
      the work is -- must be present in the task as well.

      Unless the task states no method at all. `"7 pencils at 40c each"` is a
      bare noun phrase: it names quantities and a thing, and nothing about how.
      A question that claims no method cannot contradict one, and refusing it
      would delete the headline case. The pattern's own words come from
      `task_pattern` only, never from `name` -- a name is written by the
      distiller about the artifact it saw ("approach: produce total_cost"), and
      demanding a task echo it would refuse every task that did not.
    """
    pattern_slots = _slot_kinds(terms)
    if len(pattern_slots) >= MIN_SHAPE_SLOTS and not pattern_slots <= _slot_kinds(query):
        return False
    query_method = _method_terms(query)
    if not query_method:
        return True
    return _method_terms(set(tokenize(pattern))) <= query_method


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
        evidence_task: str = "",
        source_task: str = "",
        generality: float = 0.0,
    ) -> Skill:
        """Record a candidate. It is NOT usable until a human approves it.

        ANATOMY: evidence_task
          The abstract fingerprint of the task this proposal came from. The
          same instruction proposed by a DIFFERENT task shape accrues a second
          fingerprint rather than creating a second skill -- which is the whole
          mechanism by which "worked twice on the same node" is distinguished
          from "worked on two different tasks". Content addressing is what
          makes it work: identical advice is one skill, so the evidence for it
          accumulates in one place instead of fragmenting per run.
        """
        validate_instruction(instruction, source_task=source_task)
        skill_id = make_skill_id(name, instruction)
        # The index must never carry a literal, whatever the caller passed. A
        # raw task here is the memorisation channel this library used to have.
        pattern = index_text(task_pattern.strip())
        existing = self._skills.get(skill_id)
        if existing is not None:
            if self._add_evidence(existing, evidence_task):
                self.save()
            return existing
        skill = Skill(
            skill_id=skill_id,
            name=name.strip(),
            task_pattern=pattern,
            instruction=instruction.strip(),
            provenance_run=run_id,
            provenance_criterion=criterion_hash,
            evidence_tasks=(evidence_task,) if evidence_task else (),
            generality=round(generality, 4),
        )
        self._skills[skill_id] = skill
        self.save()
        return skill

    def record_evidence(self, skill_id: str, task_fingerprint: str) -> Skill | None:
        """Note that this approach also worked on another task shape.

        Separate from `record_use` on purpose. A use is an offer that was taken
        up; evidence is a VERIFIED success on a shape, and only the second is
        allowed to move a candidate toward a human.
        """
        skill = self._skills.get(skill_id)
        if skill is None:
            return None
        if self._add_evidence(skill, task_fingerprint):
            self.save()
        return skill

    @staticmethod
    def _add_evidence(skill: Skill, task_fingerprint: str) -> bool:
        """Idempotent by construction: a repeated shape is not new evidence."""
        if not task_fingerprint or task_fingerprint in skill.evidence_tasks:
            return False
        if len(skill.evidence_tasks) >= MAX_EVIDENCE_TASKS:
            return False
        skill.evidence_tasks = (*skill.evidence_tasks, task_fingerprint)
        return True

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

    def promotable(self) -> list[Skill]:
        """Candidates with evidence from enough distinct task shapes to review.

        The rest are not rejected, they are not yet ANSWERABLE: "does this
        transfer" has no evidence either way after one task, and putting that
        question to a human is how a library fills with approaches nobody could
        have evaluated.
        """
        return [s for s in self.pending() if s.promotable]

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

    @staticmethod
    def _terms(skill: Skill) -> set[str]:
        """A skill's index terms, abstracted.

        The NAME is abstracted too, not only the pattern. A name is written by
        the distiller from the work it saw, so a date or a proper noun in it
        would put back into the index exactly what cleaning the pattern took
        out. It is not a complete defence -- `abstract` cannot see a literal
        welded into an identifier like `report_2024` -- which is why the
        distiller derives names from the artifact shape rather than the node.
        """
        return set(tokenize(index_text(f"{skill.task_pattern} {skill.name}")))

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
            counts.update(self._terms(skill))
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
        # The incoming task is abstracted with the SAME function the stored
        # pattern went through. Anything else compares a shape to a sentence.
        query = set(tokenize(index_text(task)))
        if not query:
            return []

        scored: list[tuple[float, Skill]] = []
        for skill in usable:
            terms = self._terms(skill)
            if not terms:
                continue
            overlap = query & terms
            if not overlap:
                continue
            if not _shapes_agree(terms, skill.task_pattern, query):
                # The words match; the kind of question does not. Offering a
                # unit-price approach to a task with no price in it is the
                # loose half of the same memorisation bug -- matching on
                # surface vocabulary rather than on the shape of the work.
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
            # Split out because the two numbers mean different things to a
            # reviewer: `pending` is the backlog, `promotable` is the part of
            # it that has evidence from more than one kind of task and is
            # therefore worth an opinion.
            "promotable": len(self.promotable()),
            "retired": sum(1 for s in skills if s.retired),
            "total_uses": sum(s.uses for s in skills),
            "mean_success_rate": (
                round(sum(s.success_rate for s in used) / len(used), 4) if used else 0.0
            ),
        }
