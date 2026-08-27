"""Batched generation: one model call, K candidate solutions.

CAPACITY.md's 500-agent figure rests on four levers, and this is the one that
does the heaviest lifting. It was also, until now, the one that did not exist:
every agent made its own call, so the request count grew linearly with the
population and the pool had to be capped at 16 per node to stay inside the
ceiling.

WHAT IS ACTUALLY SCARCE. Not dollars -- the free tiers cost nothing. REQUESTS
PER MINUTE. A pooled free tier gives roughly 45 rpm, so the request count is
what decides whether a run finishes in fifteen minutes or ninety. Batching
turns a pool of K agents into ONE request whose output carries K distinct
candidates. Output tokens grow roughly K-fold; requests and prompt tokens do
not, and requests are the binding constraint.

HOW IT REACHES THE AGENTS, and why there is no coordination machinery here.
The obvious design is a broker that coalesces concurrent identical requests,
which needs a batching window, a timeout, and a story for what happens when
fewer callers arrive than expected -- three sources of flakiness.

Instead the run generates the batch BEFORE spawning the pool and pre-seeds each
agent's checkpoint with its variant, marked `generate:1`. The worker's resume
path -- already built, already tested, already the mechanism that stops killed
work being redone -- then skips the generate step and charges nothing for it.
Batching and recovery turn out to be the same operation seen from two sides:
work someone else already did.

WHAT THIS DOES NOT DO. It does not batch repairs. A repair prompt carries the
specific failures of one candidate, so two agents repairing different
candidates are not asking the same question, and pretending otherwise would
trade the diversity the population exists for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The separator the model is asked to put between candidates. Chosen to be
# something no legitimate JSON or prose output contains, so a candidate that
# happens to discuss "candidates" does not split itself in half.
SEPARATOR = "===CANDIDATE==="

BATCH_INSTRUCTION = (
    "Produce {k} SEPARATE and genuinely different candidate solutions for the "
    "step above. Different approaches, not rewordings of one approach. Put a "
    "line containing exactly {sep} between candidates, and nothing before the "
    "first or after the last."
)


@dataclass(frozen=True, slots=True)
class Batch:
    """K variants from one call, with the accounting to prove it."""

    variants: tuple[str, ...]
    requested: int
    calls: int
    cost_credits: float

    @property
    def saved_calls(self) -> int:
        """Calls this batch avoided.

        Reported rather than estimated: the ledger holds the calls that WERE
        made, and this is the difference against one-call-per-agent.
        """
        return max(0, self.requested - self.calls)

    def for_agent(self, index: int) -> str:
        """Variant for the agent at this position in the pool.

        Wraps round-robin when the model returned fewer variants than asked
        for. Two agents sharing a variant is a real loss of diversity, so it is
        visible in `variants` rather than papered over by re-calling until the
        count matches -- which would spend exactly the requests batching exists
        to save.
        """
        if not self.variants:
            return ""
        return self.variants[index % len(self.variants)]


def split_candidates(text: str, *, expected: int) -> tuple[str, ...]:
    """Parse a batched response into its candidates.

    Tolerant on purpose. A model that returns one candidate, or forgets the
    separator, or wraps it in whitespace, must degrade to a smaller batch
    rather than failing the node -- the batch is an optimisation, and an
    optimisation that can break correctness is not one.
    """
    if not text.strip():
        return ()
    parts = [part.strip() for part in text.split(SEPARATOR)]
    variants = tuple(part for part in parts if part)
    if len(variants) > expected:
        # More than asked for is harmless; take the first K so pool position
        # maps to variant deterministically.
        return variants[:expected]
    return variants


def batch_prompt(base_prompt: str, k: int) -> str:
    return f"{base_prompt}\n\n{BATCH_INSTRUCTION.format(k=k, sep=SEPARATOR)}"


async def generate_batch(
    *,
    provider: Any,
    prompt: str,
    k: int,
    max_tokens: int,
    temperature: float,
    system: str,
    stage: str = "",
) -> Batch:
    """One call, K candidates.

    `max_tokens` is scaled by K and then capped. Without the scaling the model
    truncates mid-candidate and the batch silently returns fewer variants than
    asked for; with an uncapped scale a wide pool asks for a context window no
    free-tier model has.
    """
    from swarmd.router.providers import LLMRequest

    if k <= 1:
        # A batch of one is a normal call with extra instructions the model has
        # to read. Skip the ceremony.
        request = LLMRequest(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata={"stage": stage, "batch": "1"},
        )
        response = await provider.complete(request)
        return Batch((str(response.text),), requested=1, calls=1, cost_credits=0.0)

    request = LLMRequest(
        prompt=batch_prompt(prompt, k),
        system=system,
        temperature=temperature,
        max_tokens=min(max_tokens * k, 8192),
        metadata={"stage": stage, "batch": str(k)},
    )
    from swarmd.ledger import CeilingExceeded

    try:
        response = await provider.complete(request)
    except CeilingExceeded:
        # NOT survivable, and not this function's decision to survive. The
        # ceiling exists to stop a run that is spending too much; catching it
        # here would let the pool fall back to individual generation and spend
        # MORE, one call at a time, past the limit that just fired.
        raise
    except Exception as exc:  # noqa: BLE001 - a provider failure is data
        # The pool falls back to generating individually. Losing the saving is
        # survivable; losing the node is not.
        logger.warning("batch generation failed for %s: %s", stage, exc)
        return Batch((), requested=k, calls=1, cost_credits=0.0)

    variants = split_candidates(str(response.text), expected=k)
    if len(variants) < k:
        logger.info(
            "batch for %s returned %d of %d requested candidates",
            stage, len(variants), k,
        )
    return Batch(variants, requested=k, calls=1, cost_credits=0.0)
