"""Client-minted identifiers.

The control plane will mint an id for any item that arrives without one, but the
SDK always supplies its own, for two reasons: a retry after a network timeout
carries the same id and is therefore idempotent rather than a duplicate row, and
a caller can hold the trace id — to attach a score to it later — before the
trace has been reported at all.

The format is a time-ordered 128-bit value rendered as a UUID, which sorts by
creation time in any index the server puts it in.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Optional

__all__ = ["new_id", "is_valid_id", "adoptable_id"]

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def new_id() -> str:
    """Mint a time-ordered id, unique across threads in this process."""
    global _last_ms, _counter

    now_ms = int(time.time() * 1000)
    with _lock:
        if now_ms == _last_ms:
            _counter = (_counter + 1) & 0xFFFF
        else:
            _last_ms = now_ms
            _counter = 0
        counter = _counter

    # 48 bits of millisecond timestamp, 16 bits of intra-millisecond counter,
    # 64 bits of randomness — the UUIDv7 layout, without the dependency.
    timestamp = now_ms & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(8), "big")
    value = (timestamp << 80) | (counter << 64) | rand

    # Stamp the version and variant nibbles so the value is a well-formed UUID
    # and every store that validates the shape accepts it.
    value &= ~(0xF << 76)
    value |= 0x7 << 76
    value &= ~(0x3 << 62)
    value |= 0x2 << 62
    return str(uuid.UUID(int=value))


def is_valid_id(value: object) -> bool:
    """Whether a value is usable as an ingest id: a non-empty string within the length cap."""
    return isinstance(value, str) and 0 < len(value.strip()) <= 64


def adoptable_id(value: object) -> Optional[str]:
    """A caller-supplied trace id in the form the telemetry store takes, else ``None``.

    "Is a UUID" is not the test. The store addresses a run by a *version 7*
    UUID and nothing else, and its refusal is not confined to the run that
    earned it: one ``uuid.uuid4()`` on a trace costs every trace sent in the
    same request. The control plane's own check stops at the UUID shape, so
    this is the last place the difference still belongs to one run. The
    canonical spelling is returned because that is the one the run is stored
    and linked under.
    """
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None
    return str(parsed) if parsed.version == 7 else None
