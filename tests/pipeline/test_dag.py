"""Tests for the stage DAG executor."""

import asyncio

import pytest

from swarmd.pipeline.dag import Pipeline, PipelineError, Stage


async def test_linear_pipeline_processes_all_items() -> None:
    p = Pipeline()
    p.add_stage(Stage("a", lambda item: _bump(item, "a")))
    p.add_stage(
        Stage("b", lambda item: _bump(item, "b"), depends_on=["a"])
    )

    counts = await p.run([{"i": i} for i in range(5)])
    assert counts == {"a": 5, "b": 5}


async def test_independent_stages_run_concurrently() -> None:
    order: list[str] = []

    async def slow(tag: str):
        async def fn(item: dict) -> dict:
            await asyncio.sleep(0.05)
            order.append((tag, item["i"]))
            return {**item, tag: True}

        return fn

    p = Pipeline()
    p.add_stage(Stage("x", await slow("x")))
    p.add_stage(Stage("y", await slow("y")))

    t0 = asyncio.get_event_loop().time()
    await p.run([{"i": 0}])
    elapsed = asyncio.get_event_loop().time() - t0

    # Serial would be ~0.10s; concurrent should be ~0.05s.
    assert elapsed < 0.09
    assert len(order) == 2


async def test_dependency_ordering_respected() -> None:
    events: list[str] = []

    async def make(tag: str):
        async def fn(item: dict) -> dict:
            events.append(tag)
            return item

        return fn

    p = Pipeline()
    p.add_stage(Stage("last", await make("last"), depends_on=["mid"]))
    p.add_stage(Stage("mid", await make("mid"), depends_on=["first"]))
    p.add_stage(Stage("first", await make("first")))

    await p.run([{"v": 1}])
    assert events.index("first") < events.index("mid") < events.index("last")


def test_cycle_detection() -> None:
    p = Pipeline()

    async def fn(item: dict) -> dict:
        return item

    p.add_stage(Stage("a", fn, depends_on=["b"]))
    p.add_stage(Stage("b", fn, depends_on=["a"]))

    with pytest.raises(PipelineError, match="cycle"):
        asyncio.run(p.run([]))


def test_unknown_dependency_rejected() -> None:
    p = Pipeline()

    async def fn(item: dict) -> dict:
        return item

    p.add_stage(Stage("a", fn, depends_on=["ghost"]))

    with pytest.raises(PipelineError, match="unknown stage"):
        asyncio.run(p.run([]))


def test_duplicate_stage_rejected() -> None:
    p = Pipeline()

    async def fn(item: dict) -> dict:
        return item

    p.add_stage(Stage("a", fn))
    with pytest.raises(PipelineError, match="duplicate"):
        p.add_stage(Stage("a", fn))


async def test_pool_size_bounds_concurrency() -> None:
    active = [0]
    peak = [0]

    async def fn(item: dict) -> dict:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.02)
        active[0] -= 1
        return item

    p = Pipeline()
    p.add_stage(Stage("capped", fn, pool_size=2))

    await p.run([{"i": i} for i in range(10)])
    assert peak[0] <= 2


async def test_stage_can_drop_items_returning_none() -> None:
    async def filter_even(item: dict) -> dict | None:
        return item if item["i"] % 2 == 0 else None

    async def passthrough(item: dict) -> dict:
        return item

    p = Pipeline()
    p.add_stage(Stage("filter", filter_even))
    p.add_stage(Stage("downstream", passthrough, depends_on=["filter"]))

    counts = await p.run([{"i": i} for i in range(10)])
    assert counts["filter"] == 10
    assert counts["downstream"] == 5  # only evens flowed through


# ---- helpers -------------------------------------------------------------


async def _bump(item: dict, tag: str) -> dict:
    await asyncio.sleep(0)
    return {**item, tag: True}
