"""Abstraction: the contract that decides what a later run is allowed to read.

Every function here runs on the only write path this system has back into its
own inputs, so its failure mode is not a wrong answer once -- it is one task's
numbers injected into every future worker prompt that retrieves the skill they
ended up in.

Three properties carry the weight, and each has a fixture that would have
caught the bug that actually happened:

  LITERALS NEVER SURVIVE   `1.25` and any quoted string are always replaced.
  METHOD SURVIVES          `round to 2 decimal places` and `csv.DictReader`
                           are advice, not data, and must come through intact.
                           An abstraction that eats them is useless, which is
                           how a redaction rule gets switched off in practice.
  SAME SHAPE, SAME KEY     two tasks differing only in their literals abstract
                           to the same fingerprint, on any machine, in any
                           order -- otherwise retrieval and evidence-counting
                           silently disagree about what "the same task" means.
"""

from __future__ import annotations

from swarmd.swarm.generalise import (
    MIN_GENERALITY,
    _identifier_parts,
    _stem,
    abstract,
    content_tokens,
    generality,
    leaked_subject_terms,
    literals,
    merge_templates,
    render_pattern,
    shared_literals,
    strip_source_terms,
    task_signature,
)

PEN = "Compute the total cost of 3 pens at 1.25 dollars each"
PENCIL = "Compute the total cost of 7 pencils at 2.50 dollars each"

# The string the library actually stored, offered to every future run as a
# general method. It is the regression fixture for this whole module.
BUG = "calculate_pen_cost: Compute the total cost of 3 pens at 1.25 dollars each"


# --- literals never survive --------------------------------------------------


def test_numbers_and_prices_are_replaced_by_their_kind():
    """A number in a step is one task's data wearing a method's grammar.

    `3` and `1.25` are the whole of what a later run must not inherit: it is
    about pencils at a different price, and being handed these and told they
    worked is worse than being told nothing.
    """
    template = abstract(PEN).template
    assert "3" not in template
    assert "1.25" not in template
    assert "<NUMBER>" in template and "<MONEY>" in template


def test_a_quoted_string_is_always_replaced():
    """Quoted text is the other shape a literal answer arrives in -- a required
    field name, an expected phrase, a sample value someone pasted in."""
    assert abstract('the field named "total_pens" is required').template == (
        "the field named <QUOTED> is required"
    )


def test_two_tasks_differing_only_in_literals_share_a_fingerprint():
    """This is what makes "distinct task" countable.

    If number-swapped near-duplicates had different fingerprints, a generator
    emitting the same question with new digits would manufacture unlimited
    "distinct task" evidence and the promotion bar would mean nothing.
    """
    assert abstract(PEN).fingerprint == abstract(
        "Compute the total cost of 9 pens at 4.75 dollars each"
    ).fingerprint


def test_a_different_noun_is_a_different_shape():
    """The boundary in the other direction: pens and pencils differ by a word
    that is not a literal, so they are two tasks rather than one restated."""
    assert abstract(PEN).fingerprint != abstract(PENCIL).fingerprint


def test_abstraction_is_deterministic():
    """Retrieval keys and evidence keys are compared across processes. An
    abstraction that depended on iteration order would make two machines
    disagree about whether they had seen a task before."""
    assert abstract(PEN) == abstract(PEN)
    assert abstract(PEN).fingerprint == abstract(str(PEN)).fingerprint


# --- method survives ---------------------------------------------------------


def test_a_number_that_is_the_method_survives():
    """"Round to 2 decimal places" is advice; the 2 is not an answer.

    Replacing it would leave "round to <NUMBER> decimal places", which tells a
    worker to round to nothing in particular -- a redaction rule that destroys
    the instruction it is protecting gets turned off, and then nothing is
    protected.
    """
    assert abstract("round to 2 decimal places").template == "round to 2 decimal places"
    assert abstract("report to 3 significant digits").slots == ()


def test_a_dotted_identifier_is_not_a_literal():
    """`use csv.DictReader with an explicit dialect` is the repo's own example
    of a good terse skill. Slotting the class name would turn the one thing
    worth saying into a placeholder."""
    text = "use csv.DictReader with an explicit dialect"
    assert abstract(text).template == text


def test_a_capitalised_method_verb_is_not_treated_as_a_name():
    """Sentence case is why this exists. "Compute" opens a step with a capital
    letter, and slotting it would delete the verb the instruction is about
    while carefully preserving the noun it is not."""
    assert abstract("Compute the total").template == "Compute the total"


def test_a_proper_noun_is_treated_as_a_name():
    """The same rule has to bite when the capitalised word is subject matter,
    or it is not a rule, it is a pass."""
    assert "<TERM>" in abstract("summarise the Helsinki filings").template


# --- source-term stripping ---------------------------------------------------


def test_a_bare_noun_from_the_task_is_stripped():
    """`abstract` cannot see this one. "pens" is a lowercase common noun,
    structurally identical to "total" -- the only thing that marks it as this
    task's subject matter is that it came from this task."""
    stripped = strip_source_terms(abstract(PEN).template, (PEN,))
    assert "pens" not in stripped
    assert stripped == "Compute the total cost of <NUMBER> <TERM> at <MONEY> each"


def test_method_vocabulary_is_never_stripped():
    """A step reduced to placeholders joined by grammar teaches nothing, so the
    words that describe HOW must survive even though they appear in the task."""
    stripped = strip_source_terms(abstract(PEN).template, (PEN,))
    for word in ("Compute", "total", "cost", "each"):
        assert word in stripped


def test_a_word_the_task_never_used_survives():
    """Stripping is scoped to the source. A step naming a technique the task
    did not mention is the most valuable thing distillation can capture."""
    step = "parse the ledger with csv.DictReader"
    assert "csv.DictReader" in strip_source_terms(step, ("summarise the source records",))


def test_stripping_with_no_source_changes_nothing():
    """No task, no claim. A caller without source text gets shape abstraction
    only, rather than a silent pretence that a check was performed."""
    assert strip_source_terms("compute the pens total", ()) == "compute the pens total"


# --- retrieval keys ----------------------------------------------------------


def test_a_placeholder_becomes_a_token_the_tokenizer_can_see():
    """`SkillLibrary.tokenize` matches [a-z0-9_]+, which drops angle brackets
    and leaves the bare word "number" -- indistinguishable from a task that
    genuinely says "number". `slot_` cannot collide with English."""
    assert render_pattern("<NUMBER> pens at <MONEY>") == "slot_number pens at slot_money"


def test_rendering_a_pattern_is_idempotent():
    """The pattern is abstracted at proposal time and again at retrieval time.
    If the second pass changed it, a stored skill would stop matching itself."""
    once = render_pattern(abstract(PEN).template)
    assert render_pattern(abstract(once).template) == once


# --- merging -----------------------------------------------------------------


def test_merging_two_shapes_keeps_only_what_both_said():
    """The spec's worked example, and the only honest way to generalise: what
    survives is literally what two different tasks had in common, not a
    heuristic guess at which word was the incidental one."""
    merged = merge_templates(
        "Compute the total cost of <NUMBER> pens at <MONEY> each",
        "Compute the total cost of <NUMBER> pencils at <MONEY> each",
    )
    assert merged == "compute the total cost of <NUMBER> <TERM> at <MONEY> each"


def test_merging_unrelated_steps_collapses_to_almost_nothing():
    """Two steps with nothing in common must not produce a confident-looking
    instruction. Collapsing to placeholders is the honest output."""
    assert merge_templates("write one paragraph", "verify the totals") == "<TERM>"


def test_merging_is_commutative_in_content():
    """Evidence arrives in whatever order runs finish. A merge that depended on
    argument order would make the distilled skill depend on scheduling."""
    a = merge_templates("compute the <NUMBER> total", "compute the <NUMBER> subtotal")
    b = merge_templates("compute the <NUMBER> subtotal", "compute the <NUMBER> total")
    assert a == b


# --- generality --------------------------------------------------------------


def test_the_observed_bug_string_scores_below_the_floor():
    """THE ONE THAT HAPPENED, scored.

    Against two phrasings of its own task, almost nothing in it is novel:
    "pens" and "dollars" came straight from the question, and only the node
    name did not. A method that is 33% new words is a task restated.
    """
    score = generality(BUG, (PEN, PENCIL))
    assert score < MIN_GENERALITY
    assert score == 1 / 3


def test_a_fully_abstracted_step_scores_one():
    """Nothing subject-specific survived, which is the shape a generalised step
    has. Scoring it 0.0 -- nothing general because nothing at all -- would
    reject exactly the instructions this module exists to produce."""
    assert generality("compute the total cost of <NUMBER> <TERM> at <MONEY>", (PEN,)) == 1.0


def test_a_novel_technique_scores_above_the_floor():
    """The positive case: advice naming something the task never mentioned."""
    assert generality("parse it with csv.DictReader", (PEN,)) >= MIN_GENERALITY


def test_a_short_subject_word_survives_stripping_and_is_still_scored():
    """Why the generality check is not redundant with stripping.

    `strip_source_terms` only replaces tokens of three characters or more, so a
    short subject word comes through untouched. Scoring the STRIPPED step is
    what catches that -- the post-condition exists precisely because the
    stripping rule has an edge it cannot reach, and a defence that assumes its
    own completeness is not a defence.
    """
    task = "report the co2 total for 3 sites"
    step = strip_source_terms(abstract("report the co2 total").template, (task,))
    assert "co2" in step
    assert generality(step, (task,)) < MIN_GENERALITY


def test_content_tokens_ignore_placeholders_and_method_words():
    """Otherwise every instruction would score itself general on the strength
    of the word "compute", which every instruction contains."""
    assert content_tokens("compute the <TERM> slot_number pens") == ["pens"]


# --- shared literals ---------------------------------------------------------


def test_a_shared_number_is_detected():
    """The gate `validate_instruction` uses. A number appearing in both the
    advice and the task it came from is that task's answer, always."""
    assert shared_literals("always start from 1.25 per unit", PEN) == {"1.25"}


def test_a_literal_is_matched_whole_not_as_a_substring():
    """`2` is not a leak of `1.25` because the digit appears inside it. A
    substring check here would refuse correct instructions about decimal
    places, and a rule that fires on correct input gets removed."""
    assert shared_literals("round to 2 decimal places", PEN) == set()


def test_literals_are_normalised_before_comparison():
    """Thousands separators are formatting, not identity: an instruction
    carrying `1,250` is carrying the task's `1250`."""
    assert "1250" in literals("the baseline is 1,250")


# --- a number the method owns is not a value ---------------------------------


def test_a_method_number_is_not_a_literal_at_all():
    """`literals` and `abstract` have to agree about what a value IS.

    They did not. `abstract` skips a number whose following word makes it part
    of the method -- "round to 2 decimal places" -- while `literals` re-read
    the text with one independent regex per kind, and an independent read has
    no context to skip on. So the same `2` was simultaneously "not a literal"
    (in the advice that got stored) and "a literal" (in the check that decides
    whether storing it is a leak), which is a contradiction that can only ever
    resolve against a correct instruction.
    """
    assert literals("round to 2 decimal places") == set()
    assert literals("report to 3 significant digits") == set()


def test_a_method_number_is_not_a_leak_even_when_the_task_shares_the_digit():
    """The collision the two-readings bug needed, and the reason it was
    invisible: it only fires when the task happens to contain the same small
    digit the instruction rounds to. `2 pens` and `2 decimal places` have
    nothing to do with each other, and refusing the instruction over it drops
    a correct skill silently -- `_distill` catches the refusal in a blanket
    `except`, so nothing is recorded and nothing is reported.
    """
    task = "Compute the total cost of 2 pens at 1.25 dollars each"
    assert shared_literals("Compute the total and round to 2 decimal places", task) == set()


def test_a_price_still_leaks_in_whatever_notation_it_is_rewritten_in():
    """The strictness that must NOT be lost while fixing the above.

    Money has three spellings for one amount. An instruction that restates the
    task's price as a bare number is carrying the task's answer just as surely
    as one that repeats "1.25 dollars", so the bare numeric core of a price
    counts as the same literal.
    """
    assert "1.25" in shared_literals("always start from 1.25 per unit", PEN)
    assert "10" in shared_literals("apply 10 percent", "add a 10% surcharge")


# --- leaked subject terms: the near-tier's other half ------------------------
#
# `rebind` deliberately never touches TERM -- an ordinary noun like "pens" is
# not one of `_LITERAL_KINDS`, because a plan's human-facing text is supposed
# to keep its words. A CHECK PARAMETER is not human-facing text, though: it is
# a string a grader compares byte-for-byte, and one that still says "pens"
# after being rebound onto a pencils task can never be satisfied by any
# correct answer. `leaked_subject_terms` is what lets `swarm/run.py` catch
# that before trusting a rebound criterion -- see `_criterion_from_near_memo`.


def test_a_surviving_subject_word_is_reported_as_leaked():
    """The exact shape of the bug: a criterion built for PEN still says "pens"
    after being rebound onto a task about pencils, and this is the function
    that has to say so."""
    criterion_text = '{"checks": [{"params": {"substrings": ["pens", "verified"]}}]}'
    assert leaked_subject_terms(criterion_text, PEN, PENCIL) == {"pens"}


def test_a_word_only_the_criterion_added_is_not_a_leak():
    """"verified" never came from PEN, so it is not PEN's leftover -- it is
    part of what the criterion is actually checking, and flagging it would
    refuse a rebind that was never at fault."""
    criterion_text = '{"checks": [{"params": {"substrings": ["verified"]}}]}'
    assert leaked_subject_terms(criterion_text, PEN, PENCIL) == set()


def test_a_word_both_tasks_share_is_not_a_leak():
    """"total" and "cost" are method vocabulary, shared by construction, and
    "dollars" and "each" are shared by this fixture pair on purpose: a word
    the TARGET task itself contains is not evidence the rebind failed."""
    criterion_text = '{"checks": [{"params": {"substrings": ["cost", "dollars"]}}]}'
    assert leaked_subject_terms(criterion_text, PEN, PENCIL) == set()


def test_no_source_only_vocabulary_means_nothing_can_leak():
    """When PEN and PENCIL happen to share every content word (a degenerate
    fixture, not this pair), there is nothing left to leak -- the empty first
    return short-circuits before `text` is even scanned."""
    assert leaked_subject_terms("anything at all, even pens", PEN, PEN) == set()


# --- what counts as a second task --------------------------------------------


def test_the_same_question_asked_politely_is_the_same_task():
    """THE FARMING CASE. The evidence bar counts distinct task shapes, so
    whatever it counts by decides whether the bar means anything.

    Keyed on the abstracted SENTENCE, this passes as two tasks: "please" and
    "for me" are not literals, so nothing collapses them, and one question
    asked twice reaches a human as if a second independent task had confirmed
    the approach. That is the whole feature defeated by politeness.
    """
    assert task_signature(PEN) == task_signature(
        "Please compute the total cost of 3 pens at 1.25 dollars each for me"
    )


def test_word_order_and_capitalisation_do_not_make_a_second_task():
    """The same defect in its other two disguises. A signature is a set of
    subject words and literal kinds precisely so that rearranging the sentence
    around them changes nothing."""
    assert task_signature(PEN) == task_signature(
        "AT 1.25 DOLLARS EACH, 3 PENS -- COMPUTE THE TOTAL COST."
    )


def test_a_synonym_for_the_method_does_not_make_a_second_task():
    """Method vocabulary is excluded from the signature, which closes the
    cheapest farming channel of all: swapping "compute" for "calculate" would
    otherwise mint a second task shape without changing the question."""
    assert task_signature(PEN) == task_signature(
        "Calculate the total cost of 3 pens at 1.25 dollars each"
    )


def test_a_task_about_something_else_is_a_different_task():
    """The bar has to be clearable, or the feature is just a refusal. Pens and
    pencils differ in what they are ABOUT, which is the only difference that
    should count as a second observation."""
    assert task_signature(PEN) != task_signature(PENCIL)


def test_a_question_carrying_different_kinds_of_literal_is_a_different_task():
    """Subject matter alone would collapse every question about pens into one.
    The kinds of literal a task carries -- a price, a date, a count -- are part
    of what makes it a different question about the same thing."""
    assert task_signature(PEN) != task_signature("List the pens in the order given")


def test_a_signature_holds_no_word_of_the_task_it_came_from():
    """It is stored on a Skill and read by whoever can read the library. The
    same minimisation that keeps literals out of the advice and the index: a
    record that cannot hold a task's words cannot leak one."""
    signature = task_signature(PEN)
    assert "pens" not in signature
    assert "1.25" not in signature
    assert all(c in "0123456789abcdef" for c in signature)


# A blocklist loses because filler words are unbounded: catch one adverb and
# the next farm just picks a different one. These are not on any list -- they
# collapse because `task_shape` never reads a word that sits outside a noun
# phrase, whatever that word is spelled. Each line below is one attempt to
# farm a second task shape out of PEN; every one of them must fail.
PEN_PARAPHRASES = (
    # filler adverbs, singly and in combination -- the exact five named as
    # currently-differing in the bug report, plus a few more of the same kind
    "just compute the total cost of 3 pens at 1.25 dollars each",
    "now compute the total cost of 3 pens at 1.25 dollars each",
    "quickly compute the total cost of 3 pens at 1.25 dollars each",
    "simply compute the total cost of 3 pens at 1.25 dollars each",
    "approximately compute the total cost of 3 pens at 1.25 dollars each",
    "compute the total cost of 3 pens at 1.25 dollars each right now",
    "basically, compute the total cost of 3 pens at 1.25 dollars each",
    # politeness, in every position a request wraps it in
    "please compute the total cost of 3 pens at 1.25 dollars each",
    "could you compute the total cost of 3 pens at 1.25 dollars each",
    "kindly compute the total cost of 3 pens at 1.25 dollars each for me",
    "compute the total cost of 3 pens at 1.25 dollars each, thanks",
    (
        "can you please just compute the total cost of 3 pens at 1.25 dollars "
        "each for me now?"
    ),
    # reordered clauses -- the literals and the subject swap ends of the
    # sentence around the verb
    "at 1.25 dollars each, compute the total cost of 3 pens",
    "3 pens at 1.25 dollars each -- compute the total cost",
    "of 3 pens at 1.25 dollars each, compute the total cost",
    # punctuation and whitespace, none of it grammatical
    "Compute the total cost of 3 pens at 1.25 dollars each.",
    "compute the total cost of 3 pens, at 1.25 dollars each!",
    "compute   the   total   cost   of  3  pens  at  1.25 dollars each",
    # casing
    "COMPUTE THE TOTAL COST OF 3 PENS AT 1.25 DOLLARS EACH",
    # plural/singular -- folded by the same `_stem` shape agreement already
    # relies on, so "3 pens" and "1 pen" name one subject
    "compute the total cost of 3 pen at 1.25 dollars each",
    "calculate the cost of 1 pen at 1.25 dollars",
    # synonyms for the method verb -- excluded from the subject entirely, so
    # swapping it can never mint a second shape
    "calculate the total cost of 3 pens at 1.25 dollars each",
    "determine the total cost of 3 pens at 1.25 dollars each",
    "work out the total cost of 3 pens at 1.25 dollars each",
    "find the total cost of 3 pens at 1.25 dollars each",
    # a synonym for a modifier, not the subject itself -- "overall" for
    # "total" -- which the head-noun rule already collapses for free
    "compute the overall cost of 3 pens at 1.25 dollars each",
    # every channel stacked at once
    (
        "so, could you please just quickly compute the overall cost of 3 pen "
        "at 1.25 dollars each for me, thanks?"
    ),
)


def test_twenty_paraphrases_of_one_task_collapse_to_one_signature():
    """THE FARMING CASE, exhaustively. `MIN_DISTINCT_TASKS` counts signatures,
    so every one of these has to be worth zero additional evidence -- a filler
    adverb, a plural, a synonym for "compute", or any combination of the three
    must never be the second observation that promotes a candidate nobody
    actually re-asked.

    At least twenty phrasings, because the previous fix was a blocklist and a
    blocklist's failure only shows up on the word that was never added to it.
    A list this long is not proof no word can still slip through -- see
    `test_a_fronted_noun_phrase_is_the_one_named_residual_split` for the split
    this rule still has -- but it is proof the fix is not just yesterday's
    five words with today's five words appended.
    """
    assert len(PEN_PARAPHRASES) >= 20
    target = task_signature(PEN)
    for paraphrase in PEN_PARAPHRASES:
        assert task_signature(paraphrase) == target, paraphrase


def test_a_genuinely_different_task_still_gets_its_own_signature():
    """The collapse above is only meaningful if the same machinery still
    tells two different questions apart. Headcount and teams share no
    vocabulary with pens and cost, so folding filler, case, order and
    plurality must not fold this too."""
    assert task_signature(PEN) != task_signature(
        "compute the total headcount of 5 teams"
    )
    assert task_signature(PEN) != task_signature(
        "please just quickly compute the total headcount of 5 teams for me"
    )


def test_a_fronted_noun_phrase_no_longer_splits_from_its_ordinary_form():
    """This test used to assert the opposite, and said so: its previous
    docstring recorded that a real fix would need it REWRITTEN rather than
    deleted. This is that rewrite.

    A determiner-less fronted noun phrase contributed no subject at all, which
    failed in both directions at once -- it split the fronted phrasing from the
    ordinary phrasing of one task (a farm, because it mints the second shape a
    promotion needs), and it merged every such task onto the single empty
    subject (so a skill proven on pens could claim pencils). `task_shape` now
    falls back to content words when nothing was introduced, closing both.
    """
    assert task_signature(PEN) == task_signature(
        "Pens: compute the total cost of 3 at 1.25 dollars each"
    )
    assert task_signature(
        "Pens: compute the total cost of 3 at 1.25 dollars each"
    ) != task_signature(
        "Pencils: compute the total cost of 3 at 1.25 dollars each"
    )


def test_the_fallback_leaves_a_narrower_residual_and_this_is_it():
    """What the fix costs, pinned so it cannot widen unnoticed.

    The fallback keeps every content word, and it cannot tell a verb it does
    not recognise from a noun. So whenever the head-of-phrase pass leaves no
    subject -- always true with no determiner anywhere, but also true for a
    determiner-introduced phrase whose head word is itself in
    `METHOD_LEXICON` -- a verb outside `METHOD_LEXICON` is read as part of
    the subject matter, and the same task phrased with a recognised verb gets
    a different signature.

    That is strictly narrower than what it replaced: it needs BOTH an
    unrecognised verb AND a sentence where the head-of-phrase pass finds no
    subject, where the old residual fired on any fronted phrase. The honest
    fix is to widen METHOD_LEXICON when such a verb turns up, not to widen
    this fallback -- so this test failing means a word needs adding to the
    lexicon.
    """
    assert task_signature("tally widgets") != task_signature("count widgets")

    # A determiner on a head word outside METHOD_LEXICON is enough to avoid
    # it, because the head-of-phrase rule then finds a subject and the
    # fallback never runs. (A determiner alone is not enough -- "count the
    # count" still hits the fallback, because "count" heads its own phrase
    # and is itself in METHOD_LEXICON.)
    assert task_signature("tally the widgets") == task_signature(
        "count the widgets"
    )


# Nouns ending in a silent `-se`: the plural is spelled `...ses`, which looks
# identical -- for the last four letters -- to a bare-sibilant base plus `es`
# (`house`+`s` and `bus`+`es` both end `-uses`). Real task-subject vocabulary
# in this system's domain skews toward the `-se` reading (a database, a
# license, an expense, a case), so `_stem` resolves the tie that way. See its
# own docstring for the full trade and what it costs.
_SE_NOUNS = (
    "house", "case", "database", "response", "expense", "purpose", "phrase",
    "license", "horse",
)


def test_se_ending_subjects_fold_across_singular_and_plural():
    """The gap this module's docstring used to leave silent: a bare
    pluralization of an ordinary `-se` subject noun ("1 database" -> "3
    databases") is not a rewording anyone would call adversarial, and before
    `_stem` resolved the `...ses` tie toward `-se`, it minted a second
    signature for the same task -- the "farming channel itself" direction
    `task_signature`'s own docstring names as the dangerous one.
    """
    for noun in _SE_NOUNS:
        singular = task_signature(f"compute the total cost of 1 {noun} at 12 dollars")
        plural = task_signature(
            f"compute the total cost of 3 {noun}s at 12 dollars each"
        )
        assert singular == plural, noun


def test_bare_sibilant_loanword_subjects_are_a_disclosed_residual_split():
    """The tie-break above is a choice, not a discovery, and it has a named
    cost: kept as a fixture, like the fronted-noun-phrase split above, so it
    cannot silently start passing (the ambiguity would have to have grown a
    lexicon somewhere) or silently start failing worse than documented.

    Two flavours, both already explained in `_stem`'s own docstring:

      bus/gas    short bases that need `-es` and, before this change, folded
                 correctly BY ACCIDENT (the ambiguous tie went their way).
                 Resolving the tie toward `-se` for the common case costs
                 these two -- disclosed, not hidden.
      lens/virus/atlas/campus/cactus/canvas/circus/iris
                 longer Latin/Greek loanwords whose SINGULAR already ends in
                 a bare `s` indistinguishable from a genuine plural's
                 (`lens` and `pens` share their last two letters). This gap
                 predates this change and is untouched by it: no suffix rule
                 tells "already singular" from "needs stripping" apart.
    """
    for singular, plural in (
        ("bus", "buses"),
        ("gas", "gases"),
        ("atlas", "atlases"),
        ("virus", "viruses"),
        ("lens", "lenses"),
        ("campus", "campuses"),
        ("iris", "irises"),
        ("cactus", "cactuses"),
        ("canvas", "canvases"),
        ("circus", "circuses"),
    ):
        assert _stem(singular) != _stem(plural), (singular, plural)


# --- the fold has to meet in the middle --------------------------------------


def test_a_sibilant_plural_and_its_silent_e_singular_reach_one_stem():
    """Stripping "es" folds the PLURAL of a sibilant noun but left the SINGULAR
    of a silent-e one untouched, so `niches` reached `nich` while `niche` stayed
    `niche` and one task registered as two.

    That is the farming channel this module exists to close, opened by the very
    fold meant to close it -- which is why both directions are pinned here.
    """
    from swarmd.swarm.generalise import _stem

    for singular, plural in (
        ("niche", "niches"), ("cache", "caches"), ("size", "sizes"),
        ("batch", "batches"), ("dish", "dishes"), ("box", "boxes"),
    ):
        assert _stem(singular) == _stem(plural), (
            f"{singular!r} and {plural!r} split into two task shapes"
        )


def test_the_se_class_is_not_pushed_apart_by_the_symmetric_fold():
    """`s` is excluded from the symmetric fold on purpose.

    For an `-se` word the plural is already resolved toward the singular
    (`cases` -> `case`), so folding the singular's `e` as well would push the
    pair apart again in the opposite direction -- trading one split for another.
    """
    from swarmd.swarm.generalise import _stem

    for singular, plural in (
        ("case", "cases"), ("house", "houses"), ("phrase", "phrases"),
        ("database", "databases"), ("response", "responses"), ("price", "prices"),
    ):
        assert _stem(singular) == _stem(plural)


def test_words_that_merely_end_in_s_are_still_left_alone():
    from swarmd.swarm.generalise import _stem

    for word in ("process", "address", "class", "css", "abs", "ops", "data"):
        assert _stem(word) == word


# --- the near-tier leak guard must see through inflection --------------------


def test_the_leak_guard_catches_a_singular_of_the_source_subject():
    """The guard compares SUBJECTS, and `pen` is the same subject as `pens`.

    Comparing surface forms let an ordinary singular/plural paraphrase walk
    through: a criterion rebound off a task about `pens` onto one about
    `pencils` could keep a check parameter naming `pen` and be reported clean,
    which is exactly the leak the guard exists to refuse.
    """
    from swarmd.swarm.generalise import leaked_subject_terms

    source = "compute the total cost of 3 pens at 1.25 dollars each"
    target = "compute the total cost of 7 pencils at 0.40 dollars each"

    assert leaked_subject_terms("count the pens", source, target)
    assert leaked_subject_terms("count the pen", source, target)


def test_the_leak_guard_stays_clean_on_an_honest_rebind():
    """It must refuse leaks without refusing the transfer it exists to allow."""
    from swarmd.swarm.generalise import leaked_subject_terms

    source = "compute the total cost of 3 pens at 1.25 dollars each"
    target = "compute the total cost of 7 pencils at 0.40 dollars each"

    assert not leaked_subject_terms("count the pencils", source, target)
    assert not leaked_subject_terms("count the pencil", source, target)
    assert not leaked_subject_terms("count the items", source, target)


# --- a quantity written as a word is the same quantity ------------------------


def test_a_spelled_out_number_is_the_same_shape_as_its_digit():
    """One task, written twice, must not clear the two-distinct-shapes bar.

    Matching only digits made "three pens" and "3 pens" two task shapes, so an
    author could mint the second piece of evidence a promotion needs without
    ever solving a second task. That is a farm, and it is the exact thing the
    distinct-shape bar exists to refuse.
    """
    base = "compute the total cost of 3 pens at 1.25 dollars each"
    for word in ("one", "two", "three", "seven", "ten", "twelve", "twenty"):
        assert task_signature(base.replace("3", word)) == task_signature(base), (
            f"{word!r} minted a second task shape"
        )


def test_a_number_that_is_the_method_still_survives_when_spelled_out():
    """The digit branch already protects "round to 2 decimal places" -- that 2
    is advice, not data. Recognising number WORDS must not quietly strip the
    spelled version of the same advice."""
    for phrase in ("round to 2 decimal places", "round to two decimal places"):
        assert "<NUMBER>" not in abstract(phrase).template


def test_a_word_that_merely_contains_a_numeral_is_untouched():
    """The cardinals are matched on word boundaries, so "money" does not
    contain "one" and "number" does not contain "nine"."""
    for phrase in ("summarise the money in the account", "list every phone number"):
        assert "<NUMBER>" not in abstract(phrase).template


# --- a fronted subject is still a subject -------------------------------------


def test_a_fronted_subject_matches_its_ordinary_phrasing():
    """Head-of-phrase needs an introducer, and a fronted subject has none.

    "pens: compute the total cost of 3 at 1.25 each" introduced nothing, so the
    shape came back with NO subject -- which split it from the same task written
    ordinarily. One task, two phrasings, two signatures is a farm: it mints the
    second piece of evidence a promotion needs without anyone solving a second
    task.
    """
    ordinary = "compute the total cost of 3 pens at 1.25 dollars each"
    fronted = "pens: compute the total cost of 3 at 1.25 dollars each"
    assert task_signature(fronted) == task_signature(ordinary)


def test_two_fronted_subjects_do_not_collapse_into_one():
    """The same empty-subject bug merged genuinely different tasks.

    Both "pens:" and "pencils:" reduced to no subject at all, so two questions
    about different things shared one signature -- which withholds evidence in
    one direction and, worse, lets a skill proven on one claim the other.
    """
    pens = "pens: compute the total cost of 3 at 1.25 dollars each"
    pencils = "pencils: compute the total cost of 3 at 1.25 dollars each"
    assert task_signature(pens) != task_signature(pencils)


def test_the_fallback_does_not_disturb_ordinary_phrasing():
    """It is a fallback, and must only run when nothing was introduced."""
    ordinary = "compute the total cost of 3 pens at 1.25 dollars each"
    assert task_signature("just " + ordinary) == task_signature(ordinary)
    assert task_signature(ordinary.replace("3", "three")) == task_signature(ordinary)
    assert task_signature(ordinary) != task_signature(
        "compute the total cost of 3 pencils at 1.25 dollars each"
    )


# --- subject matter welded into an identifier ------------------------------


def test_an_identifier_naming_the_task_is_stripped_like_the_words_would_be():
    """The leak that reached an approved skill.

    A step naming `stock_count` names its own task exactly as plainly as one
    saying "stock count", but the whole token was not in the source vocabulary
    and survived untouched. The distilled skill then told every later run to
    emit `stock_count` and `ledger_count`, so a reconciliation of an invoice
    against a payment inherited the wrong keys -- one task's answer wearing a
    method's grammar, which is the exact failure `strip_source_terms` exists to
    prevent.
    """
    source = (
        "A stock count records 87 units while the ledger shows 92. Determine "
        "which figures disagree and produce the reconciliation.",
    )
    stripped = strip_source_terms(
        "produce a JSON object with keys stock_count, ledger_count and discrepancy",
        source,
    )
    assert "stock_count" not in stripped
    assert "ledger_count" not in stripped
    # Not from the task, so it stays: the point is to drop the subject, not to
    # reduce the step to placeholders.
    assert "discrepancy" in stripped


def test_an_identifier_made_only_of_method_words_survives():
    """`sort_by_price` describes the work, not the thing worked on. Collapsing
    it would leave a step that teaches nothing, which is the failure mode on
    the other side of this rule."""
    stripped = strip_source_terms(
        "sort_by_price the rows", ("sort the rows by price",)
    )
    assert "sort_by_price" in stripped


def test_a_word_is_not_split_where_the_writer_put_no_boundary():
    """Splitting happens on `_`, `-` and case changes only. Inventing divisions
    inside an ordinary word would strip tokens that share no vocabulary with
    the task at all."""
    assert _identifier_parts("counterpart") == ["counterpart"]
    assert _identifier_parts("stock_count") == ["stock", "count"]
    assert _identifier_parts("nextCursor") == ["next", "cursor"]
