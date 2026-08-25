"""LLMHarness: structured LLM calls through the provider router.

Wraps the router with stage-appropriate defaults (temperature, max_tokens) and
JSON-mode helpers for structured outputs. Verifier stages pin low temperature;
draft stages run warmer — the harness makes that policy explicit per instance.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel

from swarmd.router.providers import LLMRequest, Provider

T = TypeVar("T", bound=BaseModel)


class LLMHarness:
    """LLM access with stage-level policy baked in."""

    def __init__(
        self,
        provider: Provider,
        *,
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> None:
        self.provider = provider
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def text(self, prompt: str, **overrides: Any) -> str:
        req = LLMRequest(
            prompt=prompt,
            system=self.system_prompt or None,
            temperature=overrides.get("temperature", self.temperature),
            max_tokens=overrides.get("max_tokens", self.max_tokens),
        )
        resp = await self.provider.complete(req)
        return resp.text

    async def structured(self, prompt: str, model: type[T], **overrides: Any) -> T:
        """Ask for JSON conforming to a pydantic model; parse and validate.

        One repair round: if the first response isn't valid JSON for the schema,
        re-ask with the validation error appended — models usually fix it.
        """
        schema_hint = json.dumps(model.model_json_schema(), indent=None)
        full = (
            f"{prompt}\n\nRespond with ONLY a JSON object matching this schema:\n"
            f"{schema_hint}"
        )
        last_error: Exception | None = None
        for attempt in range(2):
            raw = await self.text(full, **overrides)
            try:
                return model.model_validate_json(_extract_json(raw))
            except Exception as exc:  # noqa: BLE001 - repair loop boundary
                last_error = exc
                full = (
                    f"{prompt}\n\nYour previous reply was invalid: {exc}\n"
                    f"Respond with ONLY a corrected JSON object matching:\n"
                    f"{schema_hint}"
                )
        raise ValueError(f"structured output failed after retry: {last_error}")


def _extract_json(raw: str) -> str:
    """Pull the first JSON object out of a possibly chatty response."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in response: {raw[:120]!r}")
    return raw[start : end + 1]
