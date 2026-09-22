"""Guardrails business logic.

Guardrail definitions live here; enforcement runs in the telemetry engine's
inline content checker. Everything the console can do to a guardrail — create,
tune, enable, disable, test — goes through this module, and so does the runtime
path: :func:`evaluate_ingest_batch` is the hook the ingest pipeline calls on
every trace/span batch (ingest applies the verdicts and writes the evidence
rows), while :func:`evaluate_content` serves single-content callers and writes
its own ``GuardrailEvent`` rows.

The numbers the screen shows are counted, not remembered. Triggers, blocks and
masks come from ``GROUP BY`` over the event table for the requested window, so
a guardrail that stopped firing a fortnight ago shows a falling count without
anyone resetting a counter. The cached rollup columns on ``GuardrailConfig`` are
maintained alongside for consumers outside this screen.

Two things are deliberately never done here: a verdict is never simulated (a
test with no reachable checker fails loudly rather than reporting "Clear"), and
a test never writes an event, because a rehearsal must not move the 30-day
counters an operator is judged on.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from fastapi import Request
from sqlalchemy import Select, case, event, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate
from ..api.deps import Principal
from ..core.config import settings
from ..core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from ..engine import (
    EngineBadRequest,
    EngineError,
    EngineNotFound,
    EngineServerError,
    EngineTimeout,
    deadline,
    get_engine_client,
)
from ..models.identity import Role
from ..models.quality import (
    GuardrailAction,
    GuardrailConfig,
    GuardrailEvent,
    GuardrailScope,
    GuardrailStatus,
    GuardrailType,
)
from ..models.registry import Agent
from ..schemas.guardrails import (
    EVENT_SAMPLE_LIMIT,
    SAMPLE_WITHHELD,
    ContentEvaluation,
    GuardrailCreate,
    GuardrailEventRead,
    GuardrailHit,
    GuardrailRead,
    GuardrailsSummary,
    GuardrailTestResult,
    GuardrailThresholdUpdate,
    GuardrailUpdate,
    MatchedSpan,
    config_problem,
    coverage_label,
    normalise_config,
    validation_name,
)
from . import audit
from .evaluations import owner_names, translate_engine_error

if TYPE_CHECKING:
    from ..schemas.ingest import GuardrailCandidate, GuardrailVerdict

log = logging.getLogger(__name__)

SOURCE_SCREEN: Final[str] = "Guardrails"
ENTITY_TYPE: Final[str] = "guardrail"

#: Window every counter on this screen is measured over.
WINDOW_DAYS: Final[int] = 30

#: Ceiling on the per-entity scan behind the PII card. Beyond it the card falls
#: back to the cached per-guardrail rollups and says so.
PII_SCAN_LIMIT: Final[int] = 20000

EXPORT_LIMIT: Final[int] = 5000

#: Weight of the newest measurement in the guardrail's added-latency average.
#: An exponential average keeps one slow check from rewriting the tile while
#: still tracking a checker that has genuinely got slower.
LATENCY_SMOOTHING: Final[float] = 0.2

#: Ordering used when more than one guardrail fires on the same content: the
#: strongest action wins, because that is the one actually applied.
ACTION_SEVERITY: Final[dict[str, int]] = {
    GuardrailAction.LOG.value: 0,
    GuardrailAction.WARN.value: 1,
    GuardrailAction.MASK.value: 2,
    GuardrailAction.BLOCK.value: 3,
}

#: Statuses whose guardrails run on live content. Tuning runs in shadow: the
#: check is evaluated and recorded, but nothing is enforced.
RUNTIME_STATUSES: Final[tuple[str, ...]] = (
    GuardrailStatus.ACTIVE.value,
    GuardrailStatus.TUNING.value,
)

SORTABLE: Final[dict[str, Any]] = {
    "name": GuardrailConfig.name,
    "guardrail_type": GuardrailConfig.guardrail_type,
    "status": GuardrailConfig.status,
    "action": GuardrailConfig.action,
    "threshold": GuardrailConfig.threshold,
    "scope": GuardrailConfig.scope,
    "triggers_30d": GuardrailConfig.triggers_30d,
    "blocked_30d": GuardrailConfig.blocked_30d,
    "masked_30d": GuardrailConfig.masked_30d,
    "effectiveness": GuardrailConfig.effectiveness,
    "added_latency_ms": GuardrailConfig.added_latency_ms,
    "last_triggered_at": GuardrailConfig.last_triggered_at,
    "created_at": GuardrailConfig.created_at,
    "updated_at": GuardrailConfig.updated_at,
}

EVENT_SORTABLE: Final[dict[str, Any]] = {
    "occurred_at": GuardrailEvent.occurred_at,
    "action_taken": GuardrailEvent.action_taken,
    "score": GuardrailEvent.score,
    "agent_id": GuardrailEvent.agent_id,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _window_start(window_days: int = WINDOW_DAYS) -> dt.datetime:
    return _now() - dt.timedelta(days=window_days)


# ---------------------------------------------------------------------------
# Reading the checker's answer
# ---------------------------------------------------------------------------


def _check_rows(payload: Any) -> list[dict[str, Any]]:
    """Flatten the checker's reply into one row per validation."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("validations", "results", "checks", "guardrails", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return [payload]


def _row_name(row: dict[str, Any]) -> str:
    for key in ("name", "validation", "type", "guardrail", "check"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _row_score(row: dict[str, Any]) -> float | None:
    for key in ("score", "confidence", "probability", "value"):
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return round(float(value), 4)
    return None


def _row_triggered(row: dict[str, Any], score: float | None, threshold: float) -> bool:
    """Did this validation fire?

    The checker states the verdict when it can; only when it reports nothing but
    a score is the threshold applied here, which is the same rule the engine
    would apply itself.
    """
    for key in ("triggered", "detected", "failed", "flagged", "blocked"):
        value = row.get(key)
        if isinstance(value, bool):
            return value
    for key in ("passed", "valid", "validation_passed"):
        value = row.get(key)
        if isinstance(value, bool):
            return not value
    if score is not None:
        return score >= threshold
    return False


def _row_matches(row: dict[str, Any]) -> list[MatchedSpan]:
    """Everything the checker pointed at inside the text."""
    raw: list[Any] = []
    for key in ("matches", "entities", "spans", "detections", "findings"):
        value = row.get(key)
        if isinstance(value, list):
            raw = value
            break
    if not raw:
        # The scanner answers with validation_details.detected_entities: a map
        # of entity label -> [{start, end, score, text}] rather than a flat
        # list; flatten it, carrying the label onto each hit.
        details = row.get("validation_details")
        if isinstance(details, dict):
            detected = details.get("detected_entities")
            if isinstance(detected, dict):
                for label, hits in detected.items():
                    for hit in hits if isinstance(hits, list) else []:
                        entry = dict(hit) if isinstance(hit, dict) else {}
                        entry.setdefault("label", str(label))
                        raw.append(entry)

    spans: list[MatchedSpan] = []
    for entry in raw:
        if isinstance(entry, str):
            spans.append(MatchedSpan(label=entry))
            continue
        if not isinstance(entry, dict):
            continue
        label = ""
        for key in ("label", "entity_type", "entity", "type", "name", "category"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                label = value.strip()
                break
        text = None
        for key in ("text", "value", "match", "excerpt"):
            value = entry.get(key)
            if isinstance(value, str):
                text = value
                break
        start = entry.get("start") if isinstance(entry.get("start"), int) else None
        end = entry.get("end") if isinstance(entry.get("end"), int) else None
        score = _row_score(entry)
        spans.append(
            MatchedSpan(label=label or "match", text=text, start=start, end=end, score=score)
        )
    return spans


def _mask(text: str, spans: Sequence[MatchedSpan]) -> str:
    """Blank out every matched span, longest offsets first so indexes hold.

    Spans the checker located by offset are masked in place; spans it only
    quoted are masked by replacing the quoted text. A span it neither located
    nor quoted cannot be masked, and the text is returned untouched rather than
    mangled.
    """
    masked = text
    positioned = sorted(
        (s for s in spans if s.start is not None and s.end is not None and s.end > s.start),
        key=lambda s: s.start or 0,
        reverse=True,
    )
    for span in positioned:
        start, end = max(0, span.start or 0), min(len(masked), span.end or 0)
        if start < end:
            masked = masked[:start] + "*" * (end - start) + masked[end:]
    for span in spans:
        if span.start is None and span.text:
            masked = masked.replace(span.text, "*" * len(span.text))
    return masked


#: Guardrail types whose whole purpose is keeping a kind of data out of storage.
_DATA_PROTECTION_TYPES: Final[frozenset[str]] = frozenset(
    {GuardrailType.PII.value, GuardrailType.SECRETS.value}
)


def _locatable(span: MatchedSpan) -> bool:
    """Whether :func:`_mask` can actually remove this span from a text."""
    if span.start is not None and span.end is not None and span.end > span.start:
        return True
    return span.start is None and bool(span.text)


def _event_sample(
    masked_text: str, guardrail: GuardrailConfig, matches: Sequence[MatchedSpan], limit: int
) -> str:
    """What an event row may keep of the content that fired it.

    The sample is there so an operator can recognise the case. It used to be the
    first few hundred *raw* characters, so a PII guardrail that masked an e-mail
    address out of the telemetry store wrote the same address into our own
    database, the detections feed and its CSV export -- readable by any viewer.
    The guardrail leaked exactly what it was configured to protect.

    ``masked_text`` is the content with everything *any* guardrail located in it
    already masked (masked before the cut: offsets refer to the whole text). A
    guardrail that exists to keep data out of storage, or whose action is Mask
    -- shadow trial or not -- and which could not say where that data is, keeps
    no sample at all: the same safe reading ingest applies to the payload.
    """
    protects = (
        guardrail.guardrail_type in _DATA_PROTECTION_TYPES
        or guardrail.action == GuardrailAction.MASK.value
    )
    if protects and not any(_locatable(span) for span in matches):
        return SAMPLE_WITHHELD
    return masked_text[:limit]


def _pair_rows(
    guardrails: Sequence[GuardrailConfig], rows: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Match each guardrail to the validation row that answers for it.

    The scanner answers one row per requested validation, in request order, and
    names a row only by its *type*. Two guardrails of one type -- a Global PII
    mask and a stricter agent-scoped one, two topic lists -- are therefore told
    apart by position and by nothing else. Matching on the name alone handed
    both of them the first row of that type: the second guardrail never fired
    on its own verdict, or fired (and blocked) on the first one's.

    So position comes first, whenever the counts line up and no row names a
    validation other than the one asked for in its place. Failing that, by name
    -- each row answering for one guardrail only, and only where as many rows
    carry a name as guardrails asked for it. Anything else is "no verdict"
    rather than a verdict attributed to the wrong guardrail.
    """
    wanted = [validation_name(g.guardrail_type, g.config).upper() for g in guardrails]
    names = [_row_name(row) for row in rows]
    if len(rows) == len(guardrails) and all(
        not name or name == want for name, want in zip(names, wanted, strict=True)
    ):
        return {
            guardrail.id: row for guardrail, row in zip(guardrails, rows, strict=True)
        }

    by_name: dict[str, list[dict[str, Any]]] = {}
    for row, name in zip(rows, names, strict=True):
        if name:
            by_name.setdefault(name, []).append(row)
    asking: dict[str, list[GuardrailConfig]] = {}
    for guardrail, want in zip(guardrails, wanted, strict=True):
        asking.setdefault(want, []).append(guardrail)

    paired: dict[str, dict[str, Any]] = {}
    for want, group in asking.items():
        answers = by_name.get(want, [])
        if len(answers) == len(group):
            for guardrail, row in zip(group, answers, strict=True):
                paired[guardrail.id] = row

    unmatched = [
        guardrail
        for guardrail, want in zip(guardrails, wanted, strict=True)
        if want not in by_name
    ]
    leftovers = [row for row, name in zip(rows, names, strict=True) if not name]
    if unmatched and len(unmatched) == len(leftovers):
        for guardrail, row in zip(unmatched, leftovers, strict=True):
            paired[guardrail.id] = row
    return paired


#: The validations the inline scanner actually implements. Anything else sent
#: in the same request 400s the whole batch, so unsupported guardrails are
#: excluded from the call and simply produce no verdict — never a simulated one.
SUPPORTED_CHECKER_VALIDATIONS: Final[frozenset[str]] = frozenset(
    {"PII", "TOPIC", "PROMPT_INJECTION", "CUSTOM_CLASSIFIER"}
)


def checker_supported(guardrail: GuardrailConfig) -> bool:
    """Whether the inline scanner can answer for this guardrail at all."""
    return (
        validation_name(guardrail.guardrail_type, guardrail.config).upper()
        in SUPPORTED_CHECKER_VALIDATIONS
    )


def checker_runnable(guardrail: GuardrailConfig) -> bool:
    """Whether the scanner can be asked about this guardrail as it is configured.

    A Topic guardrail with no topics is refused by the scanner, and the refusal
    used to be read as "the scanner cannot run TOPIC": every correctly configured
    Topic guardrail was suspended along with it. It is left out of the request
    instead, exactly as an unsupported type is, and the read model says why.
    """
    return checker_supported(guardrail) and (
        config_problem(guardrail.guardrail_type, guardrail.config) is None
    )


def _checker_request(guardrail: GuardrailConfig) -> dict[str, Any]:
    """One validation entry in the scanner's own contract: {type, config}."""
    config = {
        "threshold": guardrail.threshold,
        **{
            key: value
            for key, value in (guardrail.config or {}).items()
            if key != "validation"
        },
    }
    name = validation_name(guardrail.guardrail_type, guardrail.config).upper()
    if name == "TOPIC":
        # The scanner requires a mode for topic checks; a guardrail listing
        # topics means those topics are restricted unless it says otherwise.
        config.setdefault("mode", "restrict")
    return {"type": name, "config": config}


#: Scanner failures that mean "not now" rather than "not ever": a gateway in
#: front of it, a service still warming up, a request that ran out of time.
#: Only a 500 -- the handler itself raising -- is worth backing off from.
_RETRY_LATER: Final[frozenset[int]] = frozenset({502, 503, 504})

#: Validation name -> monotonic instant until which the scanner is not asked
#: for it. Per process: the worst case is each worker discovering the same
#: broken validation once. A key of the form ``guardrail:<id>`` suspends one
#: guardrail rather than a whole validation (see :func:`_suspend_for`).
_SUSPENDED: dict[str, float] = {}

_GUARDRAIL_KEY: Final[str] = "guardrail:"

#: Scanner calls in flight at once for one ingest batch.
CHECK_CONCURRENCY: Final[int] = 8

#: How what is left of a batch's budget is shared between the two rounds a check
#: may need: the request carrying every validation, and -- if that fails -- the
#: same question asked one validation at a time. Both rounds must finish, and
#: what they learned be recorded, BEFORE the budget runs out (see _run_checker).
FIRST_ROUND_SHARE: Final[float] = 0.6
LAST_ROUND_SHARE: Final[float] = 0.8

#: A check that starts with at least this much of the budget still ahead of it
#: started on time. One that queued behind the others did not: it is still asked,
#: with whatever is left, but its timing out says how long the queue was and
#: nothing about the validation it asked for.
ON_TIME: Final[float] = 0.9


class ChecksOutOfTime(Exception):
    """A batch's budget ran out on a check that never had a fair hearing.

    Not an engine failure and not a verdict: the batch is stored unevaluated,
    which is what running out of budget has always meant, and nothing is
    suspended over it -- or one oversized batch could switch a healthy
    validation off for every workspace this worker serves.
    """


def _validation(guardrail: GuardrailConfig) -> str:
    return validation_name(guardrail.guardrail_type, guardrail.config).upper()


def suspended_validations() -> list[str]:
    """Validations the scanner recently proved unable to run, for diagnostics."""
    now = time.monotonic()
    return sorted(
        name
        for name, until in _SUSPENDED.items()
        if until > now and not name.startswith(_GUARDRAIL_KEY)
    )


def guardrail_suspended(guardrail: GuardrailConfig) -> bool:
    """Whether this worker has stopped asking the scanner about this guardrail."""
    return _is_suspended(_validation(guardrail)) or _is_suspended(_GUARDRAIL_KEY + guardrail.id)


def _is_suspended(name: str) -> bool:
    until = _SUSPENDED.get(name)
    if until is None:
        return False
    if until <= time.monotonic():
        del _SUSPENDED[name]
        return False
    return True


def _suspend(name: str, exc: Exception) -> None:
    """Back a validation off -- but only one the scanner actually refused.

    Back off when asking again would be futile or expensive, and only then.

    * Futile: the scanner answered 500, its handler having raised. It will
      raise on the next batch too -- the prompt-injection model is not
      installed -- so asking costs a round trip for a verdict that never comes.
    * Expensive: it accepted the request and never answered. Every batch then
      waits out the whole timeout before giving up, which is the latency this
      code exists to avoid.
    * Neither: it could not be reached, or answered 502/503 from in front of
      itself. That fails in microseconds, and it is what a *restarting*
      scanner looks like -- which is exactly what deploying one does to it.
      Standing PII scanning down for five minutes over that invents a
      governance gap out of a blip. Not evaluated this batch; asked again next.

    Found in production: our own deploy bounced the scanner and switched PII
    off for five minutes.
    """
    futile = isinstance(exc, EngineServerError) and exc.status not in _RETRY_LATER
    expensive = isinstance(exc, EngineTimeout)
    if not (futile or expensive):
        log.warning(
            "the content scanner could not answer %s checks (%s); this batch was not "
            "evaluated and the next one will try again",
            name,
            exc,
        )
        return
    _SUSPENDED[name] = time.monotonic() + settings.guardrail_suspend_seconds
    log.error(
        "the content scanner cannot run %s checks (%s); guardrails of that type are "
        "NOT being enforced and will not be retried for %.0fs",
        name,
        exc,
        settings.guardrail_suspend_seconds,
    )


def _suspend_for(guardrail: GuardrailConfig, exc: EngineError) -> None:
    """Suspend what the failure is actually about.

    A refusal (4xx) is about what was *sent*: this guardrail's config. Suspending
    the validation for it would switch off every other guardrail of the type, in
    every workspace this worker serves, over one tenant's typo. Anything else --
    a 5xx, a timeout -- is the scanner being unable to run the validation at all,
    whoever asks.
    """
    if isinstance(exc, EngineBadRequest):
        _SUSPENDED[_GUARDRAIL_KEY + guardrail.id] = (
            time.monotonic() + settings.guardrail_suspend_seconds
        )
        log.error(
            "the content scanner refused guardrail %s (%s): its configuration cannot be "
            "run; it is NOT being enforced and will not be retried for %.0fs",
            guardrail.id,
            exc,
            settings.guardrail_suspend_seconds,
        )
        return
    _suspend(_validation(guardrail), exc)


async def _run_checker(
    text: str,
    guardrails: Sequence[GuardrailConfig],
    *,
    isolate: bool = False,
    give_up_at: float | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Run the real checks for every supported guardrail, and time them.

    The scanner takes every validation in one request and answers 500 for the
    whole request if any one of them cannot run. One guardrail whose model is
    not installed therefore silenced every other guardrail in the workspace --
    a prompt-injection rule nobody could run switched PII scanning off.

    With ``isolate`` a refused request is asked again one validation at a time,
    so the healthy ones still answer, and whichever is broken is suspended
    rather than paid for on every batch. Without it the failure is raised,
    which is what the console's own "test this guardrail" wants to show.

    ``give_up_at`` is the monotonic instant the caller's budget ends. Both
    rounds used to run on the full per-call timeout, which together is longer
    than a batch's budget: with two guardrails in scope and a scanner that had
    stopped answering, the budget cancelled the second round before it could
    suspend anything. Nothing was learned, so EVERY batch held its request --
    and the database connection under it -- for the whole budget, and one slow
    validation cost the healthy ones their verdicts for good. Each call is now
    clipped to its share of what is left, so the second round ends first. A
    check that only started after queueing behind the rest of its batch is
    still asked, with what is left, but its silence suspends nothing (ON_TIME).
    """
    client = get_engine_client()
    supported = [guardrail for guardrail in guardrails if checker_runnable(guardrail)]
    if isolate:
        supported = [g for g in supported if not guardrail_suspended(g)]
    if not supported:
        return {}, 0
    timeout = settings.guardrail_check_timeout_seconds
    started = time.perf_counter()
    on_time = give_up_at is None or (
        give_up_at - time.monotonic() >= ON_TIME * settings.guardrail_batch_budget_seconds
    )

    def elapsed() -> int:
        return int(round((time.perf_counter() - started) * 1000))

    def allowance(share: float) -> float:
        if give_up_at is None:
            return timeout
        allowed = min(timeout, (give_up_at - time.monotonic()) * share)
        if allowed <= 0:
            raise ChecksOutOfTime("the batch's budget was spent before this check was asked")
        return allowed

    def unheard(exc: EngineError) -> None:
        # A refusal or a 5xx is an answer, whenever it comes. Silence is only
        # evidence from a check that had its proper time to be answered in.
        if isinstance(exc, EngineTimeout) and not on_time:
            raise ChecksOutOfTime(
                "a check that queued behind the rest of its batch ran out of budget"
            ) from exc

    async def ask(asked: Sequence[GuardrailConfig], limit: float) -> Any:
        # Bounded as a whole as well as per read: the HTTP timeout restarts with
        # every byte, and a scanner that trickles an answer is as gone as one
        # that sends none. The exchange itself is shielded by the client, so
        # giving up on it here does not tear a pooled connection down half way.
        async with deadline(limit, what="content check"):
            return await client.evaluate_guardrails(
                text, [_checker_request(guardrail) for guardrail in asked], timeout_seconds=limit
            )

    # Nothing is kept back for a second round that a single validation never has.
    first_share = FIRST_ROUND_SHARE if isolate and len(supported) > 1 else LAST_ROUND_SHARE
    try:
        payload = await ask(supported, allowance(first_share))
        return _pair_rows(supported, _check_rows(payload)), elapsed()
    except EngineError as exc:
        if not isolate:
            raise translate_engine_error(exc) from exc
        unheard(exc)  # and no second round on the sliver a queued check has left
        if len(supported) == 1:
            _suspend_for(supported[0], exc)
            return {}, elapsed()

    limit = allowance(LAST_ROUND_SHARE)

    async def alone(guardrail: GuardrailConfig) -> dict[str, dict[str, Any]]:
        try:
            answer = await ask([guardrail], limit)
        except EngineError as exc:
            unheard(exc)
            _suspend_for(guardrail, exc)
            return {}
        return _pair_rows([guardrail], _check_rows(answer))

    merged: dict[str, dict[str, Any]] = {}
    for paired in await asyncio.gather(*(alone(guardrail) for guardrail in supported)):
        merged.update(paired)
    return merged, elapsed()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _scoped(principal: Principal) -> Select:
    return select(GuardrailConfig).where(
        GuardrailConfig.workspace_id == principal.workspace_id
    )


def _filtered_stmt(
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> Select:
    stmt = _scoped(principal)
    stmt = apply_search(
        stmt,
        params,
        [
            GuardrailConfig.name,
            GuardrailConfig.guardrail_type,
            GuardrailConfig.action,
            GuardrailConfig.scope_ref,
        ],
    )
    stmt = apply_filters(
        stmt,
        {
            GuardrailConfig.status: status,
            GuardrailConfig.guardrail_type: guardrail_type,
            GuardrailConfig.action: action,
            GuardrailConfig.scope: scope,
            GuardrailConfig.owner_user_id: owner_user_id,
        },
    )
    return apply_sort(stmt, params, SORTABLE, default=GuardrailConfig.name, default_desc=False)


class _Rollup:
    """Counted activity for one guardrail over the window."""

    __slots__ = ("triggers", "blocked", "masked", "last_triggered_at")

    def __init__(
        self,
        triggers: int = 0,
        blocked: int = 0,
        masked: int = 0,
        last_triggered_at: dt.datetime | None = None,
    ) -> None:
        self.triggers = triggers
        self.blocked = blocked
        self.masked = masked
        self.last_triggered_at = last_triggered_at


def _events_where(condition: Any) -> Any:
    """SUM of a predicate over event rows, zero rather than NULL when empty."""
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


async def _rollups(
    session: AsyncSession,
    principal: Principal,
    guardrail_ids: Sequence[str],
    since: dt.datetime,
) -> dict[str, _Rollup]:
    """Triggers, blocks, masks and last-fired per guardrail, in one statement."""
    if not guardrail_ids:
        return {}
    rows = (
        await session.execute(
            select(
                GuardrailEvent.guardrail_id,
                func.count(GuardrailEvent.id),
                _events_where(GuardrailEvent.action_taken == GuardrailAction.BLOCK.value),
                _events_where(GuardrailEvent.action_taken == GuardrailAction.MASK.value),
                func.max(GuardrailEvent.occurred_at),
            )
            .where(
                GuardrailEvent.workspace_id == principal.workspace_id,
                GuardrailEvent.guardrail_id.in_(list(guardrail_ids)),
                GuardrailEvent.occurred_at >= since,
            )
            .group_by(GuardrailEvent.guardrail_id)
        )
    ).all()
    return {
        row[0]: _Rollup(int(row[1] or 0), int(row[2] or 0), int(row[3] or 0), row[4])
        for row in rows
    }


async def _coverage_names(
    session: AsyncSession, principal: Principal, rows: Sequence[GuardrailConfig]
) -> dict[str, str]:
    """Agent names for the agent-scoped guardrails on the page."""
    agent_ids = {
        row.scope_ref
        for row in rows
        if row.scope == GuardrailScope.AGENT.value and row.scope_ref
    }
    if not agent_ids:
        return {}
    found = (
        await session.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == principal.workspace_id, Agent.id.in_(agent_ids)
            )
        )
    ).all()
    return {agent_id: name for agent_id, name in found}


def enforcement_of(guardrail: GuardrailConfig) -> tuple[str, str | None]:
    """What actually happens to live content under this guardrail, and why.

    "Active" on the screen used to be the whole story, and it was not true: a
    rule the scanner does not implement, a Topic rule with no topics, a
    validation the scanner has just failed -- each reads Active and enforces
    nothing. The most permanent reason wins, so the sentence names the thing an
    operator can actually fix.
    """
    if guardrail.status == GuardrailStatus.DISABLED.value:
        return "disabled", "Disabled: content is not checked against this guardrail."
    if not checker_supported(guardrail):
        wanted = validation_name(guardrail.guardrail_type, guardrail.config)
        return "unsupported", (
            f"The content checker on this deployment does not implement '{wanted}', so "
            "this rule is recorded but never enforced at ingest."
        )
    problem = config_problem(guardrail.guardrail_type, guardrail.config)
    if problem is not None:
        return "misconfigured", problem
    if guardrail_suspended(guardrail):
        return "suspended", (
            "The content checker failed to run this check, so it has been suspended and "
            f"is retried within {settings.guardrail_suspend_seconds / 60:.0f} minutes. "
            "Until then content passes unchecked."
        )
    if guardrail.status == GuardrailStatus.TUNING.value:
        return "shadow", "Tuning: content is checked and detections are recorded, never enforced."
    return "enforced", None


def _read(
    guardrail: GuardrailConfig,
    *,
    rollup: _Rollup | None = None,
    coverage: str | None = None,
    owner: str | None = None,
) -> GuardrailRead:
    counted = rollup or _Rollup()
    enforcement, not_enforced_reason = enforcement_of(guardrail)
    return GuardrailRead(
        id=guardrail.id,
        name=guardrail.name,
        guardrail_type=GuardrailType(guardrail.guardrail_type),
        status=GuardrailStatus(guardrail.status),
        action=GuardrailAction(guardrail.action),
        threshold=guardrail.threshold,
        scope=GuardrailScope(guardrail.scope),
        scope_ref=guardrail.scope_ref,
        coverage=coverage_label(guardrail.scope, guardrail.scope_ref, coverage),
        triggers_30d=counted.triggers,
        blocked_30d=counted.blocked,
        masked_30d=counted.masked,
        effectiveness=guardrail.effectiveness,
        added_latency_ms=guardrail.added_latency_ms,
        last_triggered_at=counted.last_triggered_at or guardrail.last_triggered_at,
        config=dict(guardrail.config or {}),
        engine_guardrail_id=guardrail.engine_guardrail_id,
        owner_user_id=guardrail.owner_user_id,
        owner_name=owner,
        checker_supported=checker_supported(guardrail),
        enforcement=enforcement,
        not_enforced_reason=not_enforced_reason,
        created_at=guardrail.created_at,
        updated_at=guardrail.updated_at,
        created_by=guardrail.created_by,
        updated_by=guardrail.updated_by,
    )


async def read_many(
    session: AsyncSession,
    principal: Principal,
    rows: Sequence[GuardrailConfig],
    *,
    window_days: int = WINDOW_DAYS,
) -> list[GuardrailRead]:
    """Turn rows into table records, counting their activity in one statement."""
    rollups = await _rollups(
        session, principal, [row.id for row in rows], _window_start(window_days)
    )
    coverage = await _coverage_names(session, principal, rows)
    owners = await owner_names(session, principal, [row.owner_user_id for row in rows])
    return [
        _read(
            row,
            rollup=rollups.get(row.id),
            coverage=coverage.get(row.scope_ref or ""),
            owner=owners.get(row.owner_user_id or ""),
        )
        for row in rows
    ]


async def list_guardrails(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> tuple[Sequence[GuardrailConfig], int]:
    """One page of the workspace's guardrails, ordered by name by default."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        guardrail_type=guardrail_type,
        action=action,
        scope=scope,
        owner_user_id=owner_user_id,
    )
    return await paginate(session, stmt, params)


async def export_guardrails(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    owner_user_id: str | None = None,
) -> Sequence[GuardrailConfig]:
    """Every guardrail matching the table's filters, capped at :data:`EXPORT_LIMIT`."""
    stmt = _filtered_stmt(
        principal,
        params,
        status=status,
        guardrail_type=guardrail_type,
        action=action,
        scope=scope,
        owner_user_id=owner_user_id,
    ).limit(EXPORT_LIMIT)
    return (await session.execute(stmt)).scalars().all()


async def get_guardrail(
    session: AsyncSession, principal: Principal, guardrail_id: str
) -> GuardrailConfig:
    """Load one guardrail, or raise :class:`NotFound`.

    A guardrail in another workspace answers 404, not 403: a 403 would confirm
    the id is real.
    """
    guardrail = (
        await session.execute(_scoped(principal).where(GuardrailConfig.id == guardrail_id))
    ).scalar_one_or_none()
    if guardrail is None:
        raise NotFound(f"Guardrail '{guardrail_id}' does not exist.")
    return guardrail


def _match_count(matched: dict[str, Any] | None) -> int:
    """How many things one event's ``matched`` payload points at.

    Two producers write this field in different shapes. The inline checker
    writes ``{"count": N, "labels": [...], "spans": [...]}``; the SDK's
    ``log_guardrail_event`` passes the reporter's own dict through verbatim,
    which by convention is a label→count map like ``{"CARD": 1, "PHONE": 1}``
    (a label may also carry the list of matched values). Both must total the
    same way — an event whose ``matched`` names real entities must never
    render as zero matches beside them.
    """
    if not matched:
        return 0
    count = matched.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    spans = matched.get("spans")
    if isinstance(spans, list):
        return len(spans)
    total = 0
    for value in matched.values():
        if not value:
            continue
        if isinstance(value, bool):
            total += 1
        elif isinstance(value, int):
            total += value
        elif isinstance(value, (list, tuple)):
            total += len(value)
        else:
            total += 1
    return total


async def list_events(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    guardrail_id: str | None = None,
    agent_id: str | None = None,
    guardrail_type: str | None = None,
    action: str | None = None,
    window_days: int = WINDOW_DAYS,
) -> tuple[list[GuardrailEventRead], int]:
    """The recent-detections feed, newest first, with names attached.

    Filtering by guardrail type joins the definition table rather than
    denormalising the type onto every event row.
    """
    if guardrail_id is not None:
        await get_guardrail(session, principal, guardrail_id)

    stmt = select(GuardrailEvent).where(
        GuardrailEvent.workspace_id == principal.workspace_id,
        GuardrailEvent.occurred_at >= _window_start(window_days),
    )
    stmt = apply_search(stmt, params, [GuardrailEvent.sample, GuardrailEvent.trace_id])
    stmt = apply_filters(
        stmt,
        {
            GuardrailEvent.guardrail_id: guardrail_id,
            GuardrailEvent.agent_id: agent_id,
            GuardrailEvent.action_taken: action,
        },
    )
    if guardrail_type:
        stmt = stmt.where(
            GuardrailEvent.guardrail_id.in_(
                select(GuardrailConfig.id).where(
                    GuardrailConfig.workspace_id == principal.workspace_id,
                    GuardrailConfig.guardrail_type == guardrail_type,
                )
            )
        )
    stmt = apply_sort(
        stmt, params, EVENT_SORTABLE, default=GuardrailEvent.occurred_at, default_desc=True
    )

    rows, total = await paginate(session, stmt, params)
    if not rows:
        return [], total

    definitions = {
        row_id: (name, kind)
        for row_id, name, kind in (
            await session.execute(
                select(
                    GuardrailConfig.id, GuardrailConfig.name, GuardrailConfig.guardrail_type
                ).where(
                    GuardrailConfig.workspace_id == principal.workspace_id,
                    GuardrailConfig.id.in_({row.guardrail_id for row in rows}),
                )
            )
        ).all()
    }
    agent_ids = {row.agent_id for row in rows if row.agent_id}
    agents = (
        {
            agent_id: name
            for agent_id, name in (
                await session.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.workspace_id == principal.workspace_id, Agent.id.in_(agent_ids)
                    )
                )
            ).all()
        }
        if agent_ids
        else {}
    )

    payload: list[GuardrailEventRead] = []
    for row in rows:
        name, kind = definitions.get(row.guardrail_id, (None, None))
        payload.append(
            GuardrailEventRead(
                id=row.id,
                guardrail_id=row.guardrail_id,
                guardrail_name=name,
                guardrail_type=GuardrailType(kind) if kind else None,
                agent_id=row.agent_id,
                agent_name=agents.get(row.agent_id or ""),
                trace_id=row.trace_id,
                occurred_at=row.occurred_at,
                action_taken=GuardrailAction(row.action_taken),
                score=row.score,
                match_count=_match_count(row.matched),
                matched=dict(row.matched or {}),
                sample=row.sample,
            )
        )
    return payload, total


async def _pii_items_masked(
    session: AsyncSession, principal: Principal, since: dt.datetime
) -> int:
    """Entities masked by PII guardrails in the window.

    A single call can mask several entities, so this counts the matches carried
    by the events rather than the events themselves. The scan is bounded; past
    :data:`PII_SCAN_LIMIT` events it falls back to the cached per-guardrail
    rollups, which count the same thing more coarsely.
    """
    pii_ids = select(GuardrailConfig.id).where(
        GuardrailConfig.workspace_id == principal.workspace_id,
        GuardrailConfig.guardrail_type == GuardrailType.PII.value,
    )
    rows = (
        await session.execute(
            select(GuardrailEvent.matched)
            .where(
                GuardrailEvent.workspace_id == principal.workspace_id,
                GuardrailEvent.occurred_at >= since,
                GuardrailEvent.action_taken == GuardrailAction.MASK.value,
                GuardrailEvent.guardrail_id.in_(pii_ids),
            )
            .limit(PII_SCAN_LIMIT)
        )
    ).all()

    if len(rows) < PII_SCAN_LIMIT:
        return sum(_match_count(row[0]) for row in rows)

    cached = (
        await session.execute(
            select(func.coalesce(func.sum(GuardrailConfig.masked_30d), 0)).where(
                GuardrailConfig.workspace_id == principal.workspace_id,
                GuardrailConfig.guardrail_type == GuardrailType.PII.value,
            )
        )
    ).scalar_one()
    return int(cached or 0)


async def summarise(
    session: AsyncSession, principal: Principal, *, window_days: int = WINDOW_DAYS
) -> GuardrailsSummary:
    """The five KPI cards, computed with SQL aggregates over the event table."""
    workspace = GuardrailConfig.workspace_id == principal.workspace_id

    def _count_where(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    totals = (
        await session.execute(
            select(
                func.count(GuardrailConfig.id),
                _count_where(GuardrailConfig.status == GuardrailStatus.ACTIVE.value),
                _count_where(GuardrailConfig.status == GuardrailStatus.DISABLED.value),
                _count_where(GuardrailConfig.status == GuardrailStatus.TUNING.value),
                func.avg(
                    case(
                        (
                            GuardrailConfig.status == GuardrailStatus.ACTIVE.value,
                            GuardrailConfig.added_latency_ms,
                        ),
                    )
                ),
            ).where(workspace)
        )
    ).one()

    now = _now()
    window = dt.timedelta(days=window_days)
    current_start, previous_start = now - window, now - 2 * window

    async def _counts(
        since: dt.datetime, until: dt.datetime
    ) -> tuple[int, int, dt.datetime | None]:
        row = (
            await session.execute(
                select(
                    func.count(GuardrailEvent.id),
                    _events_where(
                        GuardrailEvent.action_taken == GuardrailAction.BLOCK.value
                    ),
                    func.max(GuardrailEvent.occurred_at),
                ).where(
                    GuardrailEvent.workspace_id == principal.workspace_id,
                    GuardrailEvent.occurred_at >= since,
                    GuardrailEvent.occurred_at < until,
                )
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0), row[2]

    triggers, blocked, last_triggered = await _counts(current_start, now)
    previous_triggers, _, _ = await _counts(previous_start, current_start)

    masked_now = await _pii_items_masked(session, principal, current_start)
    masked_before = await _pii_items_masked(session, principal, previous_start)
    masked_before = max(0, masked_before - masked_now)

    def _delta(current: int, earlier: int) -> float | None:
        if earlier <= 0:
            return None
        return round((current - earlier) / earlier * 100, 1)

    # "Active" is a setting; "enforced" is a fact. The card counts the first, so
    # the summary also says how many of those are not the second.
    active_rows = (
        (
            await session.execute(
                select(GuardrailConfig).where(
                    workspace, GuardrailConfig.status == GuardrailStatus.ACTIVE.value
                )
            )
        )
        .scalars()
        .all()
    )
    not_enforced = sum(1 for row in active_rows if enforcement_of(row)[0] != "enforced")

    latency = totals[4]
    return GuardrailsSummary(
        window_days=window_days,
        active=int(totals[1] or 0),
        configured=int(totals[0] or 0),
        disabled=int(totals[2] or 0),
        tuning=int(totals[3] or 0),
        triggers=triggers,
        triggers_delta_percent=_delta(triggers, previous_triggers),
        blocked=blocked,
        pii_items_masked=masked_now,
        pii_items_masked_delta_percent=_delta(masked_now, masked_before),
        avg_added_latency_ms=round(float(latency), 1) if latency is not None else None,
        last_triggered_at=last_triggered,
        not_enforced=not_enforced,
        suspended_validations=suspended_validations(),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


#: What the mirrored evaluator actually scans for, per guardrail type. These
#: are heuristics run against every sampled trace by the engine's metric
#: runner — real checks, deliberately conservative ones.
_SCAN_PATTERNS: Final[dict[str, dict[str, str]]] = {
    "PII": {
        "EMAIL": r"[\w.+-]+@[\w-]+\.[\w.]{2,}",
        "PHONE": r"\+?\d[\d\s().-]{8,}\d",
        "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
        "CARD": r"\b(?:\d[ -]?){13,16}\b",
    },
    "SECRETS": {
        "API_KEY": r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}",
        "AWS_KEY": r"\bAKIA[0-9A-Z]{16}\b",
        "BEARER": r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}",
        "PRIVATE_KEY": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    },
    "PROMPT_INJECTION": {
        "OVERRIDE": r"ignore (all |any )?(previous|prior|above) (instructions|prompts)",
        "SYSTEM_SWAP": r"you are now|pretend to be|act as if you (are|were)",
        "EXFIL": r"(reveal|print|show).{0,30}(system prompt|instructions)",
    },
}

_METRIC_SOURCE_TEMPLATE: Final[str] = '''\
import re
from typing import Any

from {metric_library}.evaluation.metrics import base_metric, score_result

PATTERNS = {patterns!r}


class GuardrailScan(base_metric.BaseMetric):
    """Pattern scan mirrored from the FD AI Command Center guardrail {guardrail_name!r}."""

    def __init__(self, name: str = {metric_name!r}):
        self.name = name

    def score(self, output: Any = "", **ignored: Any) -> score_result.ScoreResult:
        text = output if isinstance(output, str) else str(output)
        hits = sorted(
            label
            for label, pattern in PATTERNS.items()
            if re.search(pattern, text, re.IGNORECASE)
        )
        return score_result.ScoreResult(
            value=0.0 if hits else 1.0,
            name=self.name,
            reason="matched: " + ", ".join(hits) if hits else "clean",
        )
'''


def _metric_source(guardrail: GuardrailConfig) -> str:
    """Executable scan code for the engine's metric runner.

    Patterns come from the guardrail's type, and a ``patterns`` mapping in the
    config extends or replaces them — so a Custom guardrail with no patterns
    scores every trace 1.0 and says "clean", which is what a rule with nothing
    to look for honestly does.
    """
    validation = validation_name(guardrail.guardrail_type, guardrail.config).upper()
    patterns = dict(_SCAN_PATTERNS.get(validation, {}))
    configured = (guardrail.config or {}).get("patterns")
    if isinstance(configured, dict):
        patterns.update({str(key): str(value) for key, value in configured.items()})
    metric_name = validation.lower() + "_scan"
    return _METRIC_SOURCE_TEMPLATE.format(
        metric_library=settings.engine_metric_library,
        patterns=patterns,
        guardrail_name=guardrail.name,
        metric_name=metric_name,
    )


def _engine_definition(
    guardrail: GuardrailConfig, project_ids: Sequence[str]
) -> dict[str, Any]:
    """The guardrail as the engine's rule store wants it.

    The store holds automation-rule evaluators — a discriminated union keyed by
    ``type`` — so the guardrail is mirrored as a ``user_defined_metric_python``
    rule carrying real scan code, attached to every namespace the guardrail's
    scope covers. Action and threshold stay on our side: they drive the inline
    checker at ingest, which the engine's rule vocabulary does not model.
    """
    return {
        "name": guardrail.name[:150],
        "type": "user_defined_metric_python",
        "project_ids": list(project_ids),
        "sampling_rate": 1.0,
        "enabled": guardrail.status == GuardrailStatus.ACTIVE.value,
        "code": {
            "metric": _metric_source(guardrail),
            "arguments": {"output": "output"},
        },
    }


async def _scope_project_ids(
    session: AsyncSession, guardrail: GuardrailConfig
) -> list[str]:
    """Engine namespaces of every provisioned agent the guardrail covers."""
    stmt = select(Agent.engine_project_id).where(
        Agent.workspace_id == guardrail.workspace_id,
        Agent.engine_project_id.is_not(None),
    )
    if guardrail.scope == GuardrailScope.AGENT.value and guardrail.scope_ref:
        stmt = stmt.where(Agent.id == guardrail.scope_ref)
    elif guardrail.scope == GuardrailScope.ENVIRONMENT.value and guardrail.scope_ref:
        stmt = stmt.where(Agent.environment == guardrail.scope_ref)
    return list((await session.execute(stmt)).scalars().all())


#: How long a console write waits on the engine's rule store before it carries
#: on without it.
MIRROR_BUDGET_SECONDS: Final[float] = 8.0

#: Outcomes of :func:`_mirror`, recorded on the audit row of the write.
MIRROR_SYNCED: Final[str] = "synced"
MIRROR_NOT_MIRRORED: Final[str] = "not_mirrored"
MIRROR_FAILED: Final[str] = "failed"


async def _mirror(session: AsyncSession, guardrail: GuardrailConfig) -> str:
    """Bring the engine's copy of the rule into line -- best effort, never fatal.

    Enforcement is the inline checker at ingest, driven by OUR row. The rule in
    the engine's store is a mirror of it, and a mirror that cannot be written
    must not cost the write it mirrors. It used to: every engine error was
    re-raised, the request rolled back, and so Disable -- the kill switch for a
    guardrail that is blocking production telemetry -- answered 503 while the
    engine restarted and 404 *for ever* once the engine had lost its copy of the
    rule. Tune, Edit and Create failed the same way.

    Now a failure is logged and reported to the caller for the audit row, and
    the next edit tries again. A rule the engine no longer holds is registered
    afresh. A guardrail whose scope covers no provisioned agent has its rule
    removed rather than patched to an empty namespace list: the store attaches
    an evaluator to at least one namespace, and one attached to none has
    nothing to run on. The same goes for a deployment that has switched the
    mirror off: the rule registered while it was on is removed, not abandoned.
    """
    # The rule store executes metric code that imports the engine's own library,
    # whose package name is deployment configuration (see
    # Settings.engine_metric_library). Without it there is no runnable code to
    # register; inline enforcement still applies at ingest.
    mirroring = bool(settings.engine_metric_library)
    if not mirroring and not guardrail.engine_guardrail_id:
        log.info(
            "guardrail %s not mirrored: engine metric library not configured",
            guardrail.id,
        )
        return MIRROR_NOT_MIRRORED

    # SQL before the clock starts: a statement cancelled half way costs the
    # request its connection.
    project_ids = await _scope_project_ids(session, guardrail) if mirroring else []
    client = get_engine_client()
    try:
        async with asyncio.timeout(MIRROR_BUDGET_SECONDS):
            if not project_ids:
                # Nothing to attach to -- or the mirror has been switched off,
                # which has to UNLOAD what was mirrored while it was on: nothing
                # reads these rules' scores, and one left behind is run by the
                # metric runner against every sampled trace for good.
                if guardrail.engine_guardrail_id:
                    # Already gone from the store is what was wanted.
                    with contextlib.suppress(EngineNotFound):
                        await client.delete_guardrails([guardrail.engine_guardrail_id])
                    guardrail.engine_guardrail_id = None
                # With no provisioned agents in scope there is nothing to attach
                # to yet; the next update re-mirrors once agents exist.
                log.info(
                    "guardrail %s not mirrored to the engine: %s",
                    guardrail.id,
                    "no provisioned agents in scope"
                    if mirroring
                    else "engine metric library not configured; its rule was removed",
                )
                return MIRROR_NOT_MIRRORED

            definition = _engine_definition(guardrail, project_ids)
            if guardrail.engine_guardrail_id:
                try:
                    await client.update_guardrail(guardrail.engine_guardrail_id, definition)
                    return MIRROR_SYNCED
                except EngineNotFound:
                    # The store lost its copy (it was reset, or the namespace
                    # went away). Retrying the PATCH can never work; register
                    # the rule again instead.
                    guardrail.engine_guardrail_id = None
            guardrail.engine_guardrail_id = await client.create_guardrail(definition)
            return MIRROR_SYNCED
    except (EngineError, TimeoutError) as exc:
        log.warning(
            "guardrail %s was saved but its engine rule could not be brought into line "
            "(%s); inline enforcement is unaffected and the next edit retries",
            guardrail.id,
            exc or type(exc).__name__,
        )
        return MIRROR_FAILED


def _checked_config(guardrail_type: str, config: dict[str, Any] | None) -> dict[str, Any]:
    """The config as it will be stored, or a 422 naming the key that is wrong."""
    try:
        return normalise_config(guardrail_type, config)
    except ValueError as exc:
        raise ValidationFailed(str(exc), details={"field": "config"}) from exc


async def _validate_scope(
    session: AsyncSession, principal: Principal, scope: str, scope_ref: str | None
) -> None:
    """A scoped guardrail must point at something that exists in this workspace."""
    if scope == GuardrailScope.GLOBAL.value:
        return
    if not scope_ref:
        raise PreconditionFailed(f"A {scope}-scoped guardrail needs a scope reference.")
    if scope == GuardrailScope.AGENT.value:
        agent = (
            await session.execute(
                select(Agent.id).where(
                    Agent.workspace_id == principal.workspace_id, Agent.id == scope_ref
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise NotFound(f"Agent '{scope_ref}' does not exist.")


async def create_guardrail(
    session: AsyncSession,
    principal: Principal,
    payload: GuardrailCreate,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """Register a guardrail and mirror it into the engine's rule store."""
    principal.require(Role.ADMIN)
    await _validate_scope(session, principal, payload.scope.value, payload.scope_ref)
    config = _checked_config(payload.guardrail_type.value, payload.config)

    clash = (
        await session.execute(_scoped(principal).where(GuardrailConfig.name == payload.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise Conflict(f"A guardrail named '{payload.name}' already exists.")

    guardrail = GuardrailConfig(
        workspace_id=principal.workspace_id,
        name=payload.name,
        guardrail_type=payload.guardrail_type.value,
        status=payload.status.value,
        action=payload.action.value,
        threshold=payload.threshold,
        scope=payload.scope.value,
        scope_ref=payload.scope_ref,
        config=config,
        owner_user_id=payload.owner_user_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(guardrail)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"A guardrail named '{payload.name}' already exists.") from exc

    mirrored = await _mirror(session, guardrail)
    await audit.record(
        session,
        principal=principal,
        action="guardrail.created",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{guardrail.guardrail_type} guardrail, action {guardrail.action}",
        metadata={
            "threshold": guardrail.threshold,
            "scope": guardrail.scope,
            "engine_mirror": mirrored,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


async def update_guardrail(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    payload: GuardrailUpdate,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """Apply a partial update, honouring the optimistic-concurrency guard."""
    principal.require(Role.ADMIN)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    if payload.expected_updated_at is not None and guardrail.updated_at is not None:
        expected = _as_utc(payload.expected_updated_at)
        if abs((_as_utc(guardrail.updated_at) - expected).total_seconds()) > 1:
            raise Conflict(
                f"'{guardrail.name}' was changed by someone else. Reload and try again."
            )

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    if not changes:
        return guardrail

    if "name" in changes and changes["name"] != guardrail.name:
        clash = (
            await session.execute(
                _scoped(principal).where(
                    GuardrailConfig.name == changes["name"],
                    GuardrailConfig.id != guardrail.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict(f"A guardrail named '{changes['name']}' already exists.")

    scope = changes.get("scope", GuardrailScope(guardrail.scope))
    scope_value = scope.value if isinstance(scope, GuardrailScope) else scope
    if "scope" in changes or "scope_ref" in changes:
        await _validate_scope(
            session,
            principal,
            scope_value,
            changes.get("scope_ref", guardrail.scope_ref),
        )

    if "config" in changes or "guardrail_type" in changes:
        # ``config`` replaces the stored one whole: the console sends what the
        # form holds. It is checked against the type it will live under, which
        # may be changing in the same request.
        kind = changes.get("guardrail_type", guardrail.guardrail_type)
        checked = _checked_config(
            kind.value if isinstance(kind, GuardrailType) else kind,
            changes.get("config", guardrail.config),
        )
        if "config" in changes or checked != (guardrail.config or {}):
            changes["config"] = checked

    vocabularies = (GuardrailType, GuardrailStatus, GuardrailAction, GuardrailScope)
    for field, value in changes.items():
        setattr(guardrail, field, value.value if isinstance(value, vocabularies) else value)
    guardrail.updated_by = principal.actor
    await session.flush()

    mirrored = await _mirror(session, guardrail)
    await audit.record(
        session,
        principal=principal,
        action="guardrail.updated",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail="Updated " + ", ".join(sorted(changes)),
        metadata={"fields": sorted(changes), "engine_mirror": mirrored},
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


async def delete_guardrail(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Remove a guardrail here and in the engine. Its events go with it."""
    principal.require(Role.ADMIN)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    if guardrail.engine_guardrail_id:
        client = get_engine_client()
        try:
            await client.delete_guardrails([guardrail.engine_guardrail_id])
        except EngineNotFound:
            pass  # already gone from the store, which is what was asked for
        except EngineError as exc:
            raise translate_engine_error(exc) from exc

    await audit.record(
        session,
        principal=principal,
        action="guardrail.deleted",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=f"Removed {guardrail.guardrail_type} guardrail",
        request=request,
    )
    await session.delete(guardrail)
    await session.flush()


_STATUS_AUDIT_ACTIONS: Final[dict[GuardrailStatus, str]] = {
    GuardrailStatus.ACTIVE: "guardrail.enabled",
    GuardrailStatus.DISABLED: "guardrail.disabled",
    GuardrailStatus.TUNING: "guardrail.shadowed",
}


async def set_status(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    status: GuardrailStatus,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """Enable, disable or shadow a guardrail, in the record and in the engine."""
    principal.require(Role.OPERATOR)
    guardrail = await get_guardrail(session, principal, guardrail_id)
    if guardrail.status == status.value:
        raise PreconditionFailed(f"'{guardrail.name}' is already {status.value}.")

    previous = guardrail.status
    guardrail.status = status.value
    guardrail.updated_by = principal.actor
    await session.flush()
    mirrored = await _mirror(session, guardrail)

    await audit.record(
        session,
        principal=principal,
        action=_STATUS_AUDIT_ACTIONS[status],
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=f"{previous} to {status.value}",
        metadata={"engine_mirror": mirrored},
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


async def set_threshold(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    payload: GuardrailThresholdUpdate,
    *,
    request: Request | None = None,
) -> GuardrailConfig:
    """The Tune action: move the threshold, and the action with it if asked."""
    principal.require(Role.ADMIN)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    previous_threshold = guardrail.threshold
    previous_action = guardrail.action
    guardrail.threshold = payload.threshold
    if payload.action is not None:
        guardrail.action = payload.action.value
    guardrail.updated_by = principal.actor
    await session.flush()
    mirrored = await _mirror(session, guardrail)

    detail = f"Threshold {previous_threshold:.2f} to {payload.threshold:.2f}"
    if payload.action is not None and payload.action.value != previous_action:
        detail += f"; action {previous_action} to {payload.action.value}"
    if payload.reason:
        detail += f" ({payload.reason})"

    await audit.record(
        session,
        principal=principal,
        action="guardrail.tuned",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={
            "threshold": payload.threshold,
            "previous_threshold": previous_threshold,
            "engine_mirror": mirrored,
        },
        request=request,
    )
    await session.flush()
    await session.refresh(guardrail)
    return guardrail


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _hit_from(
    guardrail: GuardrailConfig, row: dict[str, Any] | None
) -> tuple[bool, float | None, list[MatchedSpan]]:
    if row is None:
        return False, None, []
    score = _row_score(row)
    triggered = _row_triggered(row, score, guardrail.threshold)
    return triggered, score, _row_matches(row)


#: ``session.info`` key a request's guardrail bookkeeping waits under.
_BOOKKEEPING_KEY: Final[str] = "guardrail_bookkeeping"


def _queue_bookkeeping(
    session: AsyncSession,
    guardrail_id: str,
    *,
    latency_ms: int | None = None,
    triggers: int = 0,
    blocked: int = 0,
    masked: int = 0,
    fired_at: dt.datetime | None = None,
) -> None:
    """Hold a guardrail's counters until the request commits.

    The ingest hook runs BEFORE the batch is handed to the engine, and an UPDATE
    keeps its row lock until the transaction ends. Written on the spot, a Global
    guardrail's row -- shared by every agent in the workspace -- stayed locked
    across up to four engine writes: concurrent batches queued behind one
    another on it, and so did the console's Disable. Written from
    ``before_commit`` the lock lives for the length of the commit. A request
    that rolls back drops what it queued, which is right: its evidence rows were
    never written either.
    """
    pending: dict[str, dict[str, Any]] | None = session.info.get(_BOOKKEEPING_KEY)
    if pending is None:
        pending = session.info[_BOOKKEEPING_KEY] = {}
    sync = session.sync_session
    if not event.contains(sync, "before_commit", _write_bookkeeping):
        event.listen(sync, "before_commit", _write_bookkeeping)
        event.listen(sync, "after_rollback", _drop_bookkeeping)

    entry = pending.setdefault(
        guardrail_id, {"latency_ms": None, "triggers": 0, "blocked": 0, "masked": 0, "at": None}
    )
    if latency_ms is not None:
        entry["latency_ms"] = latency_ms
    entry["triggers"] += triggers
    entry["blocked"] += blocked
    entry["masked"] += masked
    if fired_at is not None:
        entry["at"] = fired_at


def _write_bookkeeping(sync_session: Session) -> None:
    """Apply the queued counters as increments, in id order.

    Increments, so two workers ingesting at once do not lose each other's
    counts; id order, so two commits touching the same guardrails cannot
    deadlock; and ``updated_at`` named and set to itself, because a guardrail
    firing is not an edit to it (see :func:`db.base.stamp`).
    """
    pending: dict[str, dict[str, Any]] = sync_session.info.pop(_BOOKKEEPING_KEY, None) or {}
    for guardrail_id in sorted(pending):
        entry = pending[guardrail_id]
        values: dict[str, Any] = {"updated_at": GuardrailConfig.updated_at}
        if entry["latency_ms"] is not None:
            values["added_latency_ms"] = entry["latency_ms"]
        if entry["triggers"]:
            values.update(
                triggers_30d=GuardrailConfig.triggers_30d + entry["triggers"],
                blocked_30d=GuardrailConfig.blocked_30d + entry["blocked"],
                masked_30d=GuardrailConfig.masked_30d + entry["masked"],
                last_triggered_at=entry["at"],
            )
        if len(values) == 1:
            continue
        sync_session.execute(
            sa_update(GuardrailConfig)
            .where(GuardrailConfig.id == guardrail_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )


def _drop_bookkeeping(sync_session: Session) -> None:
    sync_session.info.pop(_BOOKKEEPING_KEY, None)


async def test_guardrail(
    session: AsyncSession,
    principal: Principal,
    guardrail_id: str,
    text: str,
    *,
    request: Request | None = None,
) -> GuardrailTestResult:
    """Run one real check against a sample and report what it found.

    The check is executed by the engine's content checker, and the latency is
    the measured round trip. Nothing is written to the event table: a test must
    not move the counters the Guardrails screen reports.
    """
    principal.require(Role.OPERATOR)
    guardrail = await get_guardrail(session, principal, guardrail_id)

    if not checker_supported(guardrail):
        # Never simulate a verdict: a validation the scanner does not
        # implement cannot be tested, and saying "Clear" would be a lie.
        wanted = validation_name(guardrail.guardrail_type, guardrail.config)
        raise PreconditionFailed(
            f"The content checker on this deployment does not implement "
            f"'{wanted}'. It answers for: "
            + ", ".join(sorted(SUPPORTED_CHECKER_VALIDATIONS))
            + ".",
            details={"validation": wanted},
        )

    problem = config_problem(guardrail.guardrail_type, guardrail.config)
    if problem is not None:
        raise PreconditionFailed(problem, details={"field": "config"})

    paired, latency_ms = await _run_checker(text, [guardrail])
    triggered, score, matches = _hit_from(guardrail, paired.get(guardrail.id))
    enforced = guardrail.status == GuardrailStatus.ACTIVE.value
    action = GuardrailAction(guardrail.action)

    masked_text = None
    if triggered and action is GuardrailAction.MASK and matches:
        masked_text = _mask(text, matches)

    if triggered:
        detail = (
            f"{guardrail.name} triggered"
            + (f" at {score:.2f}" if score is not None else "")
            + f" ({len(matches)} match(es)); "
            + (f"{action.value} would be applied" if enforced else "shadow mode, nothing enforced")
        )
    else:
        detail = f"{guardrail.name} did not trigger on this sample."

    await audit.record(
        session,
        principal=principal,
        action="guardrail.tested",
        entity_type=ENTITY_TYPE,
        entity_id=guardrail.id,
        entity_label=guardrail.name,
        source_screen=SOURCE_SCREEN,
        detail=detail,
        metadata={"triggered": triggered, "score": score, "latency_ms": latency_ms},
        request=request,
    )
    await session.flush()

    return GuardrailTestResult(
        guardrail_id=guardrail.id,
        name=guardrail.name,
        guardrail_type=GuardrailType(guardrail.guardrail_type),
        triggered=triggered,
        verdict="Triggered" if triggered else "Clear",
        score=score,
        threshold=guardrail.threshold,
        action=action,
        action_applied=enforced,
        matched=matches,
        masked_text=masked_text,
        added_latency_ms=latency_ms,
        detail=detail,
    )


async def _runtime_guardrails(
    session: AsyncSession, workspace_id: str, agent_id: str | None, environment: str | None
) -> Sequence[GuardrailConfig]:
    """Every guardrail that applies to this call, in severity order."""
    scopes = [GuardrailConfig.scope == GuardrailScope.GLOBAL.value]
    if agent_id:
        scopes.append(
            (GuardrailConfig.scope == GuardrailScope.AGENT.value)
            & (GuardrailConfig.scope_ref == agent_id)
        )
    if environment:
        scopes.append(
            (GuardrailConfig.scope == GuardrailScope.ENVIRONMENT.value)
            & (GuardrailConfig.scope_ref == environment)
        )
    stmt = (
        select(GuardrailConfig)
        .where(
            GuardrailConfig.workspace_id == workspace_id,
            GuardrailConfig.status.in_(RUNTIME_STATUSES),
            or_(*scopes),
        )
        .order_by(GuardrailConfig.name.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def evaluate_content(
    session: AsyncSession,
    *,
    workspace_id: str,
    text: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
    principal: Principal | None = None,
    request: Request | None = None,
) -> ContentEvaluation:
    """Run every applicable guardrail over one piece of content.

    This is the runtime entry point the ingest path calls. It runs one check
    covering every guardrail in scope, applies the strongest action that fired,
    records an event per trigger, and returns the content with masked spans
    already replaced so the caller never has to re-apply a rule.

    Guardrails in Tuning status are evaluated and recorded but never enforced,
    which is what makes it safe to try a new rule against production traffic.
    Only a block is audited: a mask or a warning is fully described by its
    event row, and auditing every guarded call would drown the trail.
    """
    environment = None
    if agent_id:
        environment = (
            await session.execute(
                select(Agent.environment).where(
                    Agent.workspace_id == workspace_id, Agent.id == agent_id
                )
            )
        ).scalar_one_or_none()

    guardrails = await _runtime_guardrails(session, workspace_id, agent_id, environment)
    if not guardrails:
        return ContentEvaluation(
            allowed=True, blocked=False, text=text, evaluated=0, detail="No guardrails in scope."
        )

    paired, latency_ms = await _run_checker(text, guardrails)
    now = _now()
    per_guardrail_latency = max(1, latency_ms // max(1, len(guardrails)))

    hits: list[GuardrailHit] = []
    applied: list[tuple[GuardrailConfig, list[MatchedSpan]]] = []
    masked_text = text
    blocked = False

    judged = {
        guardrail.id: _hit_from(guardrail, paired[guardrail.id])
        for guardrail in guardrails
        if guardrail.id in paired
    }
    # What the event rows may keep: the content with everything any guardrail
    # located in it masked, whatever that guardrail's own action is.
    sample_text = _mask(
        text, [span for fired, _, found in judged.values() if fired for span in found]
    )

    for guardrail in guardrails:
        if guardrail.id not in judged:
            continue  # not asked, or the scanner could not answer: no verdict
        triggered, score, matches = judged[guardrail.id]
        # The measured cost is charged to every guardrail that ran, whether or
        # not it fired: the latency tile is about the checking, not the finding.
        # Queued, not assigned: assigning it would stamp updated_at, and a
        # guardrail being checked is not a guardrail being edited.
        smoothed = int(
            round(
                guardrail.added_latency_ms * (1 - LATENCY_SMOOTHING)
                + per_guardrail_latency * LATENCY_SMOOTHING
            )
        )
        if smoothed != guardrail.added_latency_ms:
            _queue_bookkeeping(session, guardrail.id, latency_ms=smoothed)
        if not triggered:
            continue

        enforced = guardrail.status == GuardrailStatus.ACTIVE.value
        action = GuardrailAction(guardrail.action)
        hits.append(
            GuardrailHit(
                guardrail_id=guardrail.id,
                name=guardrail.name,
                guardrail_type=GuardrailType(guardrail.guardrail_type),
                action=action,
                score=score,
                threshold=guardrail.threshold,
                matched=matches,
                enforced=enforced,
            )
        )

        action_taken = action if enforced else GuardrailAction.LOG
        if enforced:
            if action is GuardrailAction.BLOCK:
                blocked = True
            elif action is GuardrailAction.MASK and matches:
                masked_text = _mask(masked_text, matches)
            applied.append((guardrail, matches))

        _queue_bookkeeping(
            session,
            guardrail.id,
            triggers=1,
            blocked=1 if action_taken is GuardrailAction.BLOCK else 0,
            masked=(len(matches) or 1) if action_taken is GuardrailAction.MASK else 0,
            fired_at=now,
        )

        session.add(
            GuardrailEvent(
                workspace_id=workspace_id,
                guardrail_id=guardrail.id,
                agent_id=agent_id,
                trace_id=trace_id,
                occurred_at=now,
                action_taken=action_taken.value,
                score=score,
                matched={
                    "count": len(matches),
                    "labels": sorted({span.label for span in matches}),
                    "spans": [
                        span.model_dump(exclude={"text"}, mode="json") for span in matches
                    ],
                    "enforced": enforced,
                },
                sample=_event_sample(sample_text, guardrail, matches, EVENT_SAMPLE_LIMIT),
            )
        )

    strongest = None
    if applied:
        strongest = max(
            (guardrail for guardrail, _ in applied),
            key=lambda g: ACTION_SEVERITY.get(g.action, 0),
        )

    if blocked and principal is not None:
        names = ", ".join(hit.name for hit in hits if hit.action is GuardrailAction.BLOCK)
        await audit.record(
            session,
            principal=principal,
            action="guardrail.blocked",
            entity_type=ENTITY_TYPE,
            entity_id=strongest.id if strongest else None,
            entity_label=names,
            source_screen=SOURCE_SCREEN,
            detail=f"Content blocked by {names}",
            metadata={"agent_id": agent_id, "trace_id": trace_id},
            request=request,
        )

    await session.flush()

    return ContentEvaluation(
        allowed=not blocked,
        blocked=blocked,
        action=GuardrailAction(strongest.action) if strongest else None,
        text="" if blocked else masked_text,
        masked=masked_text != text and not blocked,
        hits=hits,
        evaluated=len(guardrails),
        added_latency_ms=latency_ms,
        detail=(
            f"{len(hits)} of {len(guardrails)} guardrail(s) triggered"
            if hits
            else f"{len(guardrails)} guardrail(s) cleared the content"
        ),
    )


async def evaluate_ingest_batch(
    session: AsyncSession,
    principal: Principal,
    candidates: Sequence[GuardrailCandidate],
) -> list[GuardrailVerdict]:
    """Judge a batch of ingested items against the guardrails in scope.

    This is the hook :mod:`services.ingest` duck-types on every trace/span
    batch. The split of responsibilities is ingest's: ingest extracts the
    content, applies the verdicts (block, mask) and writes the evidence rows;
    this side owns the checking and the per-guardrail counters. Tuning
    guardrails run in shadow — their verdict is reported as ``Log`` so the
    trial is visible in the evidence without being enforced.

    An engine failure propagates: ingest treats any exception from this hook
    as "not evaluated" and lets the telemetry through, which is the documented
    degrade-open posture for a broken checker.
    """
    from ..schemas.ingest import MAX_SAMPLE_LENGTH, GuardrailVerdict

    verdicts: list[GuardrailVerdict] = []
    now = _now()
    # One guardrail load per (agent, environment) pair: batches are almost
    # always single-agent, so this is one query, not one per item.
    scoped: dict[tuple[str | None, str | None], Sequence[GuardrailConfig]] = {}

    # The database first and on its own -- a session serves one operation at a
    # time -- so that the checks themselves can then run side by side.
    work: list[tuple[GuardrailCandidate, str, Sequence[GuardrailConfig]]] = []
    for candidate in candidates:
        text = candidate.text.strip()
        if not text:
            continue
        key = (candidate.agent_id, candidate.environment)
        if key not in scoped:
            scoped[key] = await _runtime_guardrails(
                session, principal.workspace_id, candidate.agent_id, candidate.environment
            )
        if scoped[key]:
            work.append((candidate, text, scoped[key]))
    if not work:
        return verdicts

    # A batch used to be checked one item after another, each on the full engine
    # timeout, inside the ingest request. Side by side, under one budget for the
    # whole batch: past it the batch is stored unevaluated, which ingest records
    # as exactly that, instead of the agent's request hanging on a slow scanner.
    gate = asyncio.Semaphore(CHECK_CONCURRENCY)
    # The checks are told when the budget ends, so that a scanner which has
    # stopped answering is found out -- and set aside -- inside it. The wait_for
    # below is the backstop for a batch that simply has too much to ask.
    give_up_at = time.monotonic() + settings.guardrail_batch_budget_seconds

    async def check(
        text: str, guardrails: Sequence[GuardrailConfig]
    ) -> tuple[dict[str, dict[str, Any]], int]:
        async with gate:
            return await _run_checker(text, guardrails, isolate=True, give_up_at=give_up_at)

    # The same content under the same guardrails is asked about once. A span
    # batch repeats itself -- a model step and the chain step around it carry the
    # same prompt and the same answer -- and every repeat was a scanner round
    # trip. Each item still gets its own verdict and its own evidence row.
    jobs: list[tuple[str, Sequence[GuardrailConfig]]] = []
    job_of: dict[tuple[str, tuple[str, ...]], int] = {}
    slots: list[int] = []
    for _, text, guardrails in work:
        key = (text, tuple(guardrail.id for guardrail in guardrails))
        if key not in job_of:
            job_of[key] = len(jobs)
            jobs.append((text, guardrails))
        slots.append(job_of[key])

    checked = await asyncio.wait_for(
        asyncio.gather(*(check(text, guardrails) for text, guardrails in jobs)),
        timeout=settings.guardrail_batch_budget_seconds,
    )

    # The measured cost is charged once per check that ran, to every guardrail
    # that was answered for -- whether or not it fired.
    latency: dict[str, int] = {}
    measured_before: dict[str, int] = {}
    for (_, guardrails), (paired, latency_ms) in zip(jobs, checked, strict=True):
        answered = [guardrail for guardrail in guardrails if guardrail.id in paired]
        per_guardrail_latency = max(1, latency_ms // max(1, len(answered)))
        for guardrail in answered:
            measured_before.setdefault(guardrail.id, guardrail.added_latency_ms)
            latency[guardrail.id] = int(
                round(
                    latency.get(guardrail.id, guardrail.added_latency_ms) * (1 - LATENCY_SMOOTHING)
                    + per_guardrail_latency * LATENCY_SMOOTHING
                )
            )
    for guardrail_id, smoothed in latency.items():
        if smoothed != measured_before[guardrail_id]:  # no row lock for no news
            _queue_bookkeeping(session, guardrail_id, latency_ms=smoothed)

    for (candidate, text, guardrails), slot in zip(work, slots, strict=True):
        paired, _ = checked[slot]

        fired: list[tuple[GuardrailConfig, float | None, list[MatchedSpan]]] = []
        for guardrail in guardrails:
            if guardrail.id not in paired:
                continue  # not asked, or the scanner could not answer: no verdict
            triggered, score, matches = _hit_from(guardrail, paired.get(guardrail.id))
            if triggered:
                fired.append((guardrail, score, matches))
        if not fired:
            continue
        # One guardrail's sample must not carry what another one located: a
        # Topic warning firing beside a PII mask would otherwise keep the address.
        masked_text = _mask(text, [span for _, _, matches in fired for span in matches])

        for guardrail, score, matches in fired:
            enforced = guardrail.status == GuardrailStatus.ACTIVE.value
            action_taken = GuardrailAction(guardrail.action) if enforced else GuardrailAction.LOG
            # Counted here and written at commit as increments: see
            # _queue_bookkeeping for why neither happens on the spot.
            _queue_bookkeeping(
                session,
                guardrail.id,
                triggers=1,
                blocked=1 if action_taken is GuardrailAction.BLOCK else 0,
                masked=(len(matches) or 1) if action_taken is GuardrailAction.MASK else 0,
                fired_at=now,
            )

            verdicts.append(
                GuardrailVerdict(
                    index=candidate.index,
                    guardrail_id=guardrail.id,
                    guardrail_name=guardrail.name,
                    action=action_taken.value,
                    score=score,
                    matched={
                        "count": len(matches),
                        "labels": sorted({span.label for span in matches}),
                        "spans": [
                            span.model_dump(exclude={"text"}, mode="json") for span in matches
                        ],
                        "enforced": enforced,
                    },
                    sample=_event_sample(masked_text, guardrail, matches, MAX_SAMPLE_LENGTH),
                    # The literal text found, so a mask removes that and only
                    # that. Never serialised: see the schema.
                    redactions=[
                        text[span.start : span.end]
                        for span in matches
                        if span.start is not None
                        and span.end is not None
                        and 0 <= span.start < span.end <= len(text)
                    ],
                )
            )
    return verdicts
