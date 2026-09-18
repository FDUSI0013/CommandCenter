"""What the run screens remember between requests, and when they forget it.

The run views -- the table, its KPI row, the live stream, the exports -- are all
answers to one question asked of the telemetry store: *which runs landed in this
window, in these projects?* Asked naively it is asked again for every project,
by every request, from every open tab, every few seconds; in production that was
some twenty store queries per API request and a store pinned at four cores by
people merely looking at a screen.

Three small memories remove nearly all of it:

``project_activity``
    When each project was last written to. One cheap call answers it for every
    project at once, and it lets a scan skip the projects that *cannot* have a
    run in the window being asked about -- most of them, most of the time.

``project_scans``
    The rows one project returned for one window, for a few seconds. The table,
    the KPI row and every stream subscriber want the same rows at the same
    moment; they now share one read.

``summaries``
    The finished KPI row. It folds thousands of rows and changes slowly.

All three are per process and short-lived, so the cost of being wrong is a run
that appears a few seconds late -- and a write through *this* process forgets
the affected project at once, so the common case is not late at all.

Every lifetime is a setting, read on each call. Zero switches a memory off,
which is what the test suite does: a test writes and reads back in the same
breath and must see what it wrote.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..core.config import settings
from ..core.ttlcache import SingleFlightCache

#: project id -> when it was last written to (``None``: never). The whole value
#: is ``None`` when the store could not say, which readers treat as "scan it".
ProjectActivity = dict[str, dt.datetime | None] | None

project_activity: SingleFlightCache[ProjectActivity] = SingleFlightCache(
    ttl=lambda: settings.runs_activity_cache_seconds, max_entries=4
)

#: key: (project_id, window start, window end, row limit), the window snapped to
#: a grid so that requests a moment apart share a key.
project_scans: SingleFlightCache[tuple[list[dict[str, Any]], bool]] = SingleFlightCache(
    ttl=lambda: settings.runs_scan_cache_seconds, max_entries=256
)

summaries: SingleFlightCache[Any] = SingleFlightCache(
    ttl=lambda: settings.runs_summary_cache_seconds, max_entries=64
)


def project_written(project_id: str | None) -> None:
    """Forget what is remembered about a project this process just wrote to."""
    project_activity.invalidate()
    summaries.invalidate()
    if project_id:
        project_scans.invalidate(
            lambda key: isinstance(key, tuple) and bool(key) and key[0] == project_id
        )
    else:
        project_scans.invalidate()


def reset() -> None:
    """Forget everything. For tests and for operators' peace of mind."""
    project_activity.invalidate()
    project_scans.invalidate()
    summaries.invalidate()
