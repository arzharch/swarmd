"""FetchHarness: robots-aware, allowlisted, rate-limited HTTP fetching.

Design notes:
- Allowlist is a hard gate: only domains explicitly registered are fetchable.
  Everything else raises — no silent skips that could mask a misconfiguration.
- robots.txt is fetched and cached per host; disallowed paths return None.
- Token-bucket rate limiting per host: sustained rate with burst tolerance,
  which is politer (and more realistic) than fixed-interval sleeps.
"""

from __future__ import annotations

import asyncio
import time
from urllib import robotparser
from urllib.parse import urlparse

import httpx


class DisallowedHostError(RuntimeError):
    """Raised when the target host is not on the allowlist."""


class _TokenBucket:
    def __init__(self, rate_per_s: float, burst: int) -> None:
        self.rate = rate_per_s
        self.burst = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self.tokens) / self.rate)


class FetchHarness:
    def __init__(
        self,
        allowed_hosts: list[str],
        *,
        rate_per_s: float = 2.0,
        burst: int = 5,
        timeout_s: float = 10.0,
        respect_robots: bool = True,
    ) -> None:
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.respect_robots = respect_robots
        self._buckets: dict[str, _TokenBucket] = {
            h: _TokenBucket(rate_per_s, burst) for h in self.allowed_hosts
        }
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)

    async def fetch_text(self, url: str) -> str | None:
        """Fetch a URL as text; returns None when robots.txt disallows it."""
        host = urlparse(url).hostname or ""
        if host.lower() not in self.allowed_hosts:
            raise DisallowedHostError(f"host not allowlisted: {host}")

        if self.respect_robots and not await self._allowed_by_robots(url):
            return None

        await self._buckets[host.lower()].acquire()
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.text

    async def _allowed_by_robots(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        if host not in self._robots:
            rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                resp = await self._client.get(f"https://{host}/robots.txt")
                if resp.status_code == 200:
                    assert rp is not None
                    rp.parse(resp.text.splitlines())
                    self._robots[host] = rp
                else:
                    self._robots[host] = None  # no robots.txt: allow
            except httpx.HTTPError:
                self._robots[host] = None
        cached = self._robots[host]
        return cached is None or cached.can_fetch("*", url)

    async def aclose(self) -> None:
        await self._client.aclose()
