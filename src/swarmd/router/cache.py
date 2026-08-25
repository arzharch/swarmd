"""Semantic cache: embedding-similarity lookup with TTL and LRU eviction.

Design notes:

- Exact-match caches miss paraphrases ("enrich acme" vs "enrich ACME corp");
  semantic caches answer "have we seen something close enough?" via embedding
  cosine similarity above a threshold.
- Threshold is THE tuning knob. Too low (0.8) serves wrong answers — a cache hit
  that's semantically adjacent but factually different is worse than a miss.
  Too high (0.99) degenerates to exact matching with extra overhead. 0.95 is
  the standard compromise for short prompts; we make it explicit per-cache.
- Eviction: LRU by capacity, TTL by age — both cheap, both bounded.
- Embeddings come from a pluggable embedder; the default hashes character
  n-grams deterministically so tests/CI need no model download.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

EmbedFn = Callable[[str], list[float]]


def hash_embedder(text: str, dims: int = 128) -> list[float]:
    """Deterministic char-trigram hashing embedder.

    Not semantically deep, but: stable across runs, no downloads, and it gives
    real similarity behavior for overlapping text (shared trigrams -> shared
    dimensions), which is enough to exercise cache mechanics honestly.
    """
    vec = [0.0] * dims
    padded = f"  {text.lower()}  "
    for i in range(len(padded) - 2):
        gram = padded[i : i + 3]
        h = int(hashlib.md5(gram.encode()).hexdigest()[:8], 16)
        vec[h % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass(slots=True)
class _Entry:
    prompt: str
    response: Any
    embedding: list[float]
    created_ts: float
    last_used_ts: float


class SemanticCache:
    def __init__(
        self,
        *,
        threshold: float = 0.95,
        ttl_s: float = 3600.0,
        capacity: int = 1024,
        embedder: EmbedFn | None = None,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.threshold = threshold
        self.ttl_s = ttl_s
        self.capacity = capacity
        self.embed = embedder or hash_embedder
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        # Metrics — surfaced to Prometheus in the observability phase.
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def _key(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode()).hexdigest()[:16]

    async def get(self, prompt: str) -> Any | None:
        """Return cached response for a semantically-equal prompt, else None."""
        now = time.monotonic()
        emb = self.embed(prompt)

        best_key: str | None = None
        best_sim = -1.0
        for key, entry in self._entries.items():
            if now - entry.created_ts > self.ttl_s:
                continue  # expired; lazily skipped, swept on insert
            sim = cosine(emb, entry.embedding)
            if sim > best_sim:
                best_sim, best_key = sim, key

        if best_key is not None and best_sim >= self.threshold:
            entry = self._entries[best_key]
            entry.last_used_ts = now
            self._entries.move_to_end(best_key)
            self.hits += 1
            return entry.response

        self.misses += 1
        return None

    async def put(self, prompt: str, response: Any) -> None:
        now = time.monotonic()
        # Sweep expired entries first so capacity goes to live data.
        expired = [
            k
            for k, e in self._entries.items()
            if now - e.created_ts > self.ttl_s
        ]
        for k in expired:
            del self._entries[k]

        while len(self._entries) >= self.capacity:
            self._entries.popitem(last=False)  # evict least-recently-used
            self.evictions += 1

        key = self._key(prompt)
        self._entries[key] = _Entry(
            prompt=prompt,
            response=response,
            embedding=self.embed(prompt),
            created_ts=now,
            last_used_ts=now,
        )

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def __len__(self) -> int:
        return len(self._entries)


class TokenBudget:
    """Per-run/per-stage token accounting; breach aborts cleanly.

    ANATOMY: budget_tokens
      Hard ceiling on summed tokens_in+tokens_out for a scope. On breach we
      raise BudgetExceeded — callers convert that into a clean PARTIAL run with
      a report, never silent truncation mid-item.
    """

    def __init__(self, budget_tokens: int) -> None:
        self.budget = budget_tokens
        self.used = 0

    def charge(self, tokens_in: int, tokens_out: int) -> None:
        self.used += tokens_in + tokens_out
        if self.used > self.budget:
            raise BudgetExceeded(
                f"token budget {self.budget} exceeded (used {self.used})"
            )

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)


class BudgetExceeded(RuntimeError):
    pass
