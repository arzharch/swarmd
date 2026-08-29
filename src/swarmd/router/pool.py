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
from swarmd.observability import metrics
from swarmd.router.budget import (
    LONG_WINDOW_S,
    BudgetTracker,
    RateHeaders,
    parse_duration,
    parse_rate_headers,
)
from swarmd.router.pacer import Pacer, PauseCause
from swarmd.router.providers import (
    LLMRequest,
    LLMResponse,
    Provider,
    ProviderError,
)
from swarmd.router.quota import QuotaBackend, build_quota
from swarmd.router.ration import Ration, build_ration

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


# EVERY MODEL ID BELOW WAS CALLED BEFORE IT WAS WRITTEN DOWN, on 2026-08-28,
# with the keys in this deployment. That is not ceremony. The previous table
# was assembled from documentation and every single entry in it was wrong:
#
#   groq            llama-3.3-70b-versatile   404 - the llama models are gone
#   cerebras        (all four)                402 - free tier now needs a card
#   google          gemini-2.5-flash          404 - "no longer available to
#                                                   new users"
#   openrouter      nvidia/nemotron-3-ultra   400 - not a valid model ID
#
# A provider table that has never been called is a list of plausible strings.
# `swarmd providers probe` re-runs this check; run it when calls start failing
# for a reason that looks like an outage, because a silently retired model
# looks exactly like one.
#
# Latency and throughput are measured on the prompt shape workers actually
# send -- a step, a requirement, JSON only -- not on a one-token ping, which
# measures the network and nothing else.

REGISTRY: dict[str, ProviderSpec] = {
    # Fastest by a wide margin: 384 tok/s, 0.81s to a complete structured
    # answer, roughly 4x the next provider. Ordered first for that reason.
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        # Order is by DAILY headroom, not latency. All three share 30 RPM and
        # 8K TPM, but qwen/qwen3.8-27b carries 2,000,000 tokens/day against
        # 200,000 for the gpt-oss pair -- ten times the runway. A day of
        # swarm work is hundreds of thousands of tokens, so the model with the
        # deepest daily well goes first and the fast ones take the overflow.
        models=("qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"),
        hint_rpm=30,
        hint_tpm=8_000,
    ),
    # Second-fastest and by far the largest daily allowance. The `-lite`
    # models answer in ~1.2s; plain `gemini-3.5-flash` took 5.5s on the same
    # prompt, which is why it is last in this tuple rather than first.
    "google-aistudio": ProviderSpec(
        name="google-aistudio",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GOOGLE_API_KEY",
        models=(
            "gemini-3.5-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.5-flash",
        ),
        hint_rpm=15,
        hint_tpm=250_000,
    ),
    # A GRANT, not a tier: ~1,000 credits that never refill and expire 30 days
    # after issue (see router/budget.py). Ranked behind the replenishing free
    # tiers deliberately -- spending a finite pool first because it costs
    # nothing is how the month's burst capacity disappears in week one.
    #
    # Note the model IDs: `/v1/models` lists 83 entries and this account can
    # call four of them. The rest return 404 "Not found for account", so the
    # catalogue is not an entitlement list.
    "nvidia-nim": ProviderSpec(
        name="nvidia-nim",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_NIM_API_KEY",
        models=(
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-nano-30b-a3b",
            "openai/gpt-oss-20b",
        ),
        tier="free_grant",
        hint_rpm=40,
        hint_tpm=60_000,
    ),
    # 50 requests/day on an unfunded account. Real, and small enough that it is
    # a tie-breaker rather than a workhorse. `minimax-m3:free` is the one of
    # the three previously listed that both exists and answers.
    "openrouter": ProviderSpec(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        models=("minimax/minimax-m3:free",),
        hint_rpm=20,
        hint_tpm=20_000,
    ),
    # Quota granted in exchange for consenting to have prompts used for
    # training. Never enabled unless the caller passes allow_data_training,
    # which is off in this deployment.
    "mistral-free": ProviderSpec(
        name="mistral-free",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        models=("open-mistral-nemo", "mistral-small-latest"),
        tier="free_data_training",
        hint_rpm=60,
        hint_tpm=500_000,
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

# Cerebras is NOT in this table. Its key returns 402 "Payment required to
# access this resource" on every model, so its free tier no longer exists for
# new accounts without a card on file. Listing it would advertise capacity the
# pool cannot use, and the pool would keep trying it and keep failing.

# Ordering is the cost strategy in one line. `free_grant` sits between the
# replenishing free tiers and the ones with a non-money price, because a grant
# is free but FINITE: using it before a tier that refills tomorrow trades a
# permanent resource for a renewable one.
TIER_RANK = {
    "simulated": 0,
    "free": 1,
    "free_grant": 2,
    "free_data_training": 3,
    "paid": 4,
}


def credentials_for(spec: ProviderSpec) -> list[tuple[str, str]]:
    """Discover every credential configured for a provider.

    Two forms are read, singular and plural:

        GROQ_API_KEY   = "abc"            -> one credential
        GROQ_API_KEYS  = "abc,def,ghi"    -> three

    Multiple credentials exist because a real deployment separates keys per
    environment, and because quota is per account: two accounts genuinely are
    twice the throughput, and the pool has to model that rather than assume one
    bucket per provider.

    Returns (credential_id, key) pairs. The id is a stable, non-secret handle
    used as the quota key and as a metric label -- deliberately an index rather
    than a key prefix, so a credential identifier can never leak key material
    into logs, metrics, or the dashboard.
    """
    found: list[tuple[str, str]] = []
    plural = os.environ.get(f"{spec.api_key_env}S", "")
    for i, raw in enumerate(plural.split(",")):
        key = raw.strip()
        if key:
            found.append((f"{spec.name}#{i}", key))
    single = os.environ.get(spec.api_key_env, "").strip()
    if single and single not in {k for _, k in found}:
        found.append((f"{spec.name}#{len(found)}", single))
    return found


class RateLimited(RuntimeError):
    """A provider said 429. Carries what it told us about when to come back.

    ANATOMY: daily
      True when the wait is longer than `LONG_WINDOW_S`, which for every
      provider in this pool means a day rather than a minute. The two need
      different responses and conflating them is expensive in both directions:
      backing a provider off for four hours over a one-minute bucket throws
      away most of a day's capacity, and retrying a spent day every two seconds
      earns a rejection per retry, several of which the provider charges to the
      day that is already spent.
    """

    def __init__(
        self,
        provider: str,
        retry_after_s: float | None,
        *,
        headers: RateHeaders | None = None,
    ) -> None:
        super().__init__(f"{provider} rate limited (retry_after={retry_after_s})")
        self.provider = provider
        self.retry_after_s = retry_after_s
        self.headers = headers or RateHeaders()

    @property
    def daily(self) -> bool:
        return (self.retry_after_s or 0.0) >= LONG_WINDOW_S


class NoCapacity(RuntimeError):
    """Every provider in the pool is backed off or unavailable."""


# --- generic adapter -------------------------------------------------------


class OpenAICompatProvider(Provider):
    """One adapter for every OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str,
        *,
        credential_id: str = "default",
        timeout_s: float = 60.0,
    ) -> None:
        import httpx

        if not api_key:
            raise RuntimeError(
                f"{spec.name} requires {spec.api_key_env}; unset. Either export it "
                f"or drop {spec.name} from the pool."
            )
        self.spec = spec
        self.name = spec.name
        self.credential_id = credential_id
        self.models = list(spec.models)
        self._client = httpx.AsyncClient(
            base_url=spec.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
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

        # Parsed on EVERY response, not just the rejected ones. A success that
        # says "zero tokens left, refills in two hours" is the same fact as a
        # 429, obtained one request earlier and without the rejection.
        headers = parse_rate_headers(resp.headers)
        if resp.status_code == 429:
            raise RateLimited(self.name, _retry_after(resp), headers=headers)
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.name}/{model}: HTTP {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        usage = data.get("usage", {})
        message = data["choices"][0]["message"]
        text = message.get("content") or ""

        if not text.strip():
            # AN EMPTY COMPLETION IS A FAILURE, not an answer, and saying so
            # here is what makes the pool fail over instead of handing "" to a
            # caller that has no way to tell it apart from a bad reply.
            #
            # This is not a theoretical case. `openai/gpt-oss-20b` is a
            # REASONING model: given a long prompt it spends the entire output
            # budget thinking and returns content="" with completion_tokens at
            # the cap. Criterion synthesis then reported "no proposer produced
            # a parseable criterion" and refused to run -- blaming the models
            # for output they were never given room to write.
            #
            # Raised rather than retried in place: the pool already knows how
            # to try the next model and the next provider, and duplicating that
            # here would give one failure two different recovery paths.
            finish = data["choices"][0].get("finish_reason", "?")
            raise ProviderError(
                f"{self.name}/{model}: empty completion "
                f"(finish_reason={finish}, completion_tokens="
                f"{usage.get('completion_tokens', 0)}). A reasoning model that "
                f"exhausts max_tokens before answering looks exactly like this."
            )

        return LLMResponse(
            text=text,
            provider=self.name,
            model=model,
            latency_s=time.monotonic() - start,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            rate_headers=headers,
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

    Providers disagree on both the header and its format: `retry-after` is a
    bare integer, Groq answers `x-ratelimit-reset-tokens` with "2m59.56s", and
    OpenRouter with "7.66". `parse_duration` handles all three -- the previous
    `float(raw.rstrip("s"))` turned "2m59.56s" into "2m59.56" and threw, so the
    one provider that states its reset most precisely was the one whose word we
    discarded, falling back to a guessed backoff instead.

    When no explicit retry is given, the reset for an EXHAUSTED dimension is
    used before any other: a dimension with headroom left says nothing about
    when the one that ran out comes back. Anything unparseable returns None and
    the caller falls back to exponential backoff rather than guessing.
    """
    for header in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = resp.headers.get(header)
        if not raw:
            continue
        try:
            value = parse_duration(str(raw))
            if value is not None:
                return max(0.0, value)
        except ValueError:
            pass
    headers = parse_rate_headers(resp.headers)
    empty = [
        reset
        for remaining, reset in (
            (headers.remaining_requests, headers.reset_requests_s),
            (headers.remaining_tokens, headers.reset_tokens_s),
        )
        if reset is not None and remaining == 0
    ]
    if empty:
        return max(0.0, max(empty))
    known = [
        reset
        for reset in (headers.reset_requests_s, headers.reset_tokens_s)
        if reset is not None
    ]
    return max(0.0, max(known)) if known else None


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
    credential_id: str = "default"
    state: LimitState = field(default_factory=LimitState)

    @property
    def simulated(self) -> bool:
        return self.spec.tier == "simulated"

    @property
    def quota_key(self) -> str:
        """Quota is per CREDENTIAL, not per provider.

        Two Groq keys are two accounts and genuinely two quotas; keying on the
        provider name alone would throttle them as if they shared one.
        """
        return self.credential_id


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
        quota: QuotaBackend | None = None,
        budget: BudgetTracker | None = None,
        ration: Ration | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        if not slots:
            raise ValueError("ProviderPool needs at least one provider")
        self._slots = slots
        self.account = account
        self.allow_paid = allow_paid
        self.max_wait_s = max_wait_s
        self.quota = quota if quota is not None else build_quota(None)
        self._quota_ready = False
        # Long-window budgets: hour, 5-hour session, day, week, month. The
        # quota bucket above governs the next second; this governs whether
        # there is anything left to spend this week. Separate objects because
        # they answer different questions and have different lifetimes -- the
        # bucket is in memory, the budget survives restarts.
        self.budget = budget if budget is not None else BudgetTracker()
        # The SESSION ration: how much of a daily allowance this 6-hour slot
        # may spend. The budget tracker above answers "is there anything left
        # today"; this answers "is there anything left NOW", which is the
        # question that stops a single afternoon from consuming a day.
        self.ration = ration if ration is not None else build_ration(
            self.budget, os.environ.get("SWARMD_REDIS_URL")
        )
        # One pause per pool, shared by every agent in the air. Assigned by the
        # run so its events reach the run's stream; a pool built without one
        # still works and simply waits without narrating.
        self.pacer = pacer if pacer is not None else Pacer()

    async def _ensure_quota_configured(self) -> None:
        """Seed each credential's bucket from its published limit, once.

        Published limits are only the starting estimate; observed 429s tighten
        the bucket from there (ADR-008). Done lazily rather than in __init__
        because configuring a backend is async and constructors should not be.
        """
        if self._quota_ready:
            return
        for slot in self._slots:
            await self.quota.configure(
                slot.quota_key,
                rate_per_min=float(slot.spec.hint_rpm),
                burst=max(1, slot.spec.hint_rpm // 4),
                # The token dimension is usually the binding one. Groq allows
                # 30 requests and 8,000 tokens a minute, and this deployment's
                # calls average a little over 1,000 tokens: pacing requests
                # alone lets 30 legal requests carry 30,000 tokens into an
                # 8,000-token minute.
                tokens_per_min=float(slot.spec.hint_tpm),
                # No token_burst: it defaults to a whole minute's tokens.
                # Quartering it the way the request burst is quartered refuses
                # the first call of an idle minute -- one call is legitimately
                # a large share of a minute's token allowance -- and then
                # serialises every later call behind the refill.
            )
        self._quota_ready = True

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
        quota: QuotaBackend | None = None,
        budget: BudgetTracker | None = None,
        ration: Ration | None = None,
        pacer: Pacer | None = None,
    ) -> ProviderPool:
        """Build a pool from whichever provider keys are actually present.

        A missing key is a skip, not an error: the pool's whole premise is that
        capacity is assembled from whatever is available. Zero usable providers
        IS an error, raised here rather than discovered on the first call.
        """
        from swarmd.router.simulated import SimulatedProvider, simulation_enabled

        if simulation_enabled():
            # Exclusive, not additive. A pool mixing real and simulated
            # providers produces a run whose numbers mean nothing in
            # particular -- part measured, part invented, with no way to say
            # which half a given figure came from. Better to be entirely one
            # thing and have the ledger say which.
            spec = ProviderSpec(
                name="simulated",
                base_url="",
                api_key_env="",
                models=("simulated-v1",),
                tier="simulated",
                hint_rpm=100_000,
                hint_tpm=100_000_000,
            )
            sim = SimulatedProvider()
            return cls(
                [_Slot(sim, spec, credential_id="simulated#0")],  # type: ignore[arg-type]
                account=account,
                allow_paid=allow_paid,
                max_wait_s=max_wait_s,
                quota=quota,
                budget=budget,
                ration=ration,
                pacer=pacer,
            )

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
            creds = credentials_for(spec)
            if not creds:
                skipped.append(f"{name} (no {spec.api_key_env})")
                continue
            for credential_id, key in creds:
                slots.append(
                    _Slot(
                        OpenAICompatProvider(spec, key, credential_id=credential_id),
                        spec,
                        credential_id=credential_id,
                    )
                )
        if not slots:
            raise RuntimeError(
                "no usable providers. Skipped: " + ", ".join(skipped or ["none"])
            )
        return cls(
            slots,
            account=account,
            allow_paid=allow_paid,
            max_wait_s=max_wait_s,
            quota=quota,
            budget=budget,
            ration=ration,
            pacer=pacer,
        )

    # -- routing ------------------------------------------------------------

    def _ordered(self) -> list[_Slot]:
        return sorted(
            self._slots,
            key=lambda s: (TIER_RANK.get(s.spec.tier, 9), s.state.score()),
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await self._ensure_quota_configured()
        agent_id = str(request.metadata.get("agent_id", ""))
        stage = str(request.metadata.get("stage", ""))
        errors: list[str] = []
        deadline = time.monotonic() + self.max_wait_s

        while True:
            ordered = self._ordered()
            quota_waits: list[float] = []
            # Providers that have capacity today but not in this session. Kept
            # apart from `errors` because the remedy is different: an error
            # means try elsewhere, a ration refusal means come back later.
            ration_waits: list[PauseCause] = []

            for slot in ordered:
                if not slot.state.available():
                    continue

                # Budget gate, BEFORE the quota gate. The quota bucket refills,
                # so waiting on it is worth doing; a spent daily quota or an
                # exhausted grant does not refill on any timescale this call
                # can wait for, so the right move is to route elsewhere rather
                # than queue. Checking it second would mean sleeping on a
                # bucket for a provider that has nothing left to give.
                blocked = self.budget.blocked(slot.spec.name)
                if blocked:
                    errors.append(f"{slot.spec.name}: {blocked}")
                    continue

                # A SELF-REFILLING window that is momentarily full is a wait,
                # not a refusal. It used to be neither: `blocked` returned
                # "minute budget exhausted" and this gate skipped the provider
                # outright, so one busy minute took a workhorse out of routing
                # and the run reported "no provider capacity" while three
                # providers had a day's worth left between them.
                throttle, resets_in = self.budget.throttled(slot.spec.name)
                if throttle:
                    quota_waits.append(resets_in)
                    continue

                # RATION gate. The budget gate above asks whether the DAY has
                # anything left; this asks whether this six-hour session does.
                # Without it a single run empties a day in an afternoon and the
                # next session has nothing -- which is exactly what happened on
                # 2026-08-28, when one eval sweep took the NVIDIA grant from
                # 1,000 to 0 and left groq token-blocked for the rest of the
                # day.
                #
                # A refusal here is NOT an error and must not be appended to
                # `errors`: the capacity exists, it is simply not this
                # session's to spend yet. It is collected as a pause candidate
                # so that if EVERY provider refuses, the run waits rather than
                # failing.
                grant = await self.ration.reserve(
                    provider=slot.spec.name,
                    credential=slot.credential_id,
                    model=slot.provider.models[0],
                )
                if not grant.admitted:
                    ration_waits.append(
                        PauseCause(
                            reason=grant.decision.reason,
                            provider=slot.spec.name,
                            credential=slot.credential_id,
                            dimension=grant.decision.dimension,
                            used=grant.decision.used,
                            envelope=grant.decision.envelope,
                            resumes_at=grant.decision.resumes_at,
                        )
                    )
                    continue

                # Quota gate. Asking permission before sending is what keeps N
                # pods sharing a credential from collectively exceeding the
                # account limit -- each pod's own backoff cannot see the others.
                # Both dimensions, taken together or not at all. Spending a
                # request permit and then finding the token bucket dry would
                # drain the request allowance at the token bucket's refill rate
                # and throttle the pool on a dimension with capacity to spare.
                #
                # The token figure is the ration's own estimate, measured from
                # the usage journal rather than assumed: it moves with prompt
                # size, which moves with schema hints and retrieved skills.
                wait = await self.quota.acquire(
                    slot.quota_key,
                    tokens=self.ration.estimate_tokens(slot.spec.name),
                )
                if wait > 0:
                    quota_waits.append(wait)
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
                        # The request stands against the ration even though it
                        # failed: several providers count a 429 toward the
                        # daily allowance, so releasing it here would let a
                        # throttled run spend its day discovering that.
                        await self.ration.settle(
                            grant, tokens=0, outcome="rate_limited"
                        )
                        slot.state.record_429(exc.retry_after_s)
                        metrics.record_rate_limited(
                            provider=slot.spec.name, model=model
                        )
                        if exc.daily:
                            # A wait measured in hours is the account saying the
                            # DAY is spent, not the minute. Journalled so it
                            # outlives this process and is believed over the
                            # declared table until the instant the provider
                            # itself named (ADR-008). Halving the per-minute
                            # bucket instead, as the single branch below used
                            # to, answers a spent day by retrying it slightly
                            # more slowly -- and every retry earns another
                            # rejection charged to the day already gone.
                            until = time.time() + (
                                exc.retry_after_s or LONG_WINDOW_S
                            )
                            self.budget.observe_day_limit(
                                slot.spec.name,
                                credential=slot.credential_id,
                                model=model,
                                until=until,
                                source=f"429 retry_after={exc.retry_after_s}",
                            )
                            self.pacer.limit_corrected(
                                provider=slot.spec.name,
                                credential=slot.credential_id,
                                declared=self._declared_day(slot.spec.name),
                                until=until,
                                source="429",
                            )
                        else:
                            # A minute-window rejection: our rate estimate was
                            # too high. Tighten the bucket so the correction
                            # outlives this backoff window instead of being
                            # relearned every time.
                            await self.quota.configure(
                                slot.quota_key,
                                rate_per_min=max(1.0, slot.spec.hint_rpm * 0.5),
                                burst=1,
                                tokens_per_min=max(
                                    1.0, slot.spec.hint_tpm * 0.5
                                ),
                            )
                        errors.append(f"{slot.spec.name}: 429")
                        break  # whole provider is throttled, not just this model
                    except ProviderError as exc:
                        await self.ration.settle(grant, tokens=0, outcome="error")
                        slot.state.record_error()
                        metrics.record_llm_error(
                            provider=slot.spec.name,
                            model=model,
                            reason=type(exc).__name__,
                        )
                        errors.append(str(exc))
                        continue  # try the next model on the same provider

                    # The estimate becomes the measurement. Until this
                    # call the ration held a forecast; leaving it there would
                    # ration the rest of the session against a guess.
                    await self.ration.settle(
                        grant, tokens=resp.tokens_in + resp.tokens_out
                    )
                    slot.state.record_success()
                    # Recorded per CREDENTIAL, because that is the unit every
                    # provider meters. Two Google keys are two budgets, and a
                    # tracker that summed them would report the pair as one
                    # exhausted account.
                    self.budget.record(
                        provider=slot.spec.name,
                        credential=slot.credential_id,
                        model=resp.model,
                        requests=1,
                        tokens=resp.tokens_in + resp.tokens_out,
                    )
                    if isinstance(resp.rate_headers, RateHeaders):
                        # The cheapest limit is the one the provider
                        # volunteered on a response already paid for. Believing
                        # it here is what lets the pool stop BEFORE the 429
                        # rather than one rejection after it.
                        self.budget.observe_headers(
                            resp.rate_headers,
                            provider=slot.spec.name,
                            credential=slot.credential_id,
                            model=resp.model,
                        )
                    cost = 0.0
                    if self.account is not None:
                        cost = self.account.charge_call(
                            provider=resp.provider,
                            model=resp.model,
                            tokens_in=resp.tokens_in,
                            tokens_out=resp.tokens_out,
                            agent_id=agent_id,
                            stage=stage,
                            simulated=slot.simulated,
                            detail={
                                "latency_s": round(resp.latency_s, 4),
                                "credential": slot.credential_id,
                            },
                        )
                    metrics.record_llm_call(
                        provider=resp.provider,
                        model=resp.model,
                        latency_s=resp.latency_s,
                        cost_usd=cost,
                    )
                    return resp

            # Reaching here means nothing served this pass. There are three
            # kinds of wait available and they differ by ORDERS OF MAGNITUDE, so
            # the choice between them is the whole decision:
            #
            #   backoff   seconds   a provider is throttling us
            #   quota     seconds   the minute bucket needs to refill
            #   ration    hours     this session's share of the day is spent
            #
            # Short waits are tried first: if a bucket refills in four seconds
            # there is no reason to park until the next session. Only when
            # nothing short will help does the run park.
            waits = [s.state.wait_s() for s in ordered if not s.state.available()]
            waits.extend(quota_waits)
            waits = [w for w in waits if w != float("inf")]
            now = time.monotonic()
            short_wait = min(waits) if waits else None

            # RATION PAUSE. A spent session ration is not an outage and not a
            # backoff: the capacity exists and belongs to a later slot, so the
            # honest response is to wait for that slot rather than fail a run
            # that could finish.
            #
            # Deliberately NOT bounded by max_wait_s. That deadline exists for
            # transient unavailability measured in seconds; a session boundary
            # is measured in hours, and applying a 30-second patience to a
            # 4-hour wait would turn "come back later" into "this run cannot
            # complete" -- the outcome this module exists to prevent.
            #
            # The condition is deliberately NOT "no slot is available". A slot
            # refused by the ration is still available in the backoff sense, so
            # that test never fired and the run raised NoCapacity while sitting
            # on capacity it was merely too early to spend.
            if ration_waits and (short_wait is None or now + short_wait >= deadline):
                # The soonest returning capacity across every refused provider.
                # Waiting for the latest would idle through openings.
                soonest = min(ration_waits, key=lambda c: c.resumes_at)
                await self.pacer.park(soonest, agent_id=agent_id)
                # Woken: re-enter the loop and re-evaluate from scratch. The
                # ration may have refilled, another provider may have
                # recovered, or the operator may have unlocked capacity --
                # none of which this frame's stale decisions know about.
                deadline = time.monotonic() + self.max_wait_s
                continue

            if not waits or now >= deadline:
                detail = "; ".join(errors[-5:])
                if ration_waits:
                    # Say WHICH ration ran out. "no provider capacity" on its
                    # own sent an operator hunting for an outage that was not
                    # happening.
                    detail = (
                        detail
                        + ("; " if detail else "")
                        + "; ".join(
                            f"{c.provider}: {c.reason} ({c.dimension})"
                            for c in ration_waits[:3]
                        )
                    )
                raise NoCapacity(
                    f"no provider capacity within {self.max_wait_s}s; {detail}"
                )
            sleep_for = min(min(waits), deadline - now)
            if sleep_for <= 0:
                raise NoCapacity("no provider capacity; " + "; ".join(errors[-5:]))
            await asyncio.sleep(sleep_for)

    # -- introspection ------------------------------------------------------

    def _declared_day(self, provider: str) -> Any:
        """What the table claims, so a correction can name what it corrected.

        A "limit corrected" event that does not say what the old figure was
        leaves nobody able to go and fix the table it came from.
        """
        spec = self.budget.budgets.get(provider)
        daily = spec.limit_for("day") if spec else None
        if daily is None:
            return None
        return {"requests": daily.requests, "tokens": daily.tokens}

    def credential_map(self) -> dict[str, list[str]]:
        """provider -> the credentials actually loaded for it.

        The forecast needs this because three Google keys are three separate
        daily allowances, not one. Counting a pool of N keys as a single
        credential understates capacity by a factor of N and makes a run that
        fits comfortably project days of pauses.
        """
        out: dict[str, list[str]] = {}
        for slot in self._slots:
            out.setdefault(slot.spec.name, []).append(slot.credential_id)
        return out

    def forecast(self, estimated_calls: int) -> dict[str, Any]:
        """When this many calls will actually be spent, session by session."""
        return self.ration.forecast(
            estimated_calls, credentials=self.credential_map()
        )

    def pace_status(self) -> dict[str, Any]:
        """Whether the pool is paused, why, and when it comes back.

        Separate from `status()` because that is per-provider and a pause is a
        property of the whole pool: every agent is waiting on the same clock.
        """
        return self.pacer.status()

    def status(self) -> list[dict[str, Any]]:
        """Live view of the pool. Feeds the CLI probe and the dashboard."""
        return [
            {
                "provider": s.spec.name,
                "credential": s.credential_id,
                "tier": s.spec.tier,
                "simulated": s.simulated,
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
        # The pacer holds a heartbeat task while paused. Closing providers and
        # leaving it running strands a timer on a loop that is about to go
        # away -- harmless in production where the process exits, and a hang
        # in a test suite where the next test inherits it.
        await self.pacer.aclose()
        await asyncio.gather(*(s.provider.aclose() for s in self._slots))
