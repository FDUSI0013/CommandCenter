"""Per-principal request rate limiting.

The settings for this have existed since the first release
(``rate_limit_enabled``, ``rate_limit_ingest_per_minute``,
``rate_limit_read_per_minute``) but nothing ever read them; this module is the
limiter they describe. It is a fixed one-minute window kept in process memory,
keyed by the credential — API key id or user id — so one runaway agent cannot
starve its neighbours, and an unauthenticated flood is already refused earlier
by authentication itself.

In-process means per worker: with N uvicorn workers the effective ceiling is up
to N x the configured figure. That is the documented trade of the redis-less
deployment (``redis_url`` unset); the numbers are set with headroom, and the
point of the limit is to stop a tight retry loop, not to meter revenue - quota
does that, at ingest, transactionally.
"""

from __future__ import annotations

import time

from .config import settings
from .errors import RateLimited

#: The two buckets the settings define. Ingest is the machine path and gets the
#: tall limit; everything else is a person or a report.
INGEST_PATH_PREFIXES = ("/api/v1/ingest", "/v1/traces")

_windows: dict[str, tuple[int, int]] = {}

#: Entries whose window has passed are pruned wholesale whenever the table
#: grows past this, which keeps an id-churning caller from growing it forever.
_PRUNE_ABOVE = 10_000


def check(principal_key: str, path: str) -> None:
    """Count one request; raise :class:`RateLimited` when the window is full."""
    if not settings.rate_limit_enabled:
        return

    ingest = path.startswith(INGEST_PATH_PREFIXES)
    limit = (
        settings.rate_limit_ingest_per_minute
        if ingest
        else settings.rate_limit_read_per_minute
    )
    if limit <= 0:
        return

    now = int(time.time())
    minute = now - (now % 60)
    key = f"{'ingest' if ingest else 'read'}:{principal_key}"

    window_start, count = _windows.get(key, (minute, 0))
    if window_start != minute:
        window_start, count = minute, 0

    if count >= limit:
        raise RateLimited(
            retry_after_seconds=max(1, window_start + 60 - now),
            details={"limit_per_minute": limit, "bucket": key.split(":", 1)[0]},
        )

    if len(_windows) > _PRUNE_ABOVE:
        for stale_key, (start, _) in list(_windows.items()):
            if start != minute:
                _windows.pop(stale_key, None)

    _windows[key] = (window_start, count + 1)


def reset() -> None:
    """Forget every window. Tests only."""
    _windows.clear()
