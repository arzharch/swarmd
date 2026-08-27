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
    """A single completion result with provenance attached."""

    text: str
    provider: str
    model: str
    latency_s: float
    tokens_in: int = 0
    tokens_out: int = 0


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
        digest = hashlib.sha256(
            f"{request.prompt}|{round(request.temperature, 2)}".encode()
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
            tokens_in=len(request.prompt.split()),
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
                return LLMResponse(
                    text=data["choices"][0]["message"]["content"],
                    provider=self.name,
                    model=model,
                    latency_s=time.monotonic() - start,
                    tokens_in=usage.get("prompt_tokens", 0),
                    tokens_out=usage.get("completion_tokens", 0),
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
