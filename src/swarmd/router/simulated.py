"""Simulated provider: develop the whole system before any key exists.

This is the one piece of synthetic data that lives outside `tests/`, and it
needs to justify itself against ADR-006, which says the mock is a test double
and never a demo path.

The justification is that ADR-006's actual concern is not "synthetic data
exists" but "synthetic data is indistinguishable from real data downstream". A
dashboard fed by a stub is pixel-identical to one fed by a real model, so a
config flag is not enough of a fence -- flags get set three shells ago and
forgotten, and a `.env` gets copied between machines.

So the fence is on the DATA, not the configuration:

  * every ledger row this provider produces carries `simulated=True`
  * any report aggregated from those rows carries `simulated=True`
  * `refuse_simulated()` aborts anything that would publish a number from them
  * the dashboard renders a permanent banner sourced from the same flag
  * a CI check fails if the enabling variable appears in a prod manifest

Taint propagates with the data, so there is no configuration mistake, no stale
environment, and no copied ledger file that can present simulated output as a
real result. That is a stronger guarantee than the mock ever had, which is why
this is an amendment to ADR-006 rather than a violation of it.

Determinism is deliberate and unchanged from the test mock: the same prompt
yields the same response forever, which is what makes chaos-integrity hashes
comparable across runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any

from swarmd.router.providers import LLMRequest, LLMResponse, Provider

logger = logging.getLogger(__name__)

ENV_FLAG = "SWARMD_SIMULATED_PROVIDER"

PROSE_WORDS = (
    "alpha", "beta", "gamma", "delta", "epsilon",
    "zeta", "eta", "theta", "iota", "kappa",
)


def simulation_enabled() -> bool:
    """True when the operator explicitly asked for simulated responses.

    Reads the environment rather than taking a constructor argument so that
    enabling it is visible in `env`, in a pod spec, and in a diff -- somewhere
    a reviewer can see it -- instead of being buried in a call site.
    """
    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


class SimulatedProvider(Provider):
    """Deterministic synthetic responses. Never a fallback, only a choice.

    ANATOMY: latency_s
      Artificial delay per call. Why 0.05 rather than 0: a provider that
      answers instantly hides every concurrency and backpressure bug the
      scheduler exists to handle, so development against it would produce code
      that breaks the first time a real provider takes two seconds. Low enough
      that a 600-call standard profile still finishes in under a minute.

    ANATOMY: failure_rate
      Fraction of calls that raise, deterministically chosen by prompt hash.
      Why default 0.0 but available: the error paths -- fallback chains, repair
      loops, dead-lettering -- are exactly the code that never gets exercised
      when the fake provider always succeeds, which is how a system passes
      every local test and falls over on first contact with a real API.
    """

    name = "simulated"

    def __init__(self, latency_s: float = 0.05, failure_rate: float = 0.0) -> None:
        if not simulation_enabled():
            raise RuntimeError(
                f"SimulatedProvider requires {ENV_FLAG}=true. It is not a "
                f"fallback for a missing key: a run with no providers should "
                f"fail loudly rather than quietly produce synthetic results."
            )
        self.latency_s = latency_s
        self.failure_rate = failure_rate
        self.models = ["simulated-v1"]
        logger.warning(
            "SIMULATED PROVIDER ACTIVE -- all responses are synthetic. Ledger "
            "rows will be marked simulated=true and eval will refuse to run "
            "against them."
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self.complete_with(self.models[0], request)

    async def complete_with(self, model: str, request: LLMRequest) -> LLMResponse:
        start = time.monotonic()
        await asyncio.sleep(self.latency_s)

        digest = hashlib.sha256(
            f"{request.prompt}|{round(request.temperature, 2)}".encode()
        ).hexdigest()
        n = int(digest[:8], 16)

        if self.failure_rate > 0 and (n % 1000) / 1000.0 < self.failure_rate:
            from swarmd.router.providers import ProviderError

            raise ProviderError(f"{self.name}: simulated failure (seeded)")

        text = _synthesize(request.prompt, n)

        return LLMResponse(
            text=text,
            provider=self.name,
            model=model,
            latency_s=time.monotonic() - start,
            tokens_in=len(request.prompt.split()),
            tokens_out=len(text.split()),
        )

    async def aclose(self) -> None:
        return None


def _synthesize(prompt: str, n: int) -> str:
    """Answer by prompt SHAPE, not by generic schema filling.

    A provider that returns schema-shaped noise for every prompt cannot drive
    this system: criterion synthesis correctly refuses to freeze the garbage it
    produces, so the run dies at stage zero and nothing downstream is ever
    exercised. That is the synthesizer working, and it makes a generic stub
    useless for development.

    So the simulated provider recognises the three prompt shapes the swarm
    actually issues and returns structurally valid, deterministic answers for
    each. The output is still entirely synthetic and still marked
    `simulated=true` on every ledger row (ADR-012) -- what changes is that the
    pipeline can be built and watched end to end before a key exists.
    """
    if "checks" in prompt and "schema" in prompt:
        return _criterion(n)
    if '"nodes"' in prompt and "schema" in prompt:
        return _plan(n)
    if "STEP:" in prompt and "REQUIRED:" in prompt:
        return _worker_output(prompt, n)
    if "Respond with ONLY a JSON object" in prompt:
        return _json_for_schema(prompt, n)
    return " ".join(PROSE_WORDS[(n >> i) % len(PROSE_WORDS)] for i in range(0, 40, 4))


def _criterion(n: int) -> str:
    """A criterion strong enough to survive its own adversarial pass.

    Deliberately not the weakest thing that parses: a criterion made only of
    `output_nonempty` is defeated by the `echo_task` attack, synthesis rejects
    it, and the run fails at stage zero every time. Requiring structured output
    with named keys is the smallest criterion that garbage cannot satisfy.
    """
    return json.dumps(
        {
            "description": "the step emits a structured artifact with the "
                           "required fields",
            "checks": [
                {
                    "kind": "json_parses",
                    "params": {"required_keys": ["summary", "value"]},
                },
                {"kind": "min_distinct_words", "params": {"min_distinct": 6}},
            ],
        }
    )


def _plan(n: int) -> str:
    """A small, valid, deliberately WIDE plan.

    Width varies with the prompt hash so different tasks decompose differently,
    and the parallel branch exists so the worker pool has something to be
    parallel about -- a chain would make every run single-file and hide any
    scheduling bug.
    """
    variants = [
        [
            {"name": "gather", "instruction": "produce sources.json", "depends_on": []},
            {"name": "analyse", "instruction": "produce findings.json",
             "depends_on": ["gather"]},
            {"name": "verify", "instruction": "produce verification.json",
             "depends_on": ["analyse"]},
        ],
        [
            {"name": "read", "instruction": "produce extracted.json", "depends_on": []},
            {"name": "compute", "instruction": "produce metrics.json",
             "depends_on": ["read"]},
            {"name": "crosscheck", "instruction": "produce crosscheck.json",
             "depends_on": ["read"]},
            {"name": "report", "instruction": "produce report.json",
             "depends_on": ["compute", "crosscheck"]},
        ],
    ]
    return json.dumps(
        {
            "rationale": "split extraction from verification so they can be "
                         "checked independently",
            "nodes": variants[n % len(variants)],
        }
    )


def _worker_output(prompt: str, n: int) -> str:
    """Output that satisfies a well-formed criterion without being degenerate.

    Distinct-token ratio is kept high on purpose: repeated padding is exactly
    what the red-team's criterion_gaming detector contains, so a stub emitting
    padding would have every simulated run contained and prove nothing.
    """
    step = "step"
    for line in prompt.splitlines():
        if line.startswith("STEP:"):
            step = line.split(":", 1)[1].strip()
            break
    vocabulary = [
        "loaded", "normalised", "partitioned", "computed", "validated",
        "recorded", "compared", "reconciled", "summarised", "documented",
        "sampled", "verified", "measured", "aggregated", "inspected",
    ]
    verbs = [vocabulary[(n >> i) % len(vocabulary)] for i in range(0, 24, 4)]
    return json.dumps(
        {
            "summary": f"{step}: {', '.join(dict.fromkeys(verbs))} the inputs "
                       f"and wrote the artifact for downstream checks",
            "value": (n % 900) / 10.0,
            "step": step,
            "evidence": sorted(set(verbs)),
        }
    )


def _json_for_schema(prompt: str, n: int) -> str:
    """Synthesize a valid payload for the last JSON schema in the prompt.

    Structured stages ask for a schema; returning prose would make every
    structured call fail its parse and exercise only the repair loop. Bounds
    from the schema are respected (`minimum`/`maximum`) so a field declared
    0..10 never yields 47 -- otherwise validation failures would look like
    model errors rather than the deliberate fiction they are.
    """
    matches = re.findall(r"\{.*\}", prompt, flags=re.DOTALL)
    if not matches:
        return "{}"
    try:
        schema = json.loads(matches[-1])
    except json.JSONDecodeError:
        return "{}"

    payload: dict[str, Any] = {}
    for i, (field_name, spec) in enumerate(schema.get("properties", {}).items()):
        kind = spec.get("type", "string")
        shift = (i * 7) % 24
        if kind == "integer":
            lo = spec.get("minimum", 0)
            hi = spec.get("maximum", 100)
            payload[field_name] = lo + (n >> shift) % max(hi - lo + 1, 1)
        elif kind == "number":
            payload[field_name] = round(((n >> shift) % 1000) / 10, 1)
        elif kind == "boolean":
            payload[field_name] = bool((n >> shift) & 1)
        elif kind == "array":
            payload[field_name] = [f"item-{(n >> shift) % 7}"]
        else:
            payload[field_name] = f"simulated-{field_name}-{(n >> shift) % 97}"
    return json.dumps(payload)
