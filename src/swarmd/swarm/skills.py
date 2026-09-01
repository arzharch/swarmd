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
    _stem,
    abstract,
    corroborate,
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
    # Covers the DECISION, where `skill_id` covers only the content. Written by
    # `save`, verified by `_load`. See `attestation_for`.
    attestation: str = ""
    # Distinct task shapes this approach has been verified on. Defaulted so a
    # library written by an older build still loads; an older build reading a
    # file with these fields refuses it, which is the one-way direction
    # `_load`'s unknown-field check already enforces.
    evidence_tasks: tuple[str, ...] = ()
    # The instruction each of those shapes distilled, in the same order. Kept
    # so promotion can VERIFY the advice rather than trust it: two task shapes
    # producing the same approach also produced two independent wordings of
    # it, and what only one of them said came from that one task. See
    # `served_instruction`. Defaulted for libraries written before this field.
    evidence_instructions: tuple[str, ...] = ()
    # How much of the ORIGINATING step was method vocabulary rather than the
    # task's own words, before literals were stripped. A reviewer signal, not a
    # gate: a low score says "this step was mostly the task restated", which is
    # worth seeing next to the advice it produced.
    generality: float = 0.0
    # Set only when `approve(..., force=True)` let a skill through short of
    # MIN_DISTINCT_TASKS evidence. Mirrors `retired_reason`: the bypass has to
    # be visible on the record itself, not just in whatever process invoked
    # it, because the record is what a later reader -- or the next `_load` --
    # actually sees.
    approval_note: str = ""

    def __post_init__(self) -> None:
        # JSON has no tuples. Coerced here rather than at the load site so the
        # invariant holds for every construction path, including a test's.
        if not isinstance(self.evidence_tasks, tuple):
            self.evidence_tasks = tuple(self.evidence_tasks)
        if not isinstance(self.evidence_instructions, tuple):
            self.evidence_instructions = tuple(self.evidence_instructions)

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

    @property
    def served_instruction(self) -> str:
        """What a worker is actually shown, and what a reviewer approves.

        NOT `instruction`, which is one task's wording of the approach and is
        kept verbatim because it is half of the content address. This is that
        wording with every prose word no OTHER contributing task also used
        removed -- the verification step ADR-015 named as missing.

        Domain-as-method contamination is one task's vocabulary by
        construction: `probes`, `monitoring`, `stock levels` reach the library
        because the planner knew the subject, and no rule can tell them from
        `parse` and `validate` by looking. It does not need to. A second task
        of a DIFFERENT shape, which the promotion bar already requires, either
        used the word too -- in which case it is not one task's vocabulary --
        or did not, in which case it goes. Deterministic, no threshold, no
        classifier, and nothing tuned on a sample.

        Falls back to the stored instruction when there is only one variant,
        or when the format is not the distiller's. Both are honest: a single
        variant corroborated against itself would look verified and is not.
        """
        parts = split_instruction(self.instruction)
        if parts is None or len(self.evidence_instructions) < 2:
            return self.instruction
        # DISTINCT wordings. `merge_identity` replays one record's single
        # instruction once per shape it had accrued, so a migrated library can
        # hold the same string several times -- and corroborating a string
        # against itself claims a verification that never happened.
        prose = list(dict.fromkeys(
            split[1]
            for variant in self.evidence_instructions
            if (split := split_instruction(variant)) is not None
        ))
        if len(prose) < 2:
            return self.instruction
        prefix, _, tail = parts
        agreed = corroborate(prose)
        if not agreed:
            # Nothing in the prose survived two tasks. The advice keeps the
            # part that is generated from structure and says nothing else --
            # the "structured parts only" instruction ADR-015 describes,
            # arrived at by evidence rather than chosen up front.
            return tail.strip() or self.instruction
        return f"{prefix}{agreed} {tail}".rstrip() if tail else f"{prefix}{agreed}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_skill_id(name: str, instruction: str) -> str:
    """Content-addressed, so the same skill proposed twice is one skill."""
    payload = f"{name.strip().lower()}|{instruction.strip()}"
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def approach_key(name: str, task_pattern: str) -> str:
    """Identity for "the same approach", independent of wording and of the plan.

    WHY NOT `make_skill_id`. That hashes the instruction TEXT, and the
    instruction is written by a model: the same approach distilled from two
    runs comes back phrased differently every time. Each phrasing minted a new
    record starting again from one piece of evidence, so `promotable` -- which
    wants MIN_DISTINCT_TASKS distinct task shapes -- was unreachable, nothing
    was ever queued for review, nothing was ever approved, and the treatment
    arm of every ablation had an empty library to retrieve from.

    WHY THE PLAN STEP IS NOT IN THE KEY, which is the part that took a
    measurement to see. `task_pattern` is the abstracted step plus the kinds of
    check that graded it, and the step comes from a plan synthesised for one
    task. Steps therefore never recur across tasks: a key containing one can
    only ever match another proposal from the SAME task, which is precisely the
    evidence the bar refuses to count. Measured on a real session -- four
    proposals from one task, four distinct step texts, and no cross-task match
    available at any sample size.

    What does recur is the kind of work: the artifact shape the step produced.
    That is what `name` records, abstracted and carrying no literal, and it is
    the whole identity.

    AND THE CHECK KINDS ARE NOT IN IT EITHER, which reverses an earlier version
    of this function on evidence. The criterion is authored fresh for every run
    (ADR-009), so its set of checks varies between two runs of the same work:
    one run grades a diagnosis with `artifact_exists + contains_all +
    json_parses + output_nonempty`, the next with three of those. Keying on
    them reintroduced the exact fragmentation this function exists to remove,
    one level up -- the same approach from two runs landed on two keys.

    Measured on a 38-record library rather than argued: keying on name and
    check kinds gave 38 approaches and 3 that cleared the evidence bar; keying
    on the name alone gave 34 and 5. Fewer records, more of them answerable.

    THE COST, stated because it is real: two different steps of one task merge
    when they produce the same artifact shape, and the instruction kept is the
    one proposed first. Those records were already competing for the same
    retrieval slot -- `_terms` indexes on the same name -- so the library was
    not distinguishing them either. The human gate and success-rate pruning are
    what choose between approaches; this only decides what counts as one.

    Deterministic: sha256 over sorted unique terms. No threshold, no model in
    the path.

    Necessary and NOT sufficient. Evidence only accumulates when two DIFFERENT
    tasks propose the same approach, which needs a corpus whose tasks share
    output shapes -- the `train` arm. See ADR-014.
    """
    # `task_pattern` is accepted and deliberately unused. See below; the
    # signature keeps it so a future key can take it back without touching
    # every call site.
    del task_pattern

    shape = sorted(set(tokenize(index_text(name))))
    return hashlib.sha256(" ".join(shape).encode()).hexdigest()[:12]


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
# The record at which an approved skill has demonstrably stopped earning its
# retrieval slot. Shared by `prune`, which retires on it, and by `retrieve`,
# which stops offering on it -- one rule, so a skill cannot be simultaneously
# too poor to keep and still being handed to workers.
#
# Why 5 uses: a skill that failed twice may have met two hard tasks. Why 30%:
# well below useful, so only clearly-harmful entries are caught; pruning harder
# would shrink the library toward the current task distribution, which is
# overfitting by another name.
PRUNE_MIN_USES = 5
PRUNE_MIN_SUCCESS_RATE = 0.3

MIN_INSTRUCTION_CHARS = 8
MAX_INSTRUCTION_CHARS = 2_000
MAX_NAME_CHARS = 120

# The one prose span in a distilled instruction, and therefore the only span
# corroboration has anything to do. Everything around it is generated from
# structure -- the artifact's value kinds and a fixed warning -- and is
# identical in every variant by construction, so intersecting it would only
# shred a sentence that carries nothing a task contributed. Defined here rather
# than in `run.py` because BOTH sides need the same two strings: the distiller
# writes them and `Skill.served_instruction` splits on them.
INSTRUCTION_PREFIX = "When a step calls for this: "
INSTRUCTION_SHAPE_CLAUSE = " Produce a JSON object whose values are of these kinds: "


def split_instruction(instruction: str) -> tuple[str, str, str] | None:
    """`(prefix, prose, tail)`, or None if this is not a distilled instruction.

    None rather than a guess. A library predating this format, or an
    instruction written by hand, has no identifiable prose span, and treating
    the whole string as prose would corroborate the boilerplate away.
    """
    if not instruction.startswith(INSTRUCTION_PREFIX):
        return None
    body = instruction[len(INSTRUCTION_PREFIX) :]
    cut = body.find(INSTRUCTION_SHAPE_CLAUSE.strip())
    if cut < 0:
        return INSTRUCTION_PREFIX, body.strip(), ""
    return INSTRUCTION_PREFIX, body[:cut].strip(), body[cut:]


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


def attestation_for(skill: Skill) -> str:
    """A checksum over everything a reviewer decided, not just what they read.

    WHY THIS EXISTS SEPARATELY FROM `skill_id`. The id is a content address of
    name+instruction, so it is invariant under flipping `approved` -- which
    made the human gate defeatable by editing one boolean in a JSON file. The
    id answers "is this the same skill"; nothing answered "is this the same
    decision", so nothing was checking the field that decides whether a skill
    reaches every future run's prompt.

    WHAT THIS IS NOT. It is TAMPER EVIDENCE, not authentication. Anyone who can
    write the file can also recompute this value -- there is no secret here,
    deliberately: a keyfile was considered and rejected because it introduces a
    trust primitive nothing else in this codebase has, for a threat model
    (local write access) already accepted as out of scope. What it does catch
    is the realistic case: an accidental edit, a merge artifact, a truncated
    write, or someone flipping a flag without understanding the format. The
    durable approval store remains the record of who decided what and when.
    """
    payload = "|".join((
        skill.skill_id,
        skill.name,
        skill.instruction,
        "1" if skill.approved else "0",
        skill.approved_by,
        "1" if skill.retired else "0",
        skill.retired_reason,
    ))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
            # An APPROVED skill must carry an attestation. Refused rather than
            # upgraded in place: an entry that grants itself approval with no
            # checksum is the precise shape of the attack, so accepting it
            # "just this once for legacy files" would reopen the hole it
            # closes. An unapproved entry may lack one -- it grants nothing,
            # and refusing it would break libraries mid-review for no gain.
            if skill.approved and not skill.attestation:
                raise SkillLibraryError(
                    f"library at {self.path}: entry {index} ({skill.name!r}) is "
                    f"marked approved but carries no attestation; re-approve it "
                    f"through the gate rather than editing the file"
                )
            if skill.attestation and skill.attestation != attestation_for(skill):
                raise SkillLibraryError(
                    f"library at {self.path}: entry {index} ({skill.name!r}) has "
                    f"an attestation that does not match its contents or its "
                    f"approval state; this entry has been edited since it was "
                    f"written"
                )
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
        # Re-stamped here rather than at each mutation site, because `save` is
        # the one funnel every change passes through -- approve, reject, prune
        # and record_use all end here, and a new mutator added later gets the
        # attestation for free instead of silently skipping it.
        for skill in self._skills.values():
            skill.attestation = attestation_for(skill)
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
        if existing is None:
            # Same approach, different words. The evidence belongs on the
            # record that already holds some, rather than on a second copy
            # starting again from one shape -- which is what put `promotable`
            # out of reach entirely (ADR-014).
            existing = self._same_approach(name, pattern)
        if existing is not None:
            # The NEW wording travels with the new fingerprint. This is the
            # whole input to `served_instruction`: without it a second shape
            # confirms that the approach transfers but leaves no way to tell
            # which words were the approach and which were the first task.
            if self._add_evidence(existing, evidence_task, instruction.strip()):
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
            evidence_instructions=(instruction.strip(),) if evidence_task else (),
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

    def _same_approach(self, name: str, pattern: str) -> Skill | None:
        """An existing, non-retired record for this approach, or None.

        Retired records are skipped deliberately: reviving a pruned approach
        through the back door of a re-proposal would undo a decision somebody
        made, and `retired_reason` would then describe a live skill.
        """
        key = approach_key(name, pattern)
        for skill in self._skills.values():
            if skill.retired:
                continue
            if approach_key(skill.name, skill.task_pattern) == key:
                return skill
        return None

    @staticmethod
    def _add_evidence(
        skill: Skill, task_fingerprint: str, instruction: str = ""
    ) -> bool:
        """Idempotent by construction: a repeated shape is not new evidence.

        `instruction` is how the NEW shape worded this approach. Optional
        because `record_evidence` is called from paths that only carry a
        fingerprint; when it is absent the wording list simply does not grow,
        and `served_instruction` corroborates over what it has.
        """
        if not task_fingerprint or task_fingerprint in skill.evidence_tasks:
            return False
        if len(skill.evidence_tasks) >= MAX_EVIDENCE_TASKS:
            return False
        skill.evidence_tasks = (*skill.evidence_tasks, task_fingerprint)
        if instruction and instruction not in skill.evidence_instructions:
            skill.evidence_instructions = (*skill.evidence_instructions, instruction)
        return True

    def approve(self, skill_id: str, *, actor: str, force: bool = False) -> Skill:
        """Approve a candidate. Refuses a candidate short of its own evidence bar.

        ANATOMY: the check
          `run.py` only calls `SkillGate.submit` once a candidate is
          `promotable` (MIN_DISTINCT_TASKS), but that check lives at the
          CALLER -- it gates who reaches the queue, not what `approve` itself
          will do. Nothing stopped a candidate that slipped past it (a stale
          duplicate, `--auto-approve`, a direct call) from being approved on
          one task's evidence. Checked here, at the one place every approval
          path converges, closes that regardless of how it arrived.

          Vacuous for a candidate with NO recorded evidence_tasks at all,
          same idiom as `MIN_SHAPE_SLOTS` above: the real distillation path
          (`run.py`) always supplies `evidence_task` to `propose`, so an
          empty `evidence_tasks` means this skill was never put through
          per-task tracking in the first place -- typically a skill entered
          by hand -- and a rule about DISTINCT task shapes has nothing to
          say about a candidate that was never counted against it.

        ANATOMY: force
          The explicit escape for an operator who has looked at a thin
          candidate and wants it in anyway. The bypass is written to the
          skill's own record, not just logged, because the record is what a
          later reader sees -- the same reason `retired_reason` exists.
        """
        skill = self._require(skill_id)
        short_of_bar = bool(skill.evidence_tasks) and not skill.promotable
        if short_of_bar and not force:
            raise SkillLibraryError(
                f"skill {skill_id} has evidence from only "
                f"{len(skill.evidence_tasks)} distinct task shape(s); needs "
                f"{MIN_DISTINCT_TASKS} before it can be approved "
                f"(pass force=True to approve it anyway)"
            )
        if short_of_bar:
            skill.approval_note = (
                f"evidence bar bypassed by {actor}: only "
                f"{len(skill.evidence_tasks)}/{MIN_DISTINCT_TASKS} distinct "
                f"task shape(s)"
            )
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

    def merge_identity(self, path: str | Path) -> tuple[SkillLibrary, dict[str, int]]:
        """Rebuild this library at `path` with one record per approach.

        A library written before ADR-014 holds one record per PHRASING, each
        carrying evidence from a single task shape, so `promotable` was
        unreachable. This collapses them.

        DECISIONS SURVIVE, and that is most of the work. Replaying through
        `propose` alone mints candidates, so it silently un-approves the
        library, drops every retired record, and resets the use counts pruning
        reads -- a migration that destroys the reviews it exists to preserve.
        Found by running it twice: the second time it erased two pruning
        verdicts that had cost 53 retrievals to earn.

        The rules, in the conservative direction each time:

          retired    carried at the APPROACH level. Under this identity two
                     phrasings ARE one approach, so a rejection of one is a
                     rejection of it. Never resurrected, and a retired approach
                     with no surviving phrasing is kept as its own record so
                     the decision does not vanish with the row.
          approved   carried only when the SURVIVING instruction is the one
                     that was approved -- matched on `skill_id`, the hash of
                     that text. Which phrasing survives depends on stored
                     order, so an approval that cannot be matched is dropped
                     and COUNTED rather than transferred to text nobody read.
          uses       summed across the phrasings. They are one approach now, so
                     the record of how it performed is one record too.

        Returns the new library and a tally for the caller to report.
        """
        tally = {"records_in": 0, "approaches_out": 0,
                 "approvals_kept": 0, "approvals_dropped": 0,
                 "retirements_kept": 0}
        records = self.all()
        tally["records_in"] = len(records)

        library = SkillLibrary(str(path))
        for record in records:
            if record.retired:
                continue
            for shape in record.evidence_tasks or ("",):
                library.propose(
                    name=record.name,
                    task_pattern=record.task_pattern,
                    instruction=record.instruction,
                    run_id=record.provenance_run,
                    criterion_hash=record.provenance_criterion,
                    evidence_task=shape,
                    generality=record.generality,
                )

        surviving = {
            approach_key(skill.name, skill.task_pattern): skill
            for skill in library.all()
        }
        for record in records:
            key = approach_key(record.name, record.task_pattern)
            target = surviving.get(key)

            if record.retired and target is None:
                # Nothing of this approach survived the replay. Keep the
                # rejection itself, or the next session re-proposes it as new.
                library._skills[record.skill_id] = record
                tally["retirements_kept"] += 1
                continue
            if target is None:
                continue

            target.uses += record.uses
            target.successes += record.successes
            if record.retired:
                target.retired = True
                target.retired_reason = record.retired_reason
                tally["retirements_kept"] += 1
            elif record.approved:
                if target.skill_id == record.skill_id:
                    target.approved = True
                    target.approved_by = record.approved_by
                    target.approval_note = record.approval_note
                    tally["approvals_kept"] += 1
                else:
                    tally["approvals_dropped"] += 1

        library.save()
        tally["approaches_out"] = len(library.all())
        return library, tally

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
        usable = [s for s in self.approved() if not self._spent(s)]
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

    @staticmethod
    def _spent(skill: Skill) -> bool:
        """Has this skill already earned a pruning verdict?

        `prune` runs at consolidation, which is every N TASKS -- and within one
        task a skill can be retrieved by every node. So a skill that is failing
        kept being offered until the next consolidation caught up with it.
        Measured: an approved skill reached **26 uses at 0% success** before it
        was retired, which is 26 workers handed advice the library already had
        the evidence to withdraw.

        Checked here as well, against the same constants, so the damage is
        capped at the evidence threshold instead of at whatever the
        consolidation interval happens to be. Consolidation still does the
        retiring: this only stops the bleeding, and a skill it hides is a skill
        the next `prune` will retire for the same reason.
        """
        return (
            skill.uses >= PRUNE_MIN_USES
            and skill.success_rate < PRUNE_MIN_SUCCESS_RATE
        )

    def prune(
        self,
        *,
        min_uses: int = PRUNE_MIN_USES,
        min_success_rate: float = PRUNE_MIN_SUCCESS_RATE,
    ) -> list[Skill]:
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
