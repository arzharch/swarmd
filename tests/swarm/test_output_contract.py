"""What a step PRODUCED, versus what the model replied.

Every test here covers a defect that made real-provider runs score 0/N while
the simulated provider showed a green pipeline. They share one cause: the
simulated provider replies with bare JSON, which is the single output shape
where the reply and the answer are the same string. Real models reply with
fenced code that has to be run, and the answer is what running it produced.
"""

from __future__ import annotations

import json

import pytest

from swarmd.router.providers import ProviderError
from swarmd.swarm.criteria import Criterion
from swarmd.swarm.economy import Economy
from swarmd.swarm.planner import PlanNode
from swarmd.swarm.worker import GenericWorker, WorkerContext, _unfence


class FakeSandboxResult:
    def __init__(self, artifacts=None, stdout="", exit_code=0, violation=""):
        self.artifacts = artifacts or {}
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.violation = violation


class FakeSandbox:
    def __init__(self, result):
        self._result = result

    async def run_python(self, code):
        return self._result


def _worker(sandbox=None):
    economy = Economy()
    context = WorkerContext(
        provider=None,
        criterion=Criterion.from_dict(
            {"description": "d", "checks": [{"kind": "output_nonempty", "params": {}}]}
        ),
        economy=economy,
        sandbox=sandbox,
    )
    return GenericWorker(economy.spawn().agent_id, context)


NODE = PlanNode(name="extract", instruction="produce the claims")
CODE_REPLY = '```python\nimport json\nprint("hi")\n```'


# --- the answer is what was produced ---------------------------------------


async def test_artifacts_become_the_graded_output():
    """The defect that scored a correct run 0/N.

    The worker prompt tells agents to write results to artifacts.json. The old
    code ran that code correctly and then graded the fenced SOURCE, so a
    `json_parses` check read Python and reported "not JSON" for a step that had
    succeeded.
    """
    sandbox = FakeSandbox(FakeSandboxResult(artifacts={"accuracy": 94.3}))
    candidate = await _worker(sandbox)._materialise(CODE_REPLY, NODE)

    assert json.loads(candidate.output) == {"accuracy": 94.3}
    assert candidate.artifacts == {"accuracy": 94.3}


async def test_the_raw_reply_is_kept_for_traceability():
    """Grading the result must not throw away how it was produced."""
    sandbox = FakeSandbox(FakeSandboxResult(artifacts={"accuracy": 94.3}))
    candidate = await _worker(sandbox)._materialise(CODE_REPLY, NODE)

    assert candidate.source == CODE_REPLY
    assert candidate.source != candidate.output


async def test_stdout_is_the_answer_when_the_program_printed_instead():
    """A program that prints its result has still produced one."""
    sandbox = FakeSandbox(FakeSandboxResult(stdout='{"accuracy": 94.3}\n'))
    candidate = await _worker(sandbox)._materialise(CODE_REPLY, NODE)

    assert json.loads(candidate.output) == {"accuracy": 94.3}


async def test_code_that_produced_nothing_keeps_its_source():
    """The criterion should fail this, and a repair round is more useful when
    the agent can see what it submitted."""
    sandbox = FakeSandbox(FakeSandboxResult())
    candidate = await _worker(sandbox)._materialise(CODE_REPLY, NODE)

    assert candidate.output == CODE_REPLY


async def test_a_reply_with_no_code_is_the_answer_itself():
    candidate = await _worker()._materialise('{"accuracy": 94.3}', NODE)
    assert json.loads(candidate.output) == {"accuracy": 94.3}


# --- fences are presentation, not content ----------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '  ```json\n{"a": 1}\n```  ',
    ],
)
def test_a_fenced_json_answer_is_unfenced(reply):
    """Models asked for JSON commonly wrap it. Leaving the fence on fails every
    json_parses check on output that is otherwise exactly right."""
    assert json.loads(_unfence(reply)) == {"a": 1}


def test_a_fence_inside_prose_is_left_alone():
    """Mid-prose fences are content; cutting them out changes the answer."""
    prose = 'Here is the plan:\n```\nstep one\n```\nand that is all.'
    assert _unfence(prose) == prose


def test_unfencing_leaves_plain_text_untouched():
    assert _unfence('{"a": 1}') == '{"a": 1}'


# --- an empty completion is a failure --------------------------------------


def test_an_empty_completion_is_reported_as_a_provider_error():
    """`openai/gpt-oss-20b` is a reasoning model: on a long prompt it spends the
    whole output budget thinking and returns content="" with completion_tokens
    at the cap.

    Handed back as an empty string it looked like a proposer producing
    nonsense, and criterion synthesis refused to run -- blaming the model for
    output it was never given room to write. As an error, the pool fails over
    to the next model instead.
    """
    import httpx

    from swarmd.router.pool import REGISTRY, OpenAICompatProvider

    provider = OpenAICompatProvider(REGISTRY["groq"], "k", credential_id="k#0")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 700},
            },
        )

    # base_url matters: the provider posts a relative path, and a mock client
    # without one cannot resolve it.
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.groq.com/openai/v1",
    )

    from swarmd.router.providers import LLMRequest

    async def run():
        with pytest.raises(ProviderError, match="empty completion"):
            await provider.complete_with(
                "openai/gpt-oss-20b", LLMRequest(prompt="p", max_tokens=700)
            )

    import asyncio

    asyncio.run(run())


# --- the artifact contract both halves must agree on -----------------------


def test_both_prompts_state_that_artifacts_are_keys_not_filenames():
    """The convention collision that produced correct data nested one level
    too deep: the proposer asked for `numeric_claims.json` as an artifact key,
    the worker obliged, and every check for a top-level `accuracy` failed
    against a run that had extracted the accuracy."""
    from swarmd.swarm.criteria import CHECK_PARAMS
    from swarmd.swarm.synthesis import PROPOSER_SYSTEM
    from swarmd.swarm.worker import WORKER_SYSTEM

    assert "NOT a filename" in CHECK_PARAMS["artifact_exists"]
    assert "artifacts.json" in PROPOSER_SYSTEM
    assert "filename" in WORKER_SYSTEM
