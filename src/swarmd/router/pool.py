"""Multi-provider pool: free tiers first, paid overflow last, limits discovered.

Why a pool at all. A single free tier cannot carry a run of hundreds of agents:
the binding constraint is not money, it is throughput. Measured 2026-08-27, no
single provider offers more than ~30k TPM free, but five of them together offer
roughly 86k TPM (~34 LLM calls/min). The pool is what turns five small quotas
into one usable one.

Why one generic adapter instead of five SDKs. Groq, Cerebras, Google AI Studio,
Mistral and OpenRouter all expose OpenAI-compatible chat-completions endpoints,
so they differ only in base URL, key, and model ids. Five vendor SDKs would add
five dependency trees and five auth quirks to express the same POST. When a
provider eventually breaks the contract, it gets a subclass -- not the other
four.

Why limits are DISCOVERED, not configured. Published free-tier limits disagree
across sources (OpenRouter's daily cap is variously documented as 50, 200, and
1000) and change without notice. A hardcoded constant is therefore wrong on
arrival and silently wrong later. The pool treats a 429 as the authoritative
statement of a limit: it records what the provider actually said, backs that
provider off, and routes elsewhere. Published numbers are used only as an
initial ordering hint.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

from swarmd.ledger import CostAccount
from swarmd.router.providers import (
    LLMRequest,
    LLMResponse,
    Provider,
    ProviderError,
)

# --- provider catalogue ----------------------------------------------------
#
# Tier ordering is the whole cost strategy in one column:
#   free                -> use freely
#   free_data_training  -> quota paid for in data, requires explicit opt-in
#   paid                -> real money, only when free capacity is exhausted


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key_env: str
    models: tuple[str, ...]
    tier: str = "free"
    # Published limits, used ONLY as an initial ordering hint. The pool
    # overwrites its behaviour with what a 429 actually tells it.
    hint_rpm: int = 20
    hint_tpm: int = 6_000


REGISTRY: dict[str, ProviderSpec] = {
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        hint_rpm=30,
        hint_tpm=6_000,
    ),
    "cerebras": ProviderSpec(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
        models=("llama-3.3-70b", "llama3.1-8b"),
        hint_rpm=30,
        hint_tpm=30_000,
    ),
    "google-aistudio": ProviderSpec(
        name="google-aistudio",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GOOGLE_API_KEY",
        models=("gemini-2.5-flash", "gemini-2.5-flash-lite"),
        hint_rpm=15,
        hint_tpm=250_000,
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        models=(
            "z-ai/glm-5.2:free",
            "minimax/minimax-m3:free",
            "nvidia/nemotron-3-ultra-550b:free",
        ),
        hint_rpm=20,
        hint_tpm=20_000,
    ),
    # Quota is granted in exchange for consenting to have prompts used for
    # training. Never enabled unless the caller passes allow_data_training.
    "mistral-free": ProviderSpec(
        name="mistral-free",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        models=("mistral-small-latest", "open-mistral-nemo"),
        tier="free_data_training",
        hint_rpm=60,
        hint_tpm=50_000,
    ),
    # Paid overflow. Cheapest capable model with a large context window, which
    # is what an agent carrying retrieved skills actually needs.
    "openrouter-paid": ProviderSpec(
        name="openrouter-paid",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        models=("z-ai/glm-5.3-flash", "qwen/qwen-3.8-flash"),
        tier="paid",
        hint_rpm=60,
        hint_tpm=200_000,
    ),
}

TIER_RANK = {"free": 0, "free_data_training": 1, "paid": 2}


class RateLimited(RuntimeError):
    """A provider said 429. Carries what it told us about when to come back."""

    def __init__(self, provider: str, retry_after_s: float | None) -> None:
        super().__init__(f"{provider} rate limited (retry_after={retry_after_s})")
        self.provider = provider
        self.retry_after_s = retry_after_s


class NoCapacity(RuntimeError):
    """Every provider in the pool is backed off or unavailable."""


# --- generic adapter -------------------------------------------------------


class OpenAICompatProvider(Provider):
    """One adapter for every OpenAI-compatible chat-completions endpoint."""

    def __init__(self, spec: ProviderSpec, timeout_s: float = 60.0) -> None:
        import httpx

        key = os.environ.get(spec.api_key_env)
        if not key:
            raise RuntimeError(
                f"{spec.name} requires {spec.api_key_env}; unset. Either export it "
                f"or drop {spec.name} from the pool."
            )
        self.spec = spec
        self.name = spec.name
        self.models = list(spec.models)
        self._client = httpx.AsyncClient(
            base_url=spec.base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout_s,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self.complete_with(self.models[0], request)

    async def complete_with(self, model: str, request: LLMRequest) -> LLMResponse:
        import httpx

        start = time.monotonic()
        try:
            resp = await self._client.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": _messages(request),
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}/{model}: transport: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimited(self.name, _retry_after(resp))
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.name}/{model}: HTTP {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        usage = data.get("usage", {})
        return LLMResponse(
            text=data["choices"][0]["message"]["content"],
            provider=self.name,
            model=model,
            latency_s=time.monotonic() - start,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _messages(request: LLMRequest) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    if request.system:
        msgs.append({"role": "system", "content": request.system})
    msgs.append({"role": "user", "content": request.prompt})
    return msgs


def _retry_after(resp: Any) -> float | None:
    """Read the provider's own statement of when to come back.

    Providers disagree on the header (`retry-after` in seconds, or an
    `x-ratelimit-reset-*` variant). Anything unparseable returns None, and the
    caller falls back to exponential backoff rather than guessing.
    """
    for header in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = resp.headers.get(header)
        if not raw:
            continue
        try:
            return max(0.0, float(str(raw).rstrip("s")))
        except ValueError:
            continue
    return None


# --- empirical limit state -------------------------------------------------


@dataclass
class LimitState:
    """What a provider has actually demonstrated about its limits.

    ANATOMY: backoff_base_s / backoff_cap_s
      When a provider 429s without telling us when to return, we back off
      exponentially: base * 2**consecutive_429s, capped. Why base 2s: shorter
      than any observed free-tier window, so the first retry is cheap if the
      429 was a burst rather than a quota. Why cap 120s: past two minutes a
      provider is effectively out for this run and the pool should be routing
      elsewhere rather than politely waiting.

    ANATOMY: recovery_halflife_s
      How fast the 429 streak decays once calls start succeeding again. Why 60s:
      free-tier windows are typically per-minute, so a provider that has been
      healthy for a minute has genuinely recovered rather than briefly paused.
    """

    backoff_base_s: float = 2.0
    backoff_cap_s: float = 120.0
    recovery_halflife_s: float = 60.0

    consecutive_429s: int = 0
    total_429s: int = 0
    successes: int = 0
    errors: int = 0
    blocked_until: float = 0.0
    last_success_ts: float = 0.0
    # The shortest interval at which this provider was actually observed to
    # reject. This is the discovered limit -- more trustworthy than any doc.
    observed_min_interval_s: float | None = None
    _last_429_ts: float = 0.0

    def available(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) >= self.blocked_until

    def wait_s(self, now: float | None = None) -> float:
        return max(0.0, self.blocked_until - (now or time.monotonic()))

    def record_429(self, retry_after_s: float | None) -> None:
        now = time.monotonic()
        self.consecutive_429s += 1
        self.total_429s += 1
        if self._last_429_ts:
            interval = now - self._last_429_ts
            if self.observed_min_interval_s is None:
                self.observed_min_interval_s = interval
            else:
                self.observed_min_interval_s = min(
                    self.observed_min_interval_s, interval
                )
        self._last_429_ts = now
        # The provider's own number wins; our backoff is the fallback.
        if retry_after_s is not None:
            delay = retry_after_s
        else:
            delay = min(
                self.backoff_cap_s,
                self.backoff_base_s * (2 ** (self.consecutive_429s - 1)),
            )
        self.blocked_until = now + delay

    def record_success(self) -> None:
        now = time.monotonic()
        self.successes += 1
        self.last_success_ts = now
        # Decay rather than reset: one success after a hard throttle does not
        # mean the quota refilled, and resetting to zero makes the pool
        # oscillate between hammering and backing off.
        # >= not >: a coarse monotonic clock (Windows ticks at ~15ms) can report
        # a zero delta between a 429 and the success after it, which under a
        # strict > would silently disable decay entirely.
        if self.consecutive_429s and now - self._last_429_ts >= self.recovery_halflife_s:
            self.consecutive_429s = max(0, self.consecutive_429s - 1)

    def record_error(self) -> None:
        self.errors += 1

    def score(self) -> float:
        """Lower is better. Blends error rate with throttle pressure.

        Rate-limit rejections count separately from errors because they mean
        something different: an error suggests the provider is broken, a 429
        suggests it is working and we are asking too fast. Both demote, but a
        throttled provider should recover its position faster than a broken one.
        """
        total = self.successes + self.errors + self.total_429s
        if total == 0:
            return 0.0
        return (self.errors * 1.0 + self.total_429s * 0.5) / total


# --- the pool --------------------------------------------------------------


@dataclass
class _Slot:
    provider: OpenAICompatProvider
    spec: ProviderSpec
    state: LimitState = field(default_factory=LimitState)


class ProviderPool(Provider):
    """Routes a request across many providers, cheapest capacity first.

    Ordering is tier first, then discovered health. Tier dominates because the
    cost difference between a free tier and paid overflow is categorical, not
    incremental -- a slightly-throttled free provider is still the right answer
    over a healthy paid one, right up until nothing free is available.

    ANATOMY: allow_data_training
      Off by default. Admits the Mistral Experiment tier, whose quota is granted
      in exchange for consenting to have submitted prompts used for training.
      It is a flag rather than a config default because that tier's price is
      paid in data instead of dollars, and that should be a decision someone
      makes on purpose.

    ANATOMY: allow_paid
      Off by default. Admits the paid overflow tier. With it off, exhausting
      free capacity raises NoCapacity -- the run stops rather than quietly
      spending money.

    ANATOMY: max_wait_s
      How long a call may wait for the soonest provider to come off backoff
      before giving up. Why 30s: longer than a typical per-minute window's
      remainder, short enough that a run of hundreds of agents does not
      deadlock behind one exhausted quota. Exceeding it raises NoCapacity,
      which the caller reports as degraded throughput rather than failure.
    """

    name = "pool"

    def __init__(
        self,
        slots: list[_Slot],
        *,
        account: CostAccount | None = None,
        allow_paid: bool = False,
        max_wait_s: float = 30.0,
    ) -> None:
        if not slots:
            raise ValueError("ProviderPool needs at least one provider")
        self._slots = slots
        self.account = account
        self.allow_paid = allow_paid
        self.max_wait_s = max_wait_s

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        *,
        account: CostAccount | None = None,
        allow_data_training: bool = False,
        allow_paid: bool = False,
        include: list[str] | None = None,
        max_wait_s: float = 30.0,
    ) -> ProviderPool:
        """Build a pool from whichever provider keys are actually present.

        A missing key is a skip, not an error: the pool's whole premise is that
        capacity is assembled from whatever is available. Zero usable providers
        IS an error, raised here rather than discovered on the first call.
        """
        slots: list[_Slot] = []
        skipped: list[str] = []
        for name, spec in REGISTRY.items():
            if include is not None and name not in include:
                continue
            if spec.tier == "free_data_training" and not allow_data_training:
                skipped.append(f"{name} (needs --allow-data-training)")
                continue
            if spec.tier == "paid" and not allow_paid:
                skipped.append(f"{name} (needs --allow-paid)")
                continue
            try:
                slots.append(_Slot(OpenAICompatProvider(spec), spec))
            except RuntimeError:
                skipped.append(f"{name} (no {spec.api_key_env})")
        if not slots:
            raise RuntimeError(
                "no usable providers. Skipped: " + ", ".join(skipped or ["none"])
            )
        return cls(slots, account=account, allow_paid=allow_paid, max_wait_s=max_wait_s)

    # -- routing ------------------------------------------------------------

    def _ordered(self) -> list[_Slot]:
        return sorted(
            self._slots,
            key=lambda s: (TIER_RANK.get(s.spec.tier, 9), s.state.score()),
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        agent_id = str(request.metadata.get("agent_id", ""))
        stage = str(request.metadata.get("stage", ""))
        errors: list[str] = []
        deadline = time.monotonic() + self.max_wait_s

        while True:
            ordered = self._ordered()
            for slot in ordered:
                if not slot.state.available():
                    continue
                for model in slot.provider.models:
                    if self.account is not None:
                        # Refuse before spending, not after.
                        self.account.precheck(
                            slot.spec.name, model, request.max_tokens
                        )
                    try:
                        resp = await slot.provider.complete_with(model, request)
                    except RateLimited as exc:
                        slot.state.record_429(exc.retry_after_s)
                        errors.append(f"{slot.spec.name}: 429")
                        break  # whole provider is throttled, not just this model
                    except ProviderError as exc:
                        slot.state.record_error()
                        errors.append(str(exc))
                        continue  # try the next model on the same provider
                    slot.state.record_success()
                    if self.account is not None:
                        self.account.charge_call(
                            provider=resp.provider,
                            model=resp.model,
                            tokens_in=resp.tokens_in,
                            tokens_out=resp.tokens_out,
                            agent_id=agent_id,
                            stage=stage,
                            detail={"latency_s": round(resp.latency_s, 4)},
                        )
                    return resp

            # Nothing usable right now. Wait for the soonest to come back, if
            # that fits inside the deadline.
            waits = [s.state.wait_s() for s in ordered if not s.state.available()]
            now = time.monotonic()
            if not waits or now >= deadline:
                raise NoCapacity(
                    f"no provider capacity within {self.max_wait_s}s; "
                    + "; ".join(errors[-5:])
                )
            sleep_for = min(min(waits), deadline - now)
            if sleep_for <= 0:
                raise NoCapacity("no provider capacity; " + "; ".join(errors[-5:]))
            await asyncio.sleep(sleep_for)

    # -- introspection ------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """Live view of the pool. Feeds the CLI probe and the dashboard."""
        return [
            {
                "provider": s.spec.name,
                "tier": s.spec.tier,
                "models": list(s.spec.models),
                "available": s.state.available(),
                "wait_s": round(s.state.wait_s(), 2),
                "successes": s.state.successes,
                "errors": s.state.errors,
                "rate_limits": s.state.total_429s,
                "observed_min_interval_s": s.state.observed_min_interval_s,
                "score": round(s.state.score(), 4),
            }
            for s in self._ordered()
        ]

    async def probe(self, prompt: str = "ping") -> list[dict[str, Any]]:
        """Send one tiny request per provider to discover what is actually live.

        This is the honest answer to documentation that disagrees with itself:
        ask the provider. Runs providers concurrently but models serially within
        one provider, since a 429 from the first model means the second will
        429 too and probing it would only deepen the backoff.
        """

        async def one(slot: _Slot) -> dict[str, Any]:
            req = LLMRequest(prompt=prompt, max_tokens=8, temperature=0.0)
            start = time.monotonic()
            try:
                resp = await slot.provider.complete_with(slot.provider.models[0], req)
            except RateLimited as exc:
                slot.state.record_429(exc.retry_after_s)
                return {
                    "provider": slot.spec.name,
                    "tier": slot.spec.tier,
                    "ok": False,
                    "reason": "rate_limited",
                    "retry_after_s": exc.retry_after_s,
                }
            except ProviderError as exc:
                slot.state.record_error()
                return {
                    "provider": slot.spec.name,
                    "tier": slot.spec.tier,
                    "ok": False,
                    "reason": str(exc)[:160],
                }
            slot.state.record_success()
            return {
                "provider": slot.spec.name,
                "tier": slot.spec.tier,
                "ok": True,
                "model": resp.model,
                "latency_s": round(time.monotonic() - start, 3),
                "tokens_in": resp.tokens_in,
                "tokens_out": resp.tokens_out,
            }

        return list(await asyncio.gather(*(one(s) for s in self._slots)))

    async def aclose(self) -> None:
        await asyncio.gather(*(s.provider.aclose() for s in self._slots))
