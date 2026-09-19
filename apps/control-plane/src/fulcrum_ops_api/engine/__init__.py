"""Access to the private telemetry engine.

The adapter owns a pooled HTTP client, so exactly one instance lives per
process. It is built during application startup, published here with
``set_engine_client`` and closed on shutdown; everything else asks for it with
``get_engine_client`` rather than constructing its own.
"""

from __future__ import annotations

from .client import (
    EngineBadRequest,
    EngineClient,
    EngineError,
    EngineNotFound,
    EngineServerError,
    EngineTimeout,
    EngineUnavailable,
    deadline,
)

__all__ = [
    "EngineBadRequest",
    "EngineClient",
    "EngineError",
    "EngineNotFound",
    "EngineServerError",
    "EngineTimeout",
    "EngineUnavailable",
    "deadline",
    "get_engine_client",
    "set_engine_client",
]

_client: EngineClient | None = None


def set_engine_client(client: EngineClient | None) -> None:
    """Publish (or, with ``None``, clear) the process-wide adapter."""
    global _client
    _client = client


def get_engine_client() -> EngineClient:
    """Return the adapter built at startup.

    Raises rather than lazily constructing one: a missing client means the
    startup hook did not run, and silently opening a second connection pool
    would hide that.
    """
    if _client is None:
        raise EngineUnavailable(
            "the telemetry adapter has not been initialised for this process"
        )
    return _client
