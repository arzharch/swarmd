"""Harness base: toolset + system prompt + loop policy.

A Harness bundles everything an agent needs to do one kind of work:
- a system prompt (its role/persona),
- tools it may call (as async callables),
- loop policy knobs (max iterations, retry behavior).

Business logic lives in harnesses; the kernel stays pure (ADR-002).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

Tool = Callable[..., Awaitable[Any]]


@dataclass
class Harness:
    """Reusable agent capability bundle."""

    name: str
    system_prompt: str = ""
    tools: dict[str, Tool] = field(default_factory=dict)
    max_iterations: int = 8  # loop policy: bound any agentic loop

    def register_tool(self, name: str, fn: Tool) -> None:
        if name in self.tools:
            raise ValueError(f"tool already registered: {name}")
        self.tools[name] = fn

    async def call(self, tool_name: str, **kwargs: Any) -> Any:
        if tool_name not in self.tools:
            raise KeyError(f"unknown tool {tool_name!r} on harness {self.name!r}")
        return await self.tools[tool_name](**kwargs)
