"""Chaos harness: deterministic kill/latency/outage injection.

Design notes:

- SEEDED randomness is the whole point: chaos must be reproducible. A chaos run
  with seed=42 must kill exactly what a previous seed=42 run killed, so tests can
  assert "chaos output == clean output" and CI never flakes on wall-clock luck.
- Kill injection wraps Runtime.kill_agent: each scheduling tick, every live agent
  rolls against kill_rate. At rate 0 nothing dies; at 1.0 everything dies
  immediately (and recovery still has to prove itself).
- Latency injection and provider outage simulation are stubs wired into the same
  seeded RNG — they'll be exercised by pipeline/router phases without changing
  this module's contract.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any


class ChaosHook:
    """Probabilistic fault injector driven by a seeded RNG."""

    def __init__(
        self,
        seed: int = 42,
        *,
        kill_rate: float = 0.0,
        latency_rate: float = 0.0,
        latency_s: float = 0.0,
    ) -> None:
        if not 0.0 <= kill_rate <= 1.0:
            raise ValueError("kill_rate must be in [0, 1]")
        self._rng = random.Random(seed)
        self.seed = seed
        self.kill_rate = kill_rate
        self.latency_rate = latency_rate
        self.latency_s = latency_s
        self.kills_attempted = 0

    def should_kill(self) -> bool:
        """Roll once per call; caller decides what one roll means."""
        return self._rng.random() < self.kill_rate

    async def maybe_delay(self) -> None:
        """Inject latency with probability latency_rate."""
        if self.latency_rate > 0 and self._rng.random() < self.latency_rate:
            await asyncio.sleep(self.latency_s)

    def maybe_latency(self) -> bool:
        """Non-async check for whether latency WOULD be injected (for tests)."""
        return self.latency_rate > 0 and self._rng.random() < self.latency_rate


class ChaosRunner:
    """Attaches a ChaosHook to a Runtime and runs a kill loop alongside it.

    tick_s should be slower than one step's duration: kills land mid-task (between
    checkpoints) often enough to exercise recovery, but agents still make steady
    progress. A tick faster than step latency starves the pool — every agent dies
    before finishing anything and throughput collapses.
    """

    def __init__(
        self,
        runtime: Any,  # Runtime (avoid circular import at type level)
        hook: ChaosHook,
        tick_s: float = 0.5,
    ) -> None:
        self.runtime = runtime
        self.hook = hook
        self.tick_s = tick_s
        self._task: asyncio.Task[None] | None = None
        self.kills_done = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._kill_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _kill_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            for agent_id in self.runtime.live_agent_ids():
                self.hook.kills_attempted += 1
                if self.hook.should_kill() and self.runtime.kill_agent(agent_id):
                    self.kills_done += 1
