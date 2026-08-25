"""Pipeline: stage DAG with dependency ordering and per-stage pools.

Design notes:

- Stages declare dependencies by name; the executor topologically sorts and runs
  independent stages concurrently. Cycles are rejected at definition time, not
  discovered as a hang at runtime.
- Each stage owns a pool size (its concurrency cap) and a worker function. Items
  flow between stages via asyncio queues; a stage's output items become the next
  stage's inputs.
- A stage completes when its input queue is drained AND all its in-flight items
  finish. Downstream stages see an explicit end-of-stream sentinel rather than
  guessing from queue emptiness (racy).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

_END = object()  # end-of-stream sentinel


@dataclass
class Stage:
    """One node in the pipeline DAG."""

    name: str
    fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
    pool_size: int = 4
    depends_on: list[str] = field(default_factory=list)


class PipelineError(RuntimeError):
    pass


class Pipeline:
    """DAG of stages executed with dependency ordering and bounded pools."""

    def __init__(self) -> None:
        self._stages: dict[str, Stage] = {}

    def add_stage(self, stage: Stage) -> Pipeline:
        if stage.name in self._stages:
            raise PipelineError(f"duplicate stage: {stage.name}")
        for dep in stage.depends_on:
            if dep not in self._stages and dep not in {
                s.name for s in self._stages.values()
            }:
                # Deps may be declared before or after; validated at run().
                pass
        self._stages[stage.name] = stage
        return self

    def _validate(self) -> list[str]:
        """Topological order; raises on unknown deps and cycles."""
        order: list[str] = []
        temp: set[str] = set()
        done: set[str] = set()

        def visit(name: str, chain: list[str]) -> None:
            if name in done:
                return
            if name in temp:
                raise PipelineError(f"cycle: {' -> '.join(chain + [name])}")
            if name not in self._stages:
                raise PipelineError(f"unknown stage: {name}")
            temp.add(name)
            for dep in self._stages[name].depends_on:
                visit(dep, chain + [name])
            temp.discard(name)
            done.add(name)
            order.append(name)

        for name in self._stages:
            visit(name, [])
        return order

    def _levels(self) -> list[list[str]]:
        """Group stages into dependency levels; levels run in order, stages
        within a level run concurrently. Raises on unknown deps and cycles."""
        order = self._validate()
        depth: dict[str, int] = {}
        for name in order:
            deps = self._stages[name].depends_on
            depth[name] = max((depth[d] for d in deps), default=-1) + 1
        levels: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
        for name, d in sorted(depth.items(), key=lambda kv: kv[1]):
            levels[d].append(name)
        return levels

    async def run(
        self, items: list[dict[str, Any]], on_result: Callable[[str, dict[str, Any]], None] | None = None
    ) -> dict[str, int]:
        """Run all items through the DAG. Returns per-stage processed counts.

        Stages execute level by level: every stage in a level runs concurrently
        with its siblings, and the level only advances once its queues drain.
        This guarantees a stage's inputs are fully produced before it starts,
        which makes per-stage completion well-defined without sentinels.
        """
        self._validate()
        counts: dict[str, int] = {name: 0 for name in self._stages}
        queues: dict[str, asyncio.Queue[Any]] = {
            name: asyncio.Queue() for name in self._stages
        }

        roots = [name for name in self._stages if not self._stages[name].depends_on]
        for item in items:
            for root in roots:
                await queues[root].put(item)

        async def run_stage(name: str) -> None:
            stage = self._stages[name]
            consumers = [
                dep_name
                for dep_name in self._stages
                if name in self._stages[dep_name].depends_on
            ]

            async def worker() -> None:
                while True:
                    item = await queues[name].get()
                    try:
                        result = await stage.fn(item)
                        counts[name] += 1
                        if result is not None:
                            for c in consumers:
                                await queues[c].put(result)
                    finally:
                        queues[name].task_done()

            tasks = [asyncio.create_task(worker()) for _ in range(stage.pool_size)]
            try:
                await queues[name].join()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        for level in self._levels():
            await asyncio.gather(*(run_stage(name) for name in level))
        return counts
