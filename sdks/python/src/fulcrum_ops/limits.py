"""The ingest contract's field limits, mirrored so the SDK trims instead of the
server rejecting.

A row that violates one of these comes back as a ``malformed`` per-item result,
which is a silent data loss the caller never sees — telemetry does not raise.
Clamping here turns "the trace vanished" into "the trace arrived with a
truncated tag", which is the better failure.
"""

from __future__ import annotations

from typing import Optional

# Batch ceilings, from the OpenAPI document.
MAX_TRACES_PER_BATCH = 1_000
MAX_SPANS_PER_BATCH = 1_000
MAX_SPANS_PER_TRACE = 1_000
MAX_SCORES_PER_BATCH = 1_000
MAX_EVENTS_PER_BATCH = 500

# Per-item ceilings.
MAX_NAME_LENGTH = 200
MAX_TAGS = 32
MAX_TAG_LENGTH = 64
MAX_METADATA_KEYS = 64
MAX_USAGE_KEYS = 24
MAX_SCORES_PER_ITEM = 25

MAX_EXCEPTION_TYPE_LENGTH = 200
MAX_EXCEPTION_MESSAGE_LENGTH = 4_000
MAX_TRACEBACK_LENGTH = 16_000

MAX_SCORE_NAME_LENGTH = 80
MAX_SCORE_CATEGORY_LENGTH = 80
MAX_SCORE_REASON_LENGTH = 1_000
MAX_SCORE_SOURCE_LENGTH = 32

MAX_AGENT_LENGTH = 160
MAX_ID_LENGTH = 64
MAX_TARGET_ID_LENGTH = 120
MAX_THREAD_ID_LENGTH = 120
MAX_REF_LENGTH = 64

MAX_MODEL_LENGTH = 120
MAX_PROVIDER_LENGTH = 80

# ``EventIn`` ceilings, which are tighter than they look.
MAX_GUARDRAIL_LENGTH = 160
MAX_POLICY_LENGTH = 160
#: 24 characters — "Blocked", "Masked", "Warned" fit; a sentence does not.
MAX_ACTION_TAKEN_LENGTH = 24
MAX_SEVERITY_LENGTH = 16
MAX_SENTIMENT_LENGTH = 16
MAX_EVENT_BODY_LENGTH = 4_000
MAX_EVENT_SOURCE_LENGTH = 40
MAX_SUBMITTED_BY_LENGTH = 160
MAX_SAMPLE_LENGTH = 500
#: An issue's title is a headline, not the report: the explanation goes in the body.
MAX_ISSUE_TITLE_LENGTH = 200

#: ``rating`` is an integer star count; anything outside the range is refused.
MIN_RATING = 1
MAX_RATING = 5

#: How much of ``batch_max_bytes`` a batch may fill before it flushes early.
#: The remainder is headroom for the envelope the batch is wrapped in.
BYTE_BUDGET_RATIO = 0.85


def clamp_text(value: Optional[str], maximum: int) -> Optional[str]:
    """Trim a string to a character ceiling, or drop it if nothing is left."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed if len(trimmed) <= maximum else trimmed[:maximum]


def clamp_required_text(value: str, maximum: int, fallback: str) -> str:
    """Same, for a field the contract marks required — never returns empty."""
    trimmed = clamp_text(value, maximum)
    return trimmed if trimmed else fallback
