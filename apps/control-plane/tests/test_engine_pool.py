"""The engine connection pool survives cancelled callers, and heals if it fills.

Production lost a worker at a time to this: the live-runs stream is cancelled by
an anyio cancel scope when the browser drops it, mid-scan, with a dozen engine
calls in flight. Under that particular cancellation the pooled connections
beneath those calls stay marked busy for ever. A hundred of them and the pool is
full; every later call in the process waits out the pool timeout and answers
503 until the worker is restarted.

These tests drive a real ``EngineClient`` over real sockets -- the in-memory
engine double never exercises the connection pool, which is how this went
unseen -- against a deliberately slow upstream.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import anyio
import pytest
import uvicorn
from fastapi import FastAPI

from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.engine.client import EngineClient, EngineUnavailable

SLOW_SECONDS = 0.4


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def upstream() -> AsyncIterator[str]:
    """A stand-in engine that takes its time, on a real socket."""
    app = FastAPI()

    @app.get("/slow")
    async def slow() -> dict[str, bool]:
        await asyncio.sleep(SLOW_SECONDS)
        return {"ok": True}

    @app.get("/is-alive/ping")
    async def ping() -> dict[str, bool]:
        await asyncio.sleep(5.0)
        return {"healthy": True}

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving


async def test_a_scan_cancelled_by_a_cancel_scope_releases_every_connection(upstream: str) -> None:
    client = EngineClient(base_url=upstream, retries=0)
    try:
        for _ in range(3):
            # Exactly what a dropped live-runs stream does: an anyio cancel
            # scope fires while a gathered scan is waiting on the engine.
            with anyio.move_on_after(SLOW_SECONDS / 3):
                await asyncio.gather(*(client._request("GET", "/slow") for _ in range(10)))

        await asyncio.sleep(SLOW_SECONDS * 3)  # the abandoned exchanges finish on their own
        stats = client.pool_stats()
        assert stats["busy"] == 0, f"connections leaked by the cancelled scans: {stats}"
        assert stats["orphaned_exchanges"] == 0

        # And the pool is still usable afterwards.
        assert await client._request("GET", "/slow") == {"ok": True}
    finally:
        await client.aclose()


async def test_a_full_pool_fails_fast_and_is_rebuilt(
    upstream: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "engine_pool_timeout_seconds", 0.2)
    client = EngineClient(base_url=upstream, retries=0, max_connections=2)
    try:
        hogs = [asyncio.ensure_future(client._request("GET", "/slow")) for _ in range(2)]
        await asyncio.sleep(0.05)  # both pooled connections are now taken

        started = asyncio.get_running_loop().time()
        with pytest.raises(EngineUnavailable):
            await client._request("GET", "/slow")
        waited = asyncio.get_running_loop().time() - started
        assert waited < 1.0, f"a full pool must fail fast, not hang for {waited:.1f}s"

        assert client.pool_stats()["recycles"] == 1
        # The very next call gets the fresh pool, while the old one drains.
        assert await client._request("GET", "/slow") == {"ok": True}
        assert [await hog for hog in hogs] == [{"ok": True}, {"ok": True}]
    finally:
        await client.aclose()


async def test_the_health_probe_is_bounded(upstream: str) -> None:
    client = EngineClient(base_url=upstream, retries=0)
    try:
        started = asyncio.get_running_loop().time()
        assert await client.health(timeout=0.3) is False
        assert asyncio.get_running_loop().time() - started < 1.5
    finally:
        await client.aclose()
