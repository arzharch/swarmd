"""The training corpus, and the separation that keeps it out of measurements.

A skill becomes reviewable only once it has worked on two DISTINCT task shapes.
The evaluated suite has twelve tasks with twelve disjoint output shapes, so no
approach was ever proposed twice and nothing could ever clear that bar -- which
is why every self-learning measurement this project attempted came back empty
(ADR-014). These tests pin the two properties the fix depends on: the training
tasks come in families that can supply a second piece of evidence, and no eval
can be pointed at them.
"""

from __future__ import annotations

import collections

import pytest
from pydantic import ValidationError

from examples.tasks.suite import CUSTOM, HOLDOUT, PUBLIC, TRAIN, suite
from swarmd.server.control import EvalRequest, SessionRequest


def test_the_training_tasks_come_in_families_that_share_a_shape():
    """A family is what lets an approach be proposed by a second task. One
    task per domain would leave the promotion bar exactly as unreachable as
    the evaluated suite leaves it."""
    families = collections.Counter(task.domain for task in TRAIN)

    assert families, "there is a training corpus"
    assert all(count >= 2 for count in families.values()), (
        f"every family needs a second member to be evidence for anything: "
        f"{dict(families)}"
    )
    assert len(families) >= 3, "several kinds of work, not one repeated"


def test_no_training_task_is_also_an_evaluated_task():
    """The contamination this whole arrangement exists to prevent."""
    train = {task.task_id for task in TRAIN}
    evaluated = {task.task_id for task in PUBLIC + CUSTOM + HOLDOUT}

    assert train, "there is a training corpus"
    assert train.isdisjoint(evaluated)


def test_training_prompts_are_not_reused_verbatim_anywhere_evaluated():
    """Ids being distinct is not enough: the same prompt under two ids is the
    same memorisation channel."""
    train = {task.prompt.strip() for task in TRAIN}
    evaluated = {task.prompt.strip() for task in PUBLIC + CUSTOM + HOLDOUT}

    assert train.isdisjoint(evaluated)


def test_both_does_not_quietly_include_the_training_set():
    """`both` means public plus custom. A training set folded into it would
    make every later eval a memorisation check, silently."""
    ids = {task.task_id for task in suite(arms="both")}

    assert ids == {task.task_id for task in PUBLIC + CUSTOM}
    assert not any(task.task_id.startswith("trn-") for task in suite(arms="both"))


def test_a_session_can_ask_for_the_training_set_and_an_eval_cannot():
    """Enforced rather than documented. Measuring over the tasks a library was
    built from is memorisation, so it is made unexpressible -- a convention a
    person has to keep is exactly what failed here before."""
    assert SessionRequest(arms="train").arms == "train"

    with pytest.raises(ValidationError):
        EvalRequest(arms="train")


def test_the_training_set_is_reachable_only_by_naming_it():
    assert {task.task_id for task in suite(arms="train")} == {
        task.task_id for task in TRAIN
    }
