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
from swarmd.router.budget import BudgetTracker
from swarmd.router.providers import (
    LLMRequest,
    LLMResponse,
    Provider,
    ProviderError,
)
from swarmd.router.quota import QuotaBackend, build_quota

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
        models=("openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
        hint_rpm=30,
        hint_tpm=12_000,
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

        if resp.status_code == 429:
            raise RateLimited(self.name, _retry_after(resp))
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

                # Quota gate. Asking permission before sending is what keeps N
                # pods sharing a credential from collectively exceeding the
                # account limit -- each pod's own backoff cannot see the others.
                wait = await self.quota.acquire(slot.quota_key)
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
                        slot.state.record_429(exc.retry_after_s)
                        metrics.record_rate_limited(
                            provider=slot.spec.name, model=model
                        )
                        # The provider just told us our estimate was too high.
                        # Tighten the bucket so the correction outlives this
                        # backoff window instead of being relearned every time.
                        await self.quota.configure(
                            slot.quota_key,
                            rate_per_min=max(1.0, slot.spec.hint_rpm * 0.5),
                            burst=1,
                        )
                        errors.append(f"{slot.spec.name}: 429")
                        break  # whole provider is throttled, not just this model
                    except ProviderError as exc:
                        slot.state.record_error()
                        metrics.record_llm_error(
                            provider=slot.spec.name,
                            model=model,
                            reason=type(exc).__name__,
                        )
                        errors.append(str(exc))
                        continue  # try the next model on the same provider

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

            # Nothing usable right now. Wait for the soonest opening -- from
            # either a backoff expiring or a quota bucket refilling -- if that
            # fits inside the deadline.
            waits = [s.state.wait_s() for s in ordered if not s.state.available()]
            waits.extend(quota_waits)
            waits = [w for w in waits if w != float("inf")]
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
        await asyncio.gather(*(s.provider.aclose() for s in self._slots))
