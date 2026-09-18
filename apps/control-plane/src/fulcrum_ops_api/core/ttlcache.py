"""A small per-process cache for expensive reads that many callers repeat.

Two properties matter more than the caching itself.

**Single flight.** The console polls. Ten people watching the same screen ask
the same question within the same second, and without coordination that is ten
identical scans of the telemetry store where one would do. The first caller for
a key computes; everyone who arrives while it is computing waits on that one
computation and receives the same answer.

**Cancellation does not propagate sideways.** A caller that goes away -- a
closed tab, a dropped stream -- must not cancel a computation other callers are
waiting on, and must not leave a half-finished one behind. The computation runs
as its own task and every waiter waits on a shield.

Failures are never cached: the next caller simply tries again.

This is per process by design. With four workers the worst case is four
computations per key per lifetime instead of one, which is a rounding error
against the unbounded fan-out it replaces, and it needs no shared store to be
correct.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Generic, TypeVar

V = TypeVar("V")


class SingleFlightCache(Generic[V]):
    """Time-bounded, size-bounded, single-flight cache of awaitable results."""

    def __init__(self, *, ttl: Callable[[], float], max_entries: int = 256) -> None:
        #: Read on every call rather than captured, so a setting changed at
        #: runtime -- or zeroed by a test -- takes effect immediately.
        self._ttl = ttl
        self._max_entries = max_entries
        self._entries: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future[V]] = {}

    async def get(self, key: Hashable, compute: Callable[[], Awaitable[V]]) -> V:
        ttl = self._ttl()
        if ttl <= 0:
            return await compute()  # caching switched off: behave as if absent

        hit = self._entries.get(key)
        if hit is not None:
            expires_at, value = hit
            if time.monotonic() < expires_at:
                self._entries.move_to_end(key)
                return value
            del self._entries[key]

        running = self._inflight.get(key)
        if running is None:
            running = asyncio.ensure_future(compute())
            self._inflight[key] = running
            running.add_done_callback(lambda done, key=key, ttl=ttl: self._settle(key, ttl, done))
        return await asyncio.shield(running)

    def _settle(self, key: Hashable, ttl: float, done: asyncio.Future[V]) -> None:
        if self._inflight.get(key) is done:
            del self._inflight[key]
        if done.cancelled() or done.exception() is not None:
            return  # retrieved, so it is never reported as unobserved; not cached
        self._entries[key] = (time.monotonic() + ttl, done.result())
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, matches: Callable[[Hashable], bool] | None = None) -> None:
        """Forget everything, or every key ``matches`` selects.

        A computation already in flight is left to finish -- its waiters are
        owed an answer -- but it is detached, so its result is not stored and
        the next caller computes afresh.
        """
        if matches is None:
            self._entries.clear()
            self._inflight.clear()
            return
        for key in [key for key in self._entries if matches(key)]:
            del self._entries[key]
        for key in [key for key in self._inflight if matches(key)]:
            del self._inflight[key]

    def __len__(self) -> int:
        return len(self._entries)
