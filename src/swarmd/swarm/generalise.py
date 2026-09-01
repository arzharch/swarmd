"""Turning one task's text into the SHAPE of a task. Pure, deterministic, no I/O.

WHY THIS EXISTS. Distillation kept `plan_node.instruction` verbatim as the
skill's advice, so the library filled with entries like

    "calculate_pen_cost: Compute the total cost of 3 pens at 1.25 dollars each"

presented to every future run as a general method. It is not a method. It is
one task's literals wearing a method's grammar, and a later run about pencils
at 40c is handed the wrong numbers and told they worked. The same text was
also stored as the skill's `task_pattern`, so retrieval matched a skill because
the incoming task LOOKED LIKE the one it came from -- memorisation through the
index, which is the harder half of the bug to see.

THE RULE THIS MODULE ENFORCES: a literal from the task may never survive into
anything a later run reads. `abstract` replaces literals with typed
placeholders; `strip_source_terms` removes the subject-matter vocabulary the
step shares with its own task; `render_pattern` turns what is left into a
tokenizer-safe retrieval key; `generality` scores how much of a candidate
instruction is still just its source task restated.

WHY REGEX AND NOT A MODEL. Every function here runs on the distillation path,
which is already model-adjacent output being fed back into this system's own
inputs. Asking a model to "generalise this instruction" would put a second
untrusted author on the only write path into the library, and it would cost a
provider call per node on a quota-bound system. These rules are legible,
free, and identical on every run -- so a chaos-integrity hash stays comparable
and a reviewer can say exactly why a token was replaced.

WHY NOT SIMILARITY. `router/cache.py` documents what happened the last time
this system compared machine-assembled text by cosine: three genuinely
different plan nodes measured 0.97 similar, because templated prompts are
dominated by shared boilerplate. Nothing here scores similarity. Two texts
either abstract to the same template or they do not.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

# Ordered, longest-match-first. Order IS the semantics: `1.25 dollars` must be
# MONEY rather than NUMBER followed by a word, and `2024-01-05` must be a DATE
# rather than three numbers, or two texts that say the same thing abstract to
# different templates and never match.
SLOTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("URL", re.compile(r"https?://\S+")),
    ("PATH", re.compile(r"(?:[A-Za-z]:\\|\./|/)[\w./\\-]{2,}")),
    ("DATE", re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    (
        "MONEY",
        re.compile(
            r"(?<![\w.])(?:[$£€]\s?\d[\d,]*(?:\.\d+)?"
            r"|\d[\d,]*(?:\.\d+)?\s?(?:dollars?|usd|eur|gbp|cents?|c\b|p\b))"
        ),
    ),
    ("PERCENT", re.compile(r"\d+(?:\.\d+)?\s?(?:%|percent)")),
    ("QUOTED", re.compile(r"\"[^\"\n]{1,80}\"|'[^'\n]{1,80}'")),
    # The trailing guard rejects a following period ONLY when it is a decimal
    # point. It used to reject any period, which meant a number at the end of a
    # sentence was invisible to abstraction -- and the end of a sentence is
    # where an answer gets stated. `"the answer is 42."` yielded no literals at
    # all, so a distilled step reading `"count permutations of the remaining
    # <NUMBER> <TERM>: <NUMBER>! = 6."` kept the 6: the factorial's argument
    # abstracted and its RESULT did not. `shared_literals` cannot catch that
    # either -- it compares the instruction against the TASK, and a computed
    # answer never appeared in the task.
    ("NUMBER", re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?!\w|\.\d)")),
    (
        "TERM",
        re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}|[A-Z]{2,})\b"),
    ),
)

# A number that IS the method, not an answer, survives. "round to 2 decimal
# places" is advice; "3 pens" is data. The difference is the word after it, so
# that is what decides -- not a magnitude heuristic, which would keep 2 and
# drop 1.25 for no reason a reader could defend.
KEEP_AFTER = frozenset(
    {"decimal", "decimals", "places", "significant", "digits", "precision", "dp", "sf"}
)

# Vocabulary that describes HOW work is done rather than WHAT it was about.
# Deliberately a fixed, human-authored list rather than a frequency cut-off:
# a corpus-derived stoplist changes as the task distribution changes, so the
# same instruction would abstract differently in different weeks and the
# content-addressed skill id would move underneath the library.
#
# The bar for membership: could this word appear in a step for a task about
# ANY subject? "compute", "round", "field" pass. "pens", "invoice", "kidney"
# do not -- and those are exactly the tokens that must not survive.
METHOD_LEXICON = frozenset(
    {
        "add", "apply", "append", "average", "calculate", "check", "collect",
        "column", "columns", "combine", "compare", "compute", "convert",
        "count", "cost", "csv", "date", "derive", "difference", "divide",
        "emit", "entry", "entries", "extract", "fetch", "field", "fields",
        "file", "files", "filter", "format", "gather", "group", "identify",
        "index", "item", "items", "join", "json", "key", "keys", "line",
        "lines", "list", "load", "match", "mean", "median", "merge", "multiply",
        "name", "names", "normalise", "normalize", "number", "numbers",
        "object", "output", "parse", "percentage", "price", "produce", "quote",
        "range", "rate", "read", "record", "records", "report", "result",
        "results", "return", "round", "row", "rows", "save", "schema", "select",
        "sort", "source", "split", "step", "string", "subtract", "sum",
        "summarise", "summarize", "summary", "table", "task", "text", "time",
        "total", "unit", "units", "validate", "value", "values", "verify",
        "write",
    }
)


def _stem(word: str) -> str:
    """Fold the plural forms English actually produces. Deterministic, no data.

    ANATOMY: why this exists at all
      Shape agreement compares METHOD VOCABULARY as a set, and set comparison
      is exact. A skill distilled from `"parse csv tabular files"` therefore
      refused `"parse a csv file of tabular data"` -- `files` and `file` are
      different strings, so the subset test failed and a correct, on-topic
      skill was withheld. A rule that a plural can defeat is not a rule about
      meaning, and this is the shape of failure that only appears in front of
      real traffic. `task_shape` below reuses it for the same reason on the
      other side of the same problem: "3 pens" and "1 pen" are one subject.

    ANATOMY: three rules, not a stemmer library
      Porter and friends are heavier than the job and, more to the point, they
      also fold verb tense and derivational endings -- which would start
      merging `"compute"` with `"computation"` and quietly widen retrieval.
      This narrows to plurals and stops:

        queries -> query      (`ies` is never a plural `y` word's own ending)
        boxes   -> box        (`es` after an UNAMBIGUOUS sibilant: x/z/ch/sh,
                                or a doubled `ss` -- `class`, `address`,
                                `process` are singular, so `classes` must fold
                                to `class` rather than `classe`)
        houses  -> house      (`es` after a bare, non-doubled `s`: this is the
                                ambiguous case, resolved below)
        files   -> file       (bare `s`, guarded below)

      Short words are excluded from the bare-`s` rule because `css`, `abs` and
      `ops` are whole words rather than plurals.

    ANATOMY: the bare-`s`-before-`es` ambiguity, and which way it is resolved
      `"...ses"` is genuinely two different plurals wearing one spelling, and
      nothing in the word alone says which: `house + s = houses` (the base
      already ends in a silent `e`) and `bus + es = buses` (the base has no
      `e` to carry) produce the identical last four letters. There is no
      suffix rule that reads both correctly -- telling them apart needs a
      dictionary of which bare string is a real word, which is exactly the
      lookup this module refuses to keep (see WHY REGEX AND NOT A MODEL).

      This resolves the tie toward the `-se` reading (`word[:-1]`, keeping the
      `e`) rather than the bare-sibilant reading (`word[:-2]`, dropping it).
      That is a deliberate, disclosed choice, not a discovery of the "right"
      answer: `-se` nouns (case, database, expense, house, license, phrase,
      purpose, response) are common task-subject vocabulary in this system's
      domain, while short bare-sibilant loanwords that need `-es` (bus, gas)
      are not, so this is the side of the ambiguity worth folding correctly.
      The cost is disclosed, not hidden: `bus`/`buses` and `gas`/`gases` --
      previously folding correctly by accident -- now land on `bus`/`buse` and
      `gas`/`gase` and no longer unify. Longer Latin/Greek loanwords ending in
      a bare sibilant (`atlas`, `campus`, `cactus`, `canvas`, `circus`,
      `iris`, `lens`, `virus`) were ALREADY unfixable before this rule existed
      -- their SINGULAR is indistinguishable from a genuine bare-`s` plural
      (`lens` and `pens` end in the identical two letters), so the bare-`s`
      rule above strips a letter no plural ever added. That gap is untouched
      by this rule and remains open for the same reason: no suffix pattern
      separates "already singular" from "needs stripping" without a lexicon.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es"):
        base = word[:-2]
        if base.endswith(("x", "z", "ch", "sh", "ss")):
            return base
        if base.endswith("s"):
            return word[:-1]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    # THE SYMMETRIC HALF, and the reason the branch above is not enough on its
    # own. Stripping "es" folds the PLURAL of a sibilant noun (`boxes` -> `box`)
    # but leaves the SINGULAR of a silent-e one untouched, so `niches` reached
    # `nich` while `niche` stayed `niche` and one task registered as two -- the
    # farming channel this module exists to close, opened by the fold meant to
    # close it. Folding the same trailing `e` on the way in makes both sides
    # land on the same form:
    #
    #     niche -> nich   niches -> nich      cache/caches, size/sizes
    #     case  -> cas    cases  -> cas       house/houses, phrase/phrases
    #
    # It also recovers `bus`/`buses` and `gas`/`gases`, which the previous
    # resolution of this ambiguity had to give up.
    #
    # `price` is deliberately untouched: `pric` does not end in a sibilant, so
    # neither side folds and the pair still meets at `price`.
    # `s` is NOT in this set, and that is the whole subtlety. For an `-se`
    # word the branch above already resolves the plural toward the singular
    # (`cases` -> `case`), so folding the singular here too would push them
    # apart again in the opposite direction. Only the sibilants whose plural
    # sheds a full `es` need their singular to shed the `e`.
    if len(word) > 3 and word.endswith("e") and word[:-1].endswith(
        ("x", "z", "ch", "sh")
    ):
        return word[:-1]
    return word


# Grammar, not subject matter. Never stripped, never counted as content: an
# instruction reduced to placeholders joined by nothing is unreadable, and
# "the" appearing in both a step and its task is not evidence of anything.
#
# CLOSED CLASSES ONLY. Determiners, prepositions, conjunctions, auxiliaries,
# pronouns and modals -- the parts of English a speaker cannot add to. That is
# what makes this list finishable and what distinguishes it from the thing it
# must never become: a list of filler words ("just", "simply", "quickly") added
# one at a time as each is caught farming the evidence bar. Open-class fillers
# are handled STRUCTURALLY by `_analyse`, which asks where a word sits rather
# than whether anyone has written it down here.
FUNCTION_WORDS = frozenset(
    {
        "a", "all", "an", "and", "any", "are", "as", "at", "be", "been", "but",
        "by", "each", "every", "for", "from", "has", "have", "in", "into",
        "is", "it", "its", "not", "of", "on", "one", "or", "per", "that",
        "the", "their", "then", "there", "these", "this", "to", "was", "were",
        "when", "which", "with", "you", "your",
        # pronouns, modals, politeness
        "can", "could", "do", "does", "he", "her", "him", "i", "kindly", "me",
        "my", "our", "please", "she", "should", "thank", "thanks", "them",
        "they", "us", "we", "will", "would",
        # the rest of the preposition class, so a noun phrase introduced by one
        # of them is recognised as a noun phrase rather than read as content.
        "about", "across", "after", "against", "among", "before", "below",
        "beside", "between", "during", "over", "through", "under", "until",
        "within", "without",
    }
)

# ANATOMY: INTRODUCERS
#   The subset of the closed class that OPENS a noun phrase -- determiners and
#   prepositions. Everything `_analyse` calls subject matter has to sit after
#   one of these (or after a literal placeholder, which introduces a phrase the
#   same way: "<NUMBER> pens"). That positional test is what replaces asking
#   whether a word is on a list of fillers.
#
#   "each" and "every" are deliberately ABSENT even though they are
#   determiners. English writes both "each pen" (a determiner) and "at 1.25
#   each" (a postmodifier), and admitting them would make the trailing word of
#   "...at 1.25 dollars each now" the head of a phrase -- which is how "now"
#   becomes a second task shape. Excluded, the cost is a phrase like "each pen"
#   contributing no subject, which loses a distinction; admitted, the cost is a
#   filler adverb minting one, which is the defect.
#
#   "to" is absent for the same reason in the other direction: it introduces
#   phrases ("to the ledger") and infinitives ("to compute") indistinguishably.
INTRODUCERS = frozenset(
    {
        "a", "all", "an", "any", "at", "about", "across", "after", "against",
        "among", "before", "below", "beside", "between", "by", "during",
        "for", "from", "in", "into", "its", "my", "of", "on", "our", "over",
        "per", "her", "that", "the", "their", "these", "this", "through",
        "under", "until", "with", "within", "without", "your",
    }
)

# ANATOMY: MIN_GENERALITY
#   The fraction of an instruction's content vocabulary that must NOT come
#   from the task it was distilled from. Why 0.6: the observed bug string
#   scores 0.5 against its own near-duplicate phrasings (only the node name is
#   novel; "pens" is not), and a genuine cross-task merge scores 1.0 because
#   everything task-specific has already collapsed to a placeholder. 0.6 sits
#   between those two measurements rather than between two intuitions.
MIN_GENERALITY = 0.6

_PLACEHOLDER = re.compile(r"<[A-Z]+>")
# A placeholder, or a word long enough to be subject matter. Matched together
# so a substitution can pass placeholders through untouched -- otherwise
# "<NUMBER>" is seen as the word "NUMBER" and becomes eligible for stripping.
_PLACEHOLDER_OR_WORD = re.compile(r"<[A-Z]+>|[A-Za-z][A-Za-z_-]{2,}")
_WORD = re.compile(r"[a-z][a-z0-9_-]*")
_NEXT_WORD = re.compile(r"\s*([A-Za-z]+)")
# The literal kinds a stored instruction must never share with its own task.
# TERM is absent on purpose: a shared proper noun is a leak worth scoring, but
# a shared NUMBER is a wrong answer waiting to be handed to a later run.
_LITERAL_KINDS = ("URL", "PATH", "DATE", "MONEY", "PERCENT", "QUOTED", "NUMBER")


@dataclass(frozen=True, slots=True)
class Abstraction:
    """A task's shape, with its literals removed rather than recorded.

    The literal VALUES are deliberately not a field. They exist only as local
    variables inside `abstract`, so nothing that persists an `Abstraction` --
    a skill's `task_pattern`, an evidence key -- can leak a literal by
    construction. Closing the channel beats remembering to redact it.
    """

    template: str
    slots: tuple[str, ...]
    fingerprint: str


def _keeps_literal(text: str, end: int) -> bool:
    """Does the word after a number make the number part of the method?"""
    match = _NEXT_WORD.match(text, end)
    return match is not None and match.group(1).lower() in KEEP_AFTER


def _is_method_phrase(phrase: str) -> bool:
    """A capitalised phrase that is entirely method vocabulary is not a name.

    Sentence case is the reason this exists: "Compute the total" opens with a
    capital, and slotting it would delete the verb the instruction is about
    while keeping the noun it is not.
    """
    words = phrase.lower().split()
    return bool(words) and all(word in METHOD_LEXICON for word in words)


@functools.lru_cache(maxsize=4096)
def _scan(text: str) -> tuple[tuple[str, str, int, int], ...]:
    """The one authoritative reading of where a text's literals are.

    One pass, trying the slot patterns in declared order at each position, so
    the result never depends on which pattern happened to be applied first --
    which is what makes two phrasings of the same task produce the same
    fingerprint on two different machines.

    WHY EVERYTHING GOES THROUGH HERE. `literals` used to re-read the text with
    its own independent `finditer` per pattern, and an independent read is a
    read with no context: it cannot see that a NUMBER was skipped because the
    word after it made it part of the method. So `"round to 2 decimal places"`
    -- the exact phrasing `KEEP_AFTER` exists to protect -- yielded the literal
    `2`, and any task that happened to contain a bare `2` had a correct
    instruction refused for a leak that was never there. In `_distill` that is
    swallowed by a blanket `except`, so the candidate simply vanished, and the
    tasks most likely to trip it are the numeric, quantity-heavy ones this
    module was written for. Two readings of one text is one reading too many.

    Returns `(kind, matched text, start, end)` in order. A tuple because it is
    cached, and a cached mutable would let one caller edit another's answer.
    """
    found: list[tuple[str, str, int, int]] = []
    pos, end = 0, len(text)
    while pos < end:
        for kind, pattern in SLOTS:
            match = pattern.match(text, pos)
            if match is None:
                continue
            if kind == "NUMBER" and _keeps_literal(text, match.end()):
                continue
            if kind == "TERM" and _is_method_phrase(match.group(0)):
                continue
            found.append((kind, match.group(0), pos, match.end()))
            pos = match.end()
            break
        else:
            pos += 1
    return tuple(found)


@functools.lru_cache(maxsize=4096)
def abstract(text: str) -> Abstraction:
    """Replace every literal with a typed placeholder, left to right."""
    pieces: list[str] = []
    slots: list[str] = []
    cursor = 0
    for kind, _value, start, stop in _scan(text):
        pieces.append(text[cursor:start])
        pieces.append(f"<{kind}>")
        slots.append(kind)
        cursor = stop
    pieces.append(text[cursor:])
    template = "".join(pieces)
    return Abstraction(
        template=template,
        slots=tuple(slots),
        fingerprint=hashlib.sha256(template.encode("utf-8")).hexdigest()[:16],
    )


def _literal_forms(kind: str, value: str) -> set[str]:
    """Every spelling of one literal that must count as the same literal.

    A price is written three ways for the same money -- `$1.25`, `1.25 dollars`,
    `1.25` -- and a leak check that compares only whole matches would let an
    instruction restate the task's price in another notation and pass. So a
    MONEY or PERCENT match also yields its bare numeric core, which is what
    makes `"1.25"` in an instruction collide with `"1.25 dollars"` in the task.

    This is the strictness the old per-pattern `finditer` had by accident (the
    NUMBER pattern re-matched the digits inside a price). Kept on purpose here,
    because losing it would open a leak; what is NOT kept is the context
    blindness that came with it.
    """
    forms = {re.sub(r"[\s,]", "", value).lower()}
    if kind in ("MONEY", "PERCENT"):
        core = re.search(r"\d[\d,]*(?:\.\d+)?", value)
        if core is not None:
            forms.add(re.sub(r"[\s,]", "", core.group(0)))
    return forms


def literals(text: str) -> set[str]:
    """Every literal value in a text, normalised for comparison.

    Used to prove an instruction does not carry its source task's numbers.
    Compared as whole tokens, never as substrings: `"2"` is not a leak of
    `"1.25"` merely because the digit appears inside it. Read through `_scan`,
    so a number the method owns -- `"round to 2 decimal places"` -- is not a
    value at all and cannot be shared with anything.
    """
    found: set[str] = set()
    for kind, value, _start, _stop in _scan(text):
        if kind in _LITERAL_KINDS:
            found |= _literal_forms(kind, value)
    return found


def shared_literals(instruction: str, source: str) -> set[str]:
    """Literals present in BOTH an instruction and the task it came from."""
    if not source.strip():
        return set()
    return literals(instruction) & literals(source)


def _identifier_parts(token: str) -> list[str]:
    """`stock_count` -> ["stock", "count"]; `nextCursor` -> ["next", "cursor"].

    Deterministic and stdlib-only, like everything else on this path. Splits
    only where the writer put a boundary -- an underscore, a hyphen, or a case
    change -- so it never invents divisions inside an ordinary word.
    """
    spaced = re.sub(r"[_-]+", " ", token)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    return [part for part in spaced.lower().split() if part]


def strip_source_terms(text: str, source_texts: tuple[str, ...]) -> str:
    """Replace the subject-matter words a step shares with its own task.

    `abstract` cannot catch these: "pens" is a bare lowercase noun, structurally
    indistinguishable from "total" without knowing what the task said. The
    source text is what supplies that knowledge -- a word is subject matter
    when it came from the task and is not method vocabulary.

    Deliberately NOT applied to method or function words. A step reduced to
    "<TERM> the <TERM> <TERM> of <NUMBER> <TERM>" teaches nothing, and the
    point is to keep the verb while dropping the noun.
    """
    vocabulary: set[str] = set()
    for source in source_texts:
        vocabulary.update(_WORD.findall(source.lower()))
    if not vocabulary:
        return text

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("<"):
            return token
        lowered = token.lower()
        if lowered in METHOD_LEXICON or lowered in FUNCTION_WORDS:
            return token
        if lowered in vocabulary:
            return "<TERM>"
        # IDENTIFIERS, which is how subject matter got through. A step naming
        # `stock_count` is naming the task it came from just as plainly as one
        # naming "stock count", but the whole token is not in the vocabulary
        # and survived untouched -- so a skill distilled from the stock task
        # told every later run to emit `stock_count`, and a reconciliation of
        # an invoice against a payment inherited the wrong keys.
        #
        # Split only on separators the writer chose (`_`, `-`, camelCase). A
        # token is subject matter when EVERY part came from the task and at
        # least one part is not method vocabulary. So `stock_count` collapses
        # -- "stock" is the task's subject, "count" is the method -- while
        # `sort_by_price` survives, because sort/by/price are all method or
        # function words and an identifier made only of those is describing the
        # work rather than the thing worked on.
        parts = _identifier_parts(lowered)
        if (
            len(parts) > 1
            and all(part in vocabulary for part in parts)
            and not all(
                part in METHOD_LEXICON or part in FUNCTION_WORDS for part in parts
            )
        ):
            return "<TERM>"
        return token

    stripped = _PLACEHOLDER_OR_WORD.sub(replace, text)
    # A run of placeholders is one unknown, not several. Collapsing keeps the
    # template stable when a task names the same thing in two words.
    return re.sub(r"<TERM>(\s+<TERM>)+", "<TERM>", stripped)


def leaked_subject_terms(text: str, source: str, target: str) -> frozenset[str]:
    """Subject-matter words in `text` that belong to `source` but not `target`.

    WHY THIS EXISTS. `_LITERAL_KINDS` deliberately excludes TERM (see its own
    comment): `rebind` rewrites URL/PATH/DATE/MONEY/PERCENT/QUOTED/NUMBER
    tokens onto a new task's literals but leaves an ordinary noun like "pens"
    exactly as the source task wrote it. That is correct for PLAN TEXT a human
    reads for sense -- but a memo's CRITERION carries CHECK PARAMETERS a
    machine compares byte-for-byte (`contains_all`, `regex_match`,
    `stdout_contains`), and those are exactly as likely to spell out the
    subject noun as any other string a proposer writes. A rebound criterion
    that still says "pens" can never be satisfied by a correct answer about
    pencils, and unlike a garbled or degenerate criterion, `synthesis.attack`
    cannot catch it: attack only tries degenerate candidates, none of which
    happen to contain the one specific word a genuinely correct answer never
    would.

    This is `strip_source_terms`'s other half. That function SCRUBS a step's
    surviving nouns before a human ever reads them into a skill library; this
    one DETECTS when a rebound criterion still carries one, so `swarm/run.py`
    can refuse the near-tier hit rather than trust a criterion that no
    candidate on the new subject could ever pass. Same vocabulary rule as
    `strip_source_terms` -- method and function words never count, because
    "total" appearing in both tasks is not evidence of anything -- and
    deliberately NOT stemmed, for the same reason `strip_source_terms` is not:
    a plural surviving as a singular (or the reverse) is a smaller, different
    leak than this function exists to catch, and folding the two together
    would refuse pairings that rebind perfectly clean.
    """
    def subject_stems(phrase: str) -> set[str]:
        return {
            _stem(w) for w in _WORD.findall(phrase.lower())
            if w not in METHOD_LEXICON and w not in FUNCTION_WORDS
        }

    # COMPARED AS STEMS, not as surface forms. Comparing what the two tasks
    # literally wrote let an ordinary singular/plural paraphrase walk straight
    # through the guard: a criterion rebound from a task about `pens` onto one
    # about `pen` had nothing "only in the source" to find, so a check
    # parameter still naming the source's subject was reported as clean. The
    # guard is about whether the SUBJECT survived the rebind, and `pen` is the
    # same subject as `pens`.
    only_source = subject_stems(source) - subject_stems(target)
    if not only_source:
        return frozenset()
    return frozenset(
        w for w in _WORD.findall(text.lower()) if _stem(w) in only_source
    )


def render_pattern(template: str) -> str:
    """`<NUMBER>` -> `slot_number`, so the retrieval tokenizer can see it.

    `SkillLibrary.tokenize` matches `[a-z0-9_]+`, which drops angle brackets
    and leaves the bare word "number" -- indistinguishable from a task that
    genuinely says "number". A `slot_` prefix cannot collide with English.
    """
    return _PLACEHOLDER.sub(lambda m: f"slot_{m.group(0)[1:-1].lower()}", template)


def merge_templates(a: str, b: str) -> str:
    """What two abstracted steps have in common, with the rest collapsed.

    Token-level longest common subsequence. Every non-matching run becomes one
    `<TERM>`, so the result is literally what survived across two different
    tasks rather than a heuristic guess at what might.

        "compute the total cost of <NUMBER> pens at <MONEY> each"
        "compute the total cost of <NUMBER> pencils at <MONEY> each"
     -> "compute the total cost of <NUMBER> <TERM> at <MONEY> each"
    """
    left = [t if _PLACEHOLDER.fullmatch(t) else t.lower() for t in a.split()]
    right = [t if _PLACEHOLDER.fullmatch(t) else t.lower() for t in b.split()]

    rows, cols = len(left), len(right)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            table[i][j] = (
                table[i + 1][j + 1] + 1
                if left[i] == right[j]
                else max(table[i + 1][j], table[i][j + 1])
            )

    out: list[str] = []
    i = j = 0
    gap = False
    while i < rows and j < cols:
        if left[i] == right[j]:
            if gap:
                out.append("<TERM>")
                gap = False
            out.append(left[i])
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            gap = True
            i += 1
        else:
            gap = True
            j += 1
    if gap or i < rows or j < cols:
        out.append("<TERM>")
    return " ".join(out)


def content_tokens(text: str) -> list[str]:
    """The words that carry subject matter: no placeholders, no method words.

    Numbers are excluded because they are handled by `shared_literals`, which
    refuses them outright rather than scoring them -- a shared number is never
    acceptable at any generality.
    """
    without_slots = _PLACEHOLDER.sub(" ", text)
    without_slots = re.sub(r"\bslot_[a-z]+\b", " ", without_slots.lower())
    return [
        token
        for token in _WORD.findall(without_slots)
        if token not in METHOD_LEXICON and token not in FUNCTION_WORDS
    ]


def generality(instruction: str, source_texts: tuple[str, ...]) -> float:
    """Fraction of an instruction's content vocabulary NOT taken from its source.

    1.0 when nothing subject-specific survives -- including when the
    instruction is entirely method words and placeholders, which is the shape a
    fully generalised step has. Scoring that 0.0 (nothing general because
    nothing at all) would reject exactly the instructions this module exists
    to produce.

    The observed bug string scores 0.5 against its own two near-duplicate
    phrasings: `calculate_pen_cost` is novel, `pens` is not.
    """
    tokens = content_tokens(instruction)
    if not tokens:
        return 1.0
    vocabulary: set[str] = set()
    for source in source_texts:
        vocabulary.update(content_tokens(source))
    novel = [token for token in tokens if token not in vocabulary]
    return len(novel) / len(tokens)


# A word, a literal placeholder, or a mark of punctuation. Punctuation is a
# token here because it ENDS a noun phrase: "3 pens -- compute the total" must
# not read "pens compute" as one phrase whose head is a verb.
#
# A dot only stays inside a word when a letter or digit follows it, so
# `csv.dictreader` survives as one token while the full stop closing a sentence
# is punctuation. Without that, `"...the total cost."` ends in the word
# `"cost."`, which is not `"cost"`, so it is not method vocabulary, so a
# trailing full stop mints a second task shape.
_SHAPE_TOKEN = re.compile(r"<[A-Z]+>|[a-z][a-z0-9_'-]*(?:\.[a-z0-9_'-]+)*|[^\w\s]")


@dataclass(frozen=True, slots=True)
class TaskShape:
    """What a task DOES, read off its structure rather than its wording.

    Three parts, each answering a different question, and none of them holding
    a word the task could not have shared with any other task on the subject:

      slots     the SEQUENCE of literal kinds. Ordered, because rebinding one
                task's stored artefacts onto another's literals is only
                well-defined when the two carry the same kinds in the same
                order.
      subjects  the head nouns of the task's noun phrases, minus method
                vocabulary and folded to singular by `_stem`. What the
                question is ABOUT -- "3 pens" and "1 pen" are the same thing
                to be about, so the count is `slots`' job, not this one's.
      actions   the method vocabulary the task uses. What the question DOES to
                its subject.
    """

    slots: tuple[str, ...]
    subjects: tuple[str, ...]
    actions: tuple[str, ...]

    @property
    def signature(self) -> str:
        """The evidence bar's unit: subject matter plus the kinds of literal."""
        payload = (
            f"{'|'.join(sorted(set(self.slots)))}"
            f"||{'|'.join(sorted(set(self.subjects)))}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        """The near-match index: the same work on a different subject.

        Subjects appear only as a COUNT. That is the whole difference from
        `signature`: pens and pencils are two different questions (two pieces
        of evidence) and one shape of work (one memo, rebindable from either
        onto the other).
        """
        payload = (
            f"{'|'.join(self.slots)}"
            f"||{'|'.join(sorted(set(self.actions)))}"
            f"||{len(set(self.subjects))}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _head(run: list[str]) -> str:
    """The last content word of a noun phrase -- the noun the rest modifies.

    Taking the head rather than the whole run is what makes "the total cost"
    and "the overall cost" one shape: an adjective swapped in front of a noun
    changes how the question reads and not what it is about. Folded through
    `_stem` for the same reason "3 pens" and "1 pen" must be one subject: a
    plural is a grammatical fact about the count, not a second thing the task
    is about, and the count itself already lives in `slots`.
    """
    return _stem(run[-1]) if run else ""


# The spelled-out cardinals, folded to digits for SHAPE purposes only.
#
# WHY ONLY FOR SHAPE. `abstract()` also renders distilled skill instructions,
# where a small number is usually method guidance -- "write one paragraph"
# means what it says, and rewriting it to "write <NUMBER> paragraph" destroys
# the advice. But a task SHAPE that separates "three pens" from "3 pens" is a
# FARM: one task, written twice, clears the two-distinct-shapes bar without
# anyone solving a second task. Folding here and nowhere else serves both.
#
# WHY A WORD LIST IS LEGITIMATE HERE, having rejected one for filler adverbs:
# the cardinals are a CLOSED class. There are finitely many ways to write a
# small number in English and the list cannot go stale, whereas there is no
# bound on the ways to pad a sentence. Above twenty English composes
# ("twenty-one"), which the alternation covers piecewise.
_CARDINALS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90",
}
_CARDINAL_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_CARDINALS, key=len, reverse=True)) + r")\b"
)


def _digits_for_shape(text: str) -> str:
    """Rewrite cardinal WORDS as digits so both spellings reach one shape.

    Runs before `abstract`, so the ordinary NUMBER rules -- including
    `_keeps_literal`, which spares a number that IS the method -- then apply
    to both spellings identically: "round to two decimal places" and "round to
    2 decimal places" are both kept, because "decimal" is what decides.
    """
    return _CARDINAL_RE.sub(lambda m: _CARDINALS[m.group(0)], text)


@functools.lru_cache(maxsize=4096)
def task_shape(task: str) -> TaskShape:
    """Read a task's structure: its literals, its subjects, its method.

    ANATOMY: why position and not a word list
      The previous rule counted every content token as subject matter and
      subtracted a list of words that did not count. A list is a blocklist, and
      a blocklist loses: `just`, `now`, `quickly`, `simply` and `approximately`
      each minted a second task shape until someone noticed and added it, and
      the supply of filler adverbs is not finite. English has no closed class
      of them to enumerate.

      What IS closed is the set of words that open a noun phrase -- determiners
      and prepositions -- so this asks where a word sits instead of what it is.
      Subject matter is the HEAD of a phrase introduced by one of those or by a
      literal ("<NUMBER> pens"). An adverb dropped in front of the verb, at the
      end of the sentence, or between the two sits in no phrase at all and is
      never read, whatever it happens to be spelled.

    ANATOMY: which way it fails
      Also residual: `_stem`'s bare-`s`-before-`es` tie-break (see its own
      docstring) leaves a small, named class of subject nouns unfolded --
      short bare-sibilant loanwords (`bus`/`buses`, `gas`/`gases`) and longer
      Latin/Greek ones whose singular already ends in `s` (`lens`, `virus`,
      `atlas`...). Toward SPLITTING, which is the direction this module's own
      standard treats as the dangerous one; disclosed rather than fixed
      because no suffix rule resolves it without a lexicon this module
      deliberately does not keep.

      Otherwise toward collapsing, deliberately, exactly as before. A
      determiner-less fronted noun phrase ("Pens: compute the total cost of 3
      at 1.25 each") contributes no subject from the head-of-phrase pass --
      a clause-initial run is not read as a phrase, because an unknown VERB
      starts one too ("determine the total cost...") and reading those as
      subjects would let a synonym for "compute" mint a second shape, the
      cheapest farm of all -- but the `if not subjects` fallback below then
      reads the whole template for content words, so the fronted phrasing
      still collapses onto the ordinary one instead of splitting from it.

      The fallback's own residual is narrower: it keeps every content word
      and cannot tell a verb it does not recognise from a noun. It runs
      whenever the head-of-phrase pass leaves `subjects` empty -- which is
      not only "no determiner anywhere": a determiner-introduced phrase whose
      head word is itself in `METHOD_LEXICON` contributes nothing either
      (`flush` filters it), so "count the count" hits the fallback too even
      though it has a determiner. Either way, once the fallback runs, a verb
      outside `METHOD_LEXICON` is read as subject matter and a synonymous
      verb mints a second shape
      (`task_signature("tally widgets") != task_signature("count widgets")`).
      Pinned by `test_the_fallback_leaves_a_narrower_residual_and_this_is_it`;
      a failure there means a word needs adding to `METHOD_LEXICON`, not a
      widening of the fallback.
    """
    # Whitespace and case folded HERE rather than by importing `memo.normalise`
    # -- this module deliberately imports nothing from swarmd, and borrowing
    # the memo's key discipline is what produced the defect above. Casefolding
    # first also means TERM never fires on a task, so a re-typed capital
    # cannot become a second shape.
    template = abstract(_digits_for_shape(" ".join(task.split()).casefold())).template
    slots: list[str] = []
    subjects: list[str] = []
    actions: list[str] = []
    run: list[str] = []
    # Is the phrase being accumulated one that a determiner, a preposition or a
    # literal opened? Only those are read; anything else is discarded on flush.
    introduced = False

    def flush() -> None:
        head = _head(run)
        if introduced and head and head not in METHOD_LEXICON:
            subjects.append(head)
        run.clear()

    for token in _SHAPE_TOKEN.findall(template):
        if token.startswith("<"):
            flush()
            slots.append(token[1:-1])
            introduced = True
        elif token in INTRODUCERS:
            flush()
            introduced = True
        elif token in FUNCTION_WORDS or not token[0].isalpha():
            # Grammar that does not open a phrase, or punctuation. Both END the
            # phrase in progress without starting one.
            flush()
            introduced = False
        else:
            if token in METHOD_LEXICON:
                actions.append(token)
            run.append(token)
    flush()
    if not subjects:
        # No phrase contributed a head: either nothing was introduced (no
        # determiner, preposition or literal anywhere), or every phrase that
        # was introduced had a head word that `flush` filtered because it is
        # itself in METHOD_LEXICON ("the count" heads on "count"). Either way
        # there is no subject to return. Rather than return none -- which
        # merges every such task onto one signature and splits each from its
        # own ordinary phrasing -- fall back to the content words: whatever is
        # left once the method vocabulary, the function words and the slots
        # are removed.
        #
        # Deliberately a FALLBACK and not the primary rule. Head-of-phrase is
        # the better signal when it is available, because it ignores the
        # adjectives that decorate a noun without changing what the task is
        # about; this coarser pass exists only so a phrasing the parser cannot
        # read produces a weaker signature rather than an absent one.
        subjects = [
            _stem(token)
            for token in _SHAPE_TOKEN.findall(template)
            if not token.startswith("<")
            and token[0].isalpha()
            and token not in METHOD_LEXICON
            and token not in FUNCTION_WORDS
            and token not in INTRODUCERS
        ]
    return TaskShape(tuple(slots), tuple(subjects), tuple(actions))


def task_signature(task: str) -> str:
    """What a task is ABOUT, as a fingerprint. The unit the evidence bar counts.

    ANATOMY: why not the abstracted sentence
      The evidence bar (`skills.MIN_DISTINCT_TASKS`) exists to tell "this
      approach transfers" from "this is one task drawn twice". It was keyed on
      `abstract(memo.normalise(task)).fingerprint` -- the abstracted SENTENCE,
      hashed. That inherits `memo.normalise`'s deliberate property, stated in
      its own docstring: "a paraphrase MISSES, and that is the correct
      outcome". Correct for an exact-match cache, where a miss costs six
      proposer calls. Exactly wrong here, where a miss MANUFACTURES the second
      piece of evidence: asking the same question twice, once with "please"
      and "for me" around it, produced two fingerprints and pushed a candidate
      to a human as if a second, independent task had confirmed it.

      So the signature is not the sentence. It is the pair that survives
      rewording: the SUBJECT MATTER (`TaskShape.subjects` -- the head nouns of
      the task's noun phrases, order-independent and folded to singular) and
      the KINDS OF LITERAL the question carries. "Compute the total cost of 3
      pens at 1.25 dollars each", "please just compute the total cost of 3
      pens at 1.25 dollars each for me quickly", "AT 1.25 DOLLARS EACH, 3 PENS
      -- COMPUTE THE TOTAL COST" and "calculate the cost of 1 pen at 1.25
      dollars" are one task: subject `{pen}`, literals `{MONEY, NUMBER}`.

    ANATOMY: which way it fails
      Deliberately toward collapsing. Two genuinely different questions about
      the same subject with the same literal kinds ("count the pens" / "list
      the pens") share a signature, and the cost of that is a promotion that
      does not happen -- a candidate keeps accruing evidence and no human is
      asked yet. The opposite error, splitting one task into two shapes, is
      the farming channel itself. Method verbs are excluded from the subject
      for the same reason: including them would let a synonym swap
      ("compute" -> "calculate") mint a second task shape.

      One disclosed exception sits on the wrong (splitting) side: `_stem`'s
      bare-`s`-before-`es` tie-break folds the common `-se` subject class
      correctly (house/case/database/response/...) at the cost of a few short
      bare-sibilant loanwords (bus, gas) and unfixably ambiguous ones (lens,
      virus, atlas...) that still mint two signatures across singular and
      plural. See `_stem`'s docstring for why: it is a lexicon-shaped problem
      and this module keeps no lexicon.

      Not a hash of anything readable: the fingerprint is stored on a Skill,
      and the same minimisation that keeps literals out of `instruction` and
      `task_pattern` keeps the task's own words out of its evidence.
    """
    return task_shape(task).signature


def abstract_fingerprint(task: str) -> str:
    """The index for "a task of the same SHAPE", not the same subject.

    `task_signature` splits pens from pencils, because two subjects are two
    pieces of evidence. This joins them, because one stored criterion and plan
    can serve both once its literals are rebound -- which is the whole of the
    memo's near-match tier. Nothing is served on a fingerprint match alone:
    see `swarm/run.py`, where the rebound criterion is re-attacked against the
    new task before it may grade anything.
    """
    return task_shape(task).fingerprint


def literal_map(source: str, target: str) -> tuple[tuple[str, str], ...] | None:
    """How to read one task's literals as another's, or None if they cannot be.

    Positional and kind-checked: the Nth literal of the source becomes the Nth
    literal of the target, and only when every kind matches in order. Anything
    looser would rewrite a date into a price. `None` means "these two tasks do
    not line up", which is the answer that makes the caller pay for its own
    synthesis rather than guess.

    Longest source form first, so `"1.25 dollars"` is rewritten before the
    `"1.25"` inside it and the currency word is not left stranded.
    """
    src = [(kind, value) for kind, value, _s, _e in _scan(source)]
    tgt = [(kind, value) for kind, value, _s, _e in _scan(target)]
    if [k for k, _ in src] != [k for k, _ in tgt]:
        return None
    pairs: dict[str, str] = {}
    for (kind, old), (_kind, new) in zip(src, tgt, strict=True):
        pairs[old] = new
        if kind in ("MONEY", "PERCENT"):
            # The bare numeric core, so a criterion that wrote the price
            # without its currency word is rebound too -- the same notation
            # equivalence `_literal_forms` enforces on the leak check.
            old_core = re.search(r"\d[\d,]*(?:\.\d+)?", old)
            new_core = re.search(r"\d[\d,]*(?:\.\d+)?", new)
            if old_core is not None and new_core is not None:
                pairs.setdefault(old_core.group(0), new_core.group(0))
    return tuple(sorted(pairs.items(), key=lambda p: -len(p[0])))


def rebind(text: str, mapping: tuple[tuple[str, str], ...]) -> str:
    """Rewrite one task's literals as another's, matching whole tokens only.

    Whole tokens for the same reason `literals` compares them that way: `"2"`
    inside `"1.25"` is not the number 2, and a substring rewrite would turn a
    price into rubble.
    """
    for old, new in mapping:
        text = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w.])", new, text)
    return text


# ANATOMY: corroborate
#   The one place distillation VERIFIES instead of trusting (ADR-015 named this
#   as the deeper fault: everything else in this system is criterion-first, and
#   a skill's claim -- "this approach transfers" -- went untested until it was
#   already in worker prompts).
#
#   A skill only reaches a human once two DISTINCT task shapes have produced
#   it. That means two independently distilled instructions exist for the same
#   approach, and the difference between them is exactly the part that came
#   from one task rather than from the method. Domain-as-method contamination
#   -- `probes`, `monitoring`, `stock levels` -- is one task's vocabulary by
#   construction, so it cannot appear in the other variant unless the other
#   task genuinely called for it too, in which case it is not contamination.
#
#   Deterministic, threshold-free, and it uses evidence the promotion bar
#   already requires. It is NOT a classifier: nothing here decides whether a
#   word is domain vocabulary. It keeps what more than one task attested and
#   drops what only one did.
#
#   Method words and function words are kept unconditionally. They are the
#   vocabulary the abstraction already treats as task-independent, so
#   intersecting them would only shred the sentence without removing anything
#   a task contributed.
_SEGMENT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*|[^A-Za-z]+")
_DANGLING = re.compile(r"\s+([,.;:])")
_REPEATED_COMMA = re.compile(r",(\s*,)+")
_SENTENCE = re.compile(r"[^.!?]*[.!?]|[^.!?]+")


def corroborate(variants: Sequence[str]) -> str:
    """Keep only the words EVERY variant of this advice used.

    `variants` are instructions distilled independently for the same approach
    from different task shapes. With fewer than two there is nothing to
    corroborate and the text is returned unchanged -- deliberately, because
    a single variant intersected with itself would look verified and is not.
    """
    kept_variants = [v for v in variants if v.strip()]
    if len(kept_variants) < 2:
        return kept_variants[0].strip() if kept_variants else ""

    attested: set[str] | None = None
    for variant in kept_variants:
        stems = {_stem(token) for token in content_tokens(variant)}
        attested = stems if attested is None else (attested & stems)
    assert attested is not None

    # Sentence by sentence, because a sentence that loses every word carrying
    # meaning should GO rather than survive as a string of connectives. That
    # case is the "structured parts only" instruction ADR-015 describes: the
    # advice keeps the shape two tasks proved and drops the prose one task
    # supplied.
    out = [
        rebuilt
        for sentence in _SENTENCE.findall(kept_variants[0])
        if (rebuilt := _corroborate_sentence(sentence, attested))
    ]
    return " ".join(out).strip()


def _corroborate_sentence(sentence: str, attested: set[str]) -> str:
    kept: list[str] = []
    substantive = False
    for segment in _SEGMENT.findall(sentence):
        if not segment[:1].isalpha():
            kept.append(segment)
            continue
        lowered = segment.lower()
        if lowered in METHOD_LEXICON or _stem(lowered) in attested:
            substantive = True
        elif lowered not in FUNCTION_WORDS:
            continue
        elif not substantive:
            # A connective with nothing yet in front of it is debris left by
            # the word that was dropped -- "and to collect" out of "probes and
            # monitoring to collect". Function words earn their place by
            # joining things that survived, not by being harmless.
            continue
        kept.append(segment)
    if not substantive:
        return ""
    # Removing a word leaves the punctuation that separated it. Tidied rather
    # than left, because this string is read by a human at the approval gate
    # and written into a worker prompt, and both are worse for the debris.
    text = "".join(kept)
    text = _REPEATED_COMMA.sub(",", text)
    text = _DANGLING.sub(r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip().lstrip(" ,;:-").strip()
