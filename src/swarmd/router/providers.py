"""LLM provider interface: deterministic mock + OpenRouter with fallback chain.

Design notes:

- One Provider interface for everything (mock, OpenRouter). Callers never know or
  care which backend served a request — that's what makes offline-first tests and
  real runs interchangeable.
- MockProvider is seeded and deterministic: same prompt -> same response, forever.
  This is what makes chaos tests hash-comparable (ADR-004).
- OpenRouterProvider targets FREE models only (":free" suffix) with an ordered
  fallback chain: if the primary model errors/rate-limits/times out, the next model
  serves transparently. The caller sees one latency spike at most, never an error
  (unless every model in the chain fails).
- Router = health tracking + chain iteration. Health scores decay on errors and
  recover on success; a dead model sinks to the back of preference automatically.

API key: set OPENROUTER_API_KEY env var. Without it, OpenRouterProvider raises at
construction time — fail fast beats failing mid-run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

DEFAULT_FREE_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """A single completion request."""

    prompt: str
    system: str | None = None
    temperature: float = 0.7
    max_tokens: int = 512
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A single completion result with provenance attached.

    ANATOMY: rate_headers
      The `x-ratelimit-*` family the provider attached to THIS response, parsed
      (`budget.parse_rate_headers`). Carried on the success path, not just the
      429 path, because that is the whole value of it: a response that says
      "zero tokens left for two hours" lets the pool stop before the rejection
      instead of learning the same fact from one.

      Typed loosely to keep `providers.py` free of a `budget` import -- the
      dependency runs the other way, and a backend with no notion of budgets
      still has to be constructible.

    ANATOMY: cached_tokens / cached_tokens_reported
      How many of `tokens_in` the provider says it served from its own prompt
      cache, and whether it said anything at all. TWO fields rather than one
      nullable count because 0 is ambiguous and the ambiguity matters: "this
      provider does not report cached tokens" and "this provider reports that
      nothing was cached" are different facts, and reading the first as the
      second turns a working prefix cache into an apparent no-op (or the
      reverse). Never estimated -- the provider's usage block is the only
      honest measurement, which is the same discipline `router/cache.py`
      states for the response cache.
    """

    text: str
    provider: str
    model: str
    latency_s: float
    tokens_in: int = 0
    tokens_out: int = 0
    rate_headers: Any = None
    cached_tokens: int = 0
    cached_tokens_reported: bool = False


# The keys providers actually use for the same number. OpenAI, Groq,
# OpenRouter and Google's OpenAI-compat shim all nest it under
# `prompt_tokens_details.cached_tokens`; the Anthropic-shaped
# `cache_read_input_tokens` appears flat in the usage block on gateways that
# proxy it. Anything else is absent, and absent is reported as absent.
CACHED_TOKEN_KEYS = ("cached_tokens", "cache_read_input_tokens")


def parse_cached_tokens(usage: Any) -> tuple[int, bool]:
    """Read cached prompt tokens out of an OpenAI-style usage block.

    Returns (count, reported). DEFENSIVE ON PURPOSE: providers disagree about
    this field's name, its nesting, and whether it exists at all, and several
    send `prompt_tokens_details: null` rather than omitting it. A parser that
    assumed the happy shape would raise inside a successful call and turn a
    bookkeeping gap into a failed request.

    A negative or non-numeric value is read as "not reported" rather than
    clamped to zero, because a provider sending nonsense here has told us
    nothing, and recording nothing as a measurement is how a fabricated
    saving gets into a report.
    """
    if not isinstance(usage, dict):
        return 0, False
    details = usage.get("prompt_tokens_details")
    sources: list[dict[str, Any]] = [usage]
    if isinstance(details, dict):
        sources.insert(0, details)
    for source in sources:
        for key in CACHED_TOKEN_KEYS:
            if key not in source:
                continue
            raw = source[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            if raw < 0:
                continue
            return int(raw), True
    return 0, False


class ProviderError(RuntimeError):
    """Raised when all providers/models in a chain fail."""


class Provider(ABC):
    """Minimal chat-completion interface every backend must satisfy."""

    name: str = "base"

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class MockProvider(Provider):
    """Deterministic offline provider: response derived from prompt hash.

    Same (prompt, temperature-bucket) always yields the same text — required for
    reproducible benchmarks and chaos-test integrity hashes.

    JSON mode: when the prompt requests a JSON schema (as LLMHarness.structured
    does), the mock synthesizes a deterministic VALID payload for that schema —
    strings from hash words, ints within the schema's declared bounds. This lets
    structured-output stages run fully offline with realistic shapes.
    """

    name = "mock"

    def __init__(self, latency_s: float = 0.01) -> None:
        self.latency_s = latency_s

    async def complete(self, request: LLMRequest) -> LLMResponse:
        start = time.monotonic()
        await asyncio.sleep(self.latency_s)
        # THE SYSTEM MESSAGE IS PART OF THE REQUEST, so it is part of the
        # digest. It was not, and once the run-stable layer (task + frozen
        # criterion) moved into the system role, a mock whose output ignored
        # it would return the same text for two runs graded against different
        # criteria -- making every chaos integrity hash insensitive to the one
        # thing the run is measured against.
        digest = hashlib.sha256(
            f"{request.system or ''}|{request.prompt}|"
            f"{round(request.temperature, 2)}".encode()
        ).hexdigest()
        n = int(digest[:8], 16)
        if "Respond with ONLY a JSON object" in request.prompt:
            text = self._json_for_schema(request.prompt, n)
        else:
            # Deterministic pseudo-prose so downstream stages have realistic shapes.
            words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa"]
            text = " ".join(words[(n >> i) % len(words)] for i in range(0, 40, 4))
        return LLMResponse(
            text=text,
            provider=self.name,
            model="mock-v1",
            latency_s=time.monotonic() - start,
            # Both roles, for the same reason `SimulatedProvider` counts both:
            # the prefix reordering MOVED prompt bytes into `system`, it did
            # not delete them, and a counter that saw only `prompt` would
            # report the move as a token saving that no invoice will show.
            tokens_in=len(f"{request.system or ''} {request.prompt}".split()),
            tokens_out=10,
        )

    @staticmethod
    def _json_for_schema(prompt: str, n: int) -> str:
        """Build a deterministic valid payload for the last JSON schema in prompt."""
        import json as _json
        import re

        match = re.findall(r"\{.*\}", prompt, flags=re.DOTALL)
        if not match:
            return "{}"
        try:
            schema = _json.loads(match[-1])
        except _json.JSONDecodeError:
            return "{}"
        props = schema.get("properties", {})
        payload: dict[str, Any] = {}
        for i, (name, spec) in enumerate(props.items()):
            t = spec.get("type", "string")
            shift = (i * 7) % 24
            if t == "integer":
                lo = spec.get("minimum", 0)
                hi = spec.get("maximum", 100)
                payload[name] = lo + (n >> shift) % max(hi - lo + 1, 1)
            elif t == "number":
                payload[name] = round(((n >> shift) % 1000) / 10, 1)
            elif t == "boolean":
                payload[name] = bool((n >> shift) & 1)
            elif t == "array":
                words = ["signal-a", "signal-b", "signal-c"]
                payload[name] = [words[(n >> shift) % 3]]
            else:
                payload[name] = f"mock-{name}-{(n >> shift) % 97}"
        return _json.dumps(payload)


class _ModelHealth:
    """EWMA-based health score per model; lower is better."""

    def __init__(self) -> None:
        self.errors = 0
        self.successes = 0
        self.last_error_ts = 0.0

    def record_success(self) -> None:
        self.successes += 1

    def record_error(self) -> None:
        self.errors += 1
        self.last_error_ts = time.monotonic()

    def score(self) -> float:
        """Error ratio weighted by recency of failures."""
        total = self.errors + self.successes
        if total == 0:
            return 0.0
        recency = max(0.0, 1.0 - (time.monotonic() - self.last_error_ts) / 60.0)
        return self.errors / total * (0.5 + 0.5 * recency)


class OpenRouterProvider(Provider):
    """OpenRouter adapter restricted to free models, with ordered fallbacks.

    Tries models in health-sorted order; a model that fails is skipped for this
    request and demoted for future ones. Requires httpx (installed in Phase 2).
    """

    name = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        models: list[str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        import httpx  # deferred: keeps Phase-1 kernel dependency-free

        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(
                "OpenRouterProvider requires OPENROUTER_API_KEY (env var or arg)"
            )
        self._key = key
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout_s,
        )
        self.models = models or DEFAULT_FREE_MODELS
        self._health: dict[str, _ModelHealth] = {
            m: _ModelHealth() for m in self.models
        }

    def _ordered_models(self) -> list[str]:
        """Health-sorted chain: healthiest first, ties keep configured order."""
        return sorted(self.models, key=lambda m: self._health[m].score())

    async def complete(self, request: LLMRequest) -> LLMResponse:
        errors: list[str] = []
        for model in self._ordered_models():
            start = time.monotonic()
            try:
                resp = await self._client.post(
                    "/chat/completions",
                    json={
                        "model": model,
                        "messages": self._messages(request),
                        "temperature": request.temperature,
                        "max_tokens": request.max_tokens,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._health[model].record_success()
                usage = data.get("usage", {})
                cached, cached_reported = parse_cached_tokens(usage)
                return LLMResponse(
                    text=data["choices"][0]["message"]["content"],
                    provider=self.name,
                    model=model,
                    latency_s=time.monotonic() - start,
                    tokens_in=usage.get("prompt_tokens", 0),
                    tokens_out=usage.get("completion_tokens", 0),
                    cached_tokens=cached,
                    cached_tokens_reported=cached_reported,
                )
            except Exception as exc:  # noqa: BLE001 - chain boundary by design
                self._health[model].record_error()
                errors.append(f"{model}: {exc}")
        raise ProviderError("all models failed:\n" + "\n".join(errors))

    @staticmethod
    def _messages(request: LLMRequest) -> list[dict[str, str]]:
        msgs = []
        if request.system:
            msgs.append({"role": "system", "content": request.system})
        msgs.append({"role": "user", "content": request.prompt})
        return msgs

    async def aclose(self) -> None:
        await self._client.aclose()


class FallbackRouter(Provider):
    """Routes across providers: tries each provider's chain before moving on.

    DO NOT put MockProvider at the end of the chain. An earlier version of this
    docstring recommended exactly that as a "last-resort guarantee", which meant
    a total provider outage silently produced synthetic output that nothing
    downstream marked as synthetic -- the precise failure ADR-006 exists to
    prevent. A run with no reachable provider must fail loudly.

    Superseded in practice by ProviderPool (ADR-008), which adds per-credential
    quota, empirical rate-limit discovery, and tier ordering. This remains for
    the LeadOps example, which is frozen.
    """

    name = "router"

    def __init__(self, providers: list[Provider]) -> None:
        if not providers:
            raise ValueError("FallbackRouter needs at least one provider")
        self.providers = providers

    async def complete(self, request: LLMRequest) -> LLMResponse:
        errors: list[str] = []
        for p in self.providers:
            try:
                return await p.complete(request)
            except Exception as exc:  # noqa: BLE001 - chain boundary by design
                errors.append(f"{p.name}: {exc}")
        raise ProviderError("all providers failed:\n" + "\n".join(errors))


def make_router(mode: str = "simulated") -> Provider:
    """Factory used by the CLI and the LeadOps example.

    ANATOMY: mode
      "simulated"  -> the tainted synthetic provider. Requires
                      SWARMD_SIMULATED_PROVIDER=true, and every ledger row it
                      produces is marked simulated (ADR-012).
      "openrouter" -> free models. Requires OPENROUTER_API_KEY and RAISES
                      without one.

    Two behaviours here were ADR-006 violations and are now fixed. `mode="mock"`
    returned an UNMARKED deterministic provider on a user-facing path, so a demo
    could show synthetic output that nothing identified as synthetic. And a
    missing API key silently downgraded to that same mock "rather than crashing
    a demo run" -- which is precisely the trade ADR-006 refuses: a crash is
    visible, and quietly fabricated output is not.

    `mode="mock"` is still accepted as an alias for "simulated" so the frozen
    LeadOps example keeps working, but it now goes through the tainted path.
    """
    if mode in {"simulated", "mock"}:
        from swarmd.router.simulated import ENV_FLAG, SimulatedProvider

        try:
            return SimulatedProvider()
        except RuntimeError as exc:
            raise RuntimeError(
                f"{exc}\n\nThere is deliberately no unmarked mock on this "
                f"path: set {ENV_FLAG}=true to run against synthetic output "
                f"that is marked as such everywhere it surfaces."
            ) from exc
    if mode == "openrouter":
        # No mock fallback. A run with no reachable provider fails loudly.
        return OpenRouterProvider()
    raise ValueError(f"unknown router mode: {mode!r}")


def request_to_json(request: LLMRequest) -> str:
    """Stable serialization for cache keys (Phase 4 semantic cache will reuse)."""
    return json.dumps(
        {"p": request.prompt, "s": request.system, "t": request.temperature},
        sort_keys=True,
    )
