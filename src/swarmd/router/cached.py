"""A provider that answers from the semantic cache when it honestly can.

`SemanticCache` has existed since Phase 4 and nothing called it. The ledger has
had `charge_cache_hit` for as long, and nothing called that either, so the
`cache_hits` figure on every report and dashboard has been a structural zero --
not "we measured no hits", but "no code path can produce one". CAPACITY.md
nonetheless counted a 2.5x saving from caching toward its wall-clock argument.
This closes that gap.

Wrapping the provider, rather than caching inside the worker, is deliberate:
criterion synthesis, plan synthesis, batched generation and repairs all go
through `.complete()`, so one wrapper covers every call the system makes
instead of the one path someone remembered to instrument.

THREE THINGS THE KEY MUST INCLUDE, each learned from a way this goes wrong.

  Temperature and max_tokens. Criterion synthesis runs N proposers whose
  diversity comes partly from sampling. Keying on prompt text alone would serve
  proposer 1's answer to proposers 2 and 3, and a consensus of one identical
  opinion is not a consensus.

  The system prompt. Same user text under a different system prompt is a
  different question.

  The simulated flag. A cached synthetic response served into a real run would
  launder synthetic data past the taint tracking of ADR-012 -- the run would
  report real, and one of its answers would not be. Simulated and live entries
  therefore cannot collide, and a hit on a simulated entry writes a ledger row
  marked simulated exactly as the original call did.

WHAT IS NOT CACHED, and why the exclusion is enforced rather than documented:
evaluation runs. An eval measures variance across repeats. Serving repeat 2
from repeat 1's cache makes them identical, which does not merely bias the
bootstrap CI -- it collapses it toward zero width and turns "no measured
improvement" into whatever the first sample happened to say. `SwarmRun` refuses
to construct an eval profile with a cache attached.
"""

from __future__ import annotations

import logging
from typing import Any

from swarmd.observability import metrics
from swarmd.router.cache import SemanticCache

logger = logging.getLogger(__name__)


def _would_have_cost(response: Any) -> float:
    """What this hit avoided, priced from the same table a real call uses.

    An unpriced model prices at zero rather than raising: a pricing gap must
    not turn a cache hit into a failed run.
    """
    from swarmd.ledger import UnpricedModel, price_for

    try:
        price = price_for(str(response.provider), str(response.model))
    except UnpricedModel:
        return 0.0
    return price.cost(int(response.tokens_in), int(response.tokens_out))


def cache_key(request: Any, *, simulated: bool) -> str:
    """Everything that changes the answer, in one string.

    The cache embeds this, so ordering matters less than presence: a field left
    out cannot be recovered by similarity.
    """
    return (
        f"sim={int(simulated)}|t={round(float(request.temperature), 2)}"
        f"|m={int(request.max_tokens)}|s={request.system}\n{request.prompt}"
    )


class CachedProvider:
    """Wraps a provider with a semantic cache and honest accounting.

    Delegates every attribute it does not define, so a pool's `account`,
    `name`, `probe` and the rest keep working through the wrapper -- a proxy
    that quietly drops the wrapped object's interface is how a cost ceiling
    stops being wired up.
    """

    def __init__(
        self,
        provider: Any,
        cache: SemanticCache,
        *,
        account: Any = None,
        stage: str = "",
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._account = account
        self._stage = stage
        self.hits = 0
        self.misses = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    @property
    def account(self) -> Any:
        return self._account

    @account.setter
    def account(self, value: Any) -> None:
        # SwarmRun assigns this so the pool charges the run's ledger. The
        # wrapper must accept it AND pass it down, or the ledger silently stops
        # seeing calls the moment caching is enabled.
        self._account = value
        if hasattr(self._provider, "account"):
            self._provider.account = value

    async def complete(self, request: Any) -> Any:
        from swarmd.router.simulated import simulation_enabled

        simulated = simulation_enabled()
        key = cache_key(request, simulated=simulated)

        hit = await self._cache.get(key)
        if hit is not None:
            self.hits += 1
            stage = str(request.metadata.get("stage", self._stage or "unknown"))
            metrics.record_cache_hit(stage=stage, saved_usd=_would_have_cost(hit))
            if self._account is not None:
                # A row, not a counter. The saving is then a query over the
                # ledger -- `would_have_cost` summed over cache_hit rows --
                # rather than a number some code path incremented.
                self._account.charge_cache_hit(
                    provider=str(hit.provider),
                    model=str(hit.model),
                    tokens_in=int(hit.tokens_in),
                    tokens_out=int(hit.tokens_out),
                    agent_id=str(request.metadata.get("agent_id", "")),
                    stage=stage,
                    # Taint travels with the entry, not with the run doing the
                    # reading. Keyed apart as well, so this is belt and braces
                    # for the one row that would otherwise misreport.
                    simulated=simulated or str(hit.provider) == "simulated",
                )
            return hit

        self.misses += 1
        response = await self._provider.complete(request)
        await self._cache.put(key, response)
        return response

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def report(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 4),
            "entries": len(self._cache),
            "evictions": self._cache.evictions,
        }
