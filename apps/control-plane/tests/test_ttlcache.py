"""The single-flight cache behind the run, metrics and agent screens.

It sits between every operator's browser and the telemetry store, so what it
promises has to hold exactly: one computation per question however many people
ask, no waiter able to cancel another's answer, nothing cached that failed --
and nothing cached that a write has since made stale.
"""

from __future__ import annotations

import asyncio

import pytest

from fulcrum_ops_api.core.ttlcache import SingleFlightCache


def cache(ttl: float = 30.0, max_entries: int = 256) -> SingleFlightCache[int]:
    return SingleFlightCache(ttl=lambda: ttl, max_entries=max_entries)


async def test_ten_callers_asking_at_once_share_one_computation() -> None:
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return 42

    subject = cache()
    answers = await asyncio.gather(*(subject.get("kpis", compute) for _ in range(10)))

    assert answers == [42] * 10
    assert calls == 1
    assert await subject.get("kpis", compute) == 42 and calls == 1, "then served from memory"


async def test_a_waiter_that_goes_away_does_not_cancel_the_others_answer() -> None:
    started = asyncio.Event()

    async def compute() -> int:
        started.set()
        await asyncio.sleep(0.1)
        return 7

    subject = cache()
    leaver = asyncio.ensure_future(subject.get("k", compute))
    stayer = asyncio.ensure_future(subject.get("k", compute))
    await started.wait()
    leaver.cancel()

    assert await stayer == 7
    with pytest.raises(asyncio.CancelledError):
        await leaver


async def test_a_failure_is_raised_to_every_waiter_and_never_cached() -> None:
    attempts = 0

    async def compute() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("store is down")
        return 5

    subject = cache()
    with pytest.raises(RuntimeError):
        await subject.get("k", compute)
    assert await subject.get("k", compute) == 5, "the next caller simply tries again"


async def test_a_read_that_was_in_flight_when_a_write_invalidated_it_is_not_cached() -> None:
    """The order that matters: read starts, write lands, read finishes.

    The read may have seen the world before the write. Its waiters are owed the
    answer they asked for, but caching it would serve the pre-write world to
    everyone who asks *after* the write -- which is the one thing invalidating
    exists to prevent.
    """
    has_read, release = asyncio.Event(), asyncio.Event()
    world = {"runs": 1}

    async def compute() -> int:
        seen = world["runs"]
        has_read.set()
        await release.wait()
        return seen

    subject = cache()
    reading = asyncio.ensure_future(subject.get("project-1", compute))
    await has_read.wait()  # the read has started and seen runs == 1

    world["runs"] = 2  # a write lands ...
    subject.invalidate(lambda key: key == "project-1")  # ... and says so
    release.set()

    assert await reading == 1, "the waiter still gets the answer to what it asked"
    assert len(subject) == 0, "but the stale answer must not have been stored"
    assert await subject.get("project-1", compute) == 2


async def test_zero_lifetime_switches_it_off_entirely() -> None:
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    subject = cache(ttl=0.0)
    assert [await subject.get("k", compute) for _ in range(3)] == [1, 2, 3]
    assert len(subject) == 0


async def test_it_is_bounded_and_forgets_the_least_recently_used() -> None:
    async def value(n: int) -> int:
        return n

    subject = cache(max_entries=2)
    await subject.get("a", lambda: value(1))
    await subject.get("b", lambda: value(2))
    await subject.get("a", lambda: value(1))  # touch "a": "b" is now the oldest
    await subject.get("c", lambda: value(3))

    assert len(subject) == 2
    recomputed = False

    async def again() -> int:
        nonlocal recomputed
        recomputed = True
        return 2

    await subject.get("b", again)
    assert recomputed, '"b" was the one evicted'
