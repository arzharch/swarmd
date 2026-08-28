"""Criterion synthesis: decide what "done" means before trying to be done.

ADR-009 in code. An unscoped task arrives with no definition of success, so the
swarm authors one, attacks it, and freezes it -- and only then starts solving.
The ordering is the entire point: a criterion produced after seeing candidate
solutions is a rationalisation, not a test.

WHY A DECLARATIVE CHECK LANGUAGE, not model-written Python.

The obvious implementation is to have the model emit a `def check(candidate):`
and `exec` it. That is rejected for four reasons, in descending order of how
much they matter:

1. **It is inspectable.** A frozen criterion is a run output that a human may
   need to audit months later. A list of typed checks can be read; a page of
   generated Python has to be comprehended.
2. **It is content-addressable.** Checks are canonicalised and hashed, so the
   same criterion always produces the same hash regardless of how the model
   phrased it. Generated code hashes differently for a renamed variable.
3. **It cannot do anything.** A check evaluates a candidate. It cannot read
   files, open sockets, or spend budget. Model-written code being executed to
   decide correctness, in a system whose agents are selected on passing that
   check, is an obvious exploit surface.
4. **Malformed proposals fail loudly.** An unknown check kind is rejected at
   parse time, not discovered as a NameError halfway through a run.

The cost, stated plainly: the language bounds what can be expressed. A task
needing a genuinely novel predicate cannot be graded until a check kind is
added. That is a real limitation and it is preferred to executing arbitrary
model output as the arbiter of truth.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# --- candidates ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """What gets graded: what the step PRODUCED, plus how it produced it.

    `output` is the answer, not the transcript. For a step that writes code,
    the answer is what running the code produced -- not the source. Grading the
    source was a real failure: every real-provider run scored 0/N because
    models replied with a fenced ```python block, the sandbox ran it correctly,
    and then a `json_parses` check read the Python and reported "not JSON".

    `source` keeps the raw model reply so traceability does not lose it. It is
    deliberately not what checks read: a criterion asking "is the answer valid
    JSON with these keys" means the answer.
    """

    output: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    # The raw model reply when `output` was derived from running it. Empty when
    # the reply IS the output.
    source: str = ""


# --- checks ----------------------------------------------------------------


class CheckError(ValueError):
    """A proposed check is malformed. Raised at parse time, never at grade time."""


@dataclass(frozen=True, slots=True)
class Check:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> str:
        """Stable text form. Sorted keys so key order cannot change the hash."""
        return json.dumps(
            {"kind": self.kind, "params": self.params},
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    kind: str
    passed: bool
    detail: str = ""


def _p(params: dict[str, Any], key: str, *default: Any) -> Any:
    if key in params:
        return params[key]
    if default:
        return default[0]
    raise CheckError(f"check parameter {key!r} is required")


def _check_output_nonempty(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    """Output has at least `min_chars` non-whitespace characters.

    The weakest useful check, and the one an adversary defeats first. It exists
    so a criterion made ONLY of this is caught by the adversarial pass rather
    than shipped.
    """
    minimum = int(_p(params, "min_chars", 1))
    stripped = c.output.strip()
    return CheckOutcome(
        "output_nonempty",
        len(stripped) >= minimum,
        f"{len(stripped)} non-whitespace chars, need {minimum}",
    )


def _check_contains_all(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    needles = [str(s) for s in _p(params, "substrings")]
    if not needles:
        raise CheckError("contains_all needs at least one substring")
    haystack = c.output if _p(params, "case_sensitive", False) else c.output.lower()
    missing = [
        n for n in needles
        if (n if _p(params, "case_sensitive", False) else n.lower()) not in haystack
    ]
    return CheckOutcome("contains_all", not missing, f"missing={missing}")


def _check_regex_match(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    pattern = str(_p(params, "pattern"))
    try:
        # Compiled at grade time but validated at parse time; see _validate.
        matched = re.search(pattern, c.output, re.MULTILINE | re.DOTALL) is not None
    except re.error as exc:
        raise CheckError(f"invalid regex {pattern!r}: {exc}") from exc
    return CheckOutcome("regex_match", matched, f"pattern={pattern!r}")


def _check_json_parses(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    try:
        parsed = json.loads(c.output)
    except json.JSONDecodeError as exc:
        return CheckOutcome("json_parses", False, f"not JSON: {exc}")
    required = [str(k) for k in _p(params, "required_keys", [])]
    if not isinstance(parsed, dict) and required:
        return CheckOutcome("json_parses", False, "not a JSON object")
    missing = [k for k in required if k not in parsed]
    return CheckOutcome("json_parses", not missing, f"missing_keys={missing}")


def _check_numeric_range(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    """A numeric artifact falls within a range.

    The workhorse for reproduction tasks: a paper claims 0.942, we measured
    0.917, and the criterion says how close is close enough. Tolerance is
    explicit in the criterion rather than a constant somewhere, so what counted
    as success is visible in the frozen hash.
    """
    key = str(_p(params, "key"))
    lo = float(_p(params, "min", float("-inf")))
    hi = float(_p(params, "max", float("inf")))
    if key not in c.artifacts:
        return CheckOutcome("numeric_range", False, f"artifact {key!r} absent")
    try:
        value = float(c.artifacts[key])
    except (TypeError, ValueError):
        return CheckOutcome("numeric_range", False, f"artifact {key!r} not numeric")
    ok = lo <= value <= hi
    return CheckOutcome("numeric_range", ok, f"{key}={value} range=[{lo}, {hi}]")


def _check_artifact_exists(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    key = str(_p(params, "key"))
    present = key in c.artifacts and c.artifacts[key] not in (None, "", [], {})
    return CheckOutcome("artifact_exists", present, f"key={key!r}")


def _check_exit_code(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    """Sandboxed execution succeeded. Objective, and free to evaluate."""
    expected = int(_p(params, "expected", 0))
    return CheckOutcome(
        "exit_code", c.exit_code == expected, f"got={c.exit_code} want={expected}"
    )


def _check_stdout_contains(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    needle = str(_p(params, "substring"))
    return CheckOutcome("stdout_contains", needle in c.stdout, f"needle={needle!r}")


def _check_min_distinct_words(c: Candidate, params: dict[str, Any]) -> CheckOutcome:
    """Guards against constant and repeated-token output.

    Specifically an anti-degenerate check: "aaaa aaaa aaaa" satisfies length
    and non-emptiness while carrying no information. Included in the library so
    the adversarial pass has something to strengthen a weak criterion WITH,
    rather than only being able to reject it.
    """
    minimum = int(_p(params, "min_distinct", 5))
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9_]+", c.output)}
    return CheckOutcome(
        "min_distinct_words", len(words) >= minimum, f"{len(words)} distinct"
    )


# What each check REQUIRES, as data, so the proposal prompt can state the
# contract instead of showing `"params": {}` and hoping.
#
# This existed only in the checks' own `_p(params, "key")` calls, which the
# model proposing a criterion cannot read. It copied the empty example, emitted
# checks with no parameters, and every one of them failed every candidate --
# the first real-provider run graded 0 of 16 nodes and blamed the workers.
#
# Kept beside CHECK_KINDS so a new check with a required parameter that is not
# declared here is visible in review as an obviously missing line.
CHECK_PARAMS: dict[str, str] = {
    # Values are ANGLE-BRACKET PLACEHOLDERS describing what to supply, never
    # plausible literals. Both failure modes have now been observed against
    # real models, one after fixing the other:
    #
    #   `"params": {}`            copied faithfully -> checks with no
    #                             parameters -> unsatisfiable criterion
    #   `{"key": "claims.json"}`  copied faithfully -> every criterion demanded
    #                             a file called claims.json and a stdout marker
    #                             reading VERIFIED, whatever the task was
    #
    # A concrete example is an instruction to copy it. The shape has to be
    # legible without any of it being usable as-is.
    "output_nonempty": '{"min_chars": <integer>}',
    "contains_all": '{"substrings": [<strings that must appear in the output>]}',
    "regex_match": '{"pattern": "<regex the output must match>"}',
    "json_parses": '{"required_keys": [<keys the JSON object must contain>]}',
    # ARTIFACTS ARE KEYS IN ONE JSON OBJECT, NOT FILENAMES. Both halves of the
    # system have to agree on this and for a while they did not: the worker
    # prompt says "write results to artifacts.json", so proposers reasonably
    # read `key` as a file name and asked for `numeric_claims.json`. The worker
    # obliged by writing {"numeric_claims.json": {...}} -- correct data, nested
    # one level too deep -- and every check for a top-level `accuracy` failed
    # against a run that had actually extracted the accuracy.
    "numeric_range": (
        '{"key": "<top-level key in artifacts.json, e.g. accuracy -- '
        'NOT a filename>", "min": <number>, "max": <number>}'
    ),
    "artifact_exists": (
        '{"key": "<top-level key the step must put in artifacts.json, '
        'e.g. claims -- NOT a filename>"}'
    ),
    "exit_code": '{"expected": <integer, usually 0>}',
    "stdout_contains": '{"substring": "<text the program must print>"}',
    "min_distinct_words": '{"min_distinct": <integer>}',
}


CHECK_KINDS = {
    "output_nonempty": _check_output_nonempty,
    "contains_all": _check_contains_all,
    "regex_match": _check_regex_match,
    "json_parses": _check_json_parses,
    "numeric_range": _check_numeric_range,
    "artifact_exists": _check_artifact_exists,
    "exit_code": _check_exit_code,
    "stdout_contains": _check_stdout_contains,
    "min_distinct_words": _check_min_distinct_words,
}

# Checks that a degenerate candidate can satisfy on its own. A criterion built
# only from these is not a criterion, it is a formality -- see `is_weak`.
TRIVIAL_KINDS = frozenset({"output_nonempty", "artifact_exists"})


# --- criterion -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriterionResult:
    passed: bool
    outcomes: tuple[CheckOutcome, ...]

    @property
    def failures(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.passed]

    def summary(self) -> str:
        if self.passed:
            return f"pass ({len(self.outcomes)} checks)"
        return "fail: " + "; ".join(f"{o.kind}: {o.detail}" for o in self.failures)


@dataclass(frozen=True, slots=True)
class Criterion:
    """A frozen, content-addressed definition of success.

    Immutable by construction. A criterion that can move while results are
    graded against it is not a criterion, so correction happens between runs --
    where it is visible as a new hash.
    """

    description: str
    checks: tuple[Check, ...]

    def __post_init__(self) -> None:
        if not self.checks:
            raise CheckError("a criterion with no checks accepts everything")
        for check in self.checks:
            if check.kind not in CHECK_KINDS:
                raise CheckError(
                    f"unknown check kind {check.kind!r}; known: "
                    f"{sorted(CHECK_KINDS)}"
                )
            if check.kind == "regex_match":
                try:
                    re.compile(str(check.params.get("pattern", "")))
                except re.error as exc:
                    raise CheckError(f"invalid regex: {exc}") from exc

    def malformed(self) -> list[str]:
        """Checks that can never pass, whatever a worker produces.

        A check missing a required parameter -- `artifact_exists` with no
        `key`, `contains_all` with no `substrings` -- raises `CheckError` on
        every candidate. `evaluate` turns that into a failed outcome, so the
        criterion grades every attempt as a failure forever, and the run
        reports 0/16 nodes passed with no indication that the criterion itself
        is the problem.

        That is not hypothetical: it is what the first real-provider run did.
        Against the simulated provider it never appeared, because the stub
        emitted proposals whose parameters were always complete.

        Dry-run against a deliberately rich candidate: anything that raises
        here is malformed regardless of input, as opposed to merely failing on
        this particular input.
        """
        probe = Candidate(
            output='{"summary": "probe", "count": 1}',
            artifacts={"probe": "1"},
            exit_code=0,
            stdout="probe",
        )
        broken = []
        for check in self.checks:
            try:
                CHECK_KINDS[check.kind](probe, check.params)
            except CheckError as exc:
                broken.append(f"{check.kind}: {exc}")
            except KeyError:
                broken.append(f"{check.kind}: unknown check kind")
            except Exception:  # noqa: BLE001, S112 - see below
                # Raising something OTHER than CheckError means the check ran
                # and disliked the probe, which is exactly what a check is for.
                # Not logged: this is the expected path for most checks against
                # a probe they were never written for, so logging it would be
                # noise at the volume of every criterion ever frozen.
                continue
        return broken

    def evaluate(self, candidate: Candidate) -> CriterionResult:
        """Grade a candidate. Every check runs, even after one fails.

        Short-circuiting would be faster and would produce a worse repair
        signal: an agent told only the first failure fixes one thing per round
        and burns the bounded repair budget discovering the rest.
        """
        outcomes = []
        for check in self.checks:
            try:
                outcomes.append(CHECK_KINDS[check.kind](candidate, check.params))
            except CheckError as exc:
                outcomes.append(CheckOutcome(check.kind, False, f"malformed: {exc}"))
        return CriterionResult(all(o.passed for o in outcomes), tuple(outcomes))

    def content_hash(self) -> str:
        """Stable identity. Same checks -> same hash, whatever their order.

        Order-independent because two proposals listing the same checks in
        different orders are the same criterion, and treating them as different
        would make cross-run comparison meaningless.
        """
        canonical = sorted(c.canonical() for c in self.checks)
        return hashlib.sha256("|".join(canonical).encode()).hexdigest()[:16]

    def is_weak(self) -> bool:
        """True when every check is one a degenerate candidate can satisfy."""
        return all(c.kind in TRIVIAL_KINDS for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "hash": self.content_hash(),
            "checks": [{"kind": c.kind, "params": c.params} for c in self.checks],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Criterion:
        raw = data.get("checks")
        if not isinstance(raw, list):
            raise CheckError("criterion payload has no 'checks' list")
        checks = []
        for entry in raw:
            if not isinstance(entry, dict) or "kind" not in entry:
                raise CheckError(f"malformed check entry: {entry!r}")
            checks.append(
                Check(str(entry["kind"]), dict(entry.get("params") or {}))
            )
        return Criterion(str(data.get("description", "")), tuple(checks))
