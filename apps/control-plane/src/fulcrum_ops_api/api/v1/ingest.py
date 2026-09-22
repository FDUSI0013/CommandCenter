"""SDK ingest routes.

Five endpoints carry every byte a customer's agent reports: traces with their
nested spans, spans on their own, feedback scores, governance events, and the
small configuration document an SDK fetches on start-up.

Three things are different here from every other router in this tree, and all
three follow from this being the machine-facing surface rather than a console
screen:

* **API keys only.** :func:`require_ingest_principal` refuses a browser session
  outright — a person is not an agent, and letting one post telemetry would let
  them forge an agent's history.
* **The body is read raw.** ``settings.ingest_max_body_bytes`` has to be
  enforced on the bytes that actually arrived, not on a Content-Length header a
  client is free to lie about. The typed batch models are still published to
  OpenAPI through ``openapi_extra`` so the SDKs can be generated from them.
* **A partial failure is still a 200.** The batch's fate lives in the per-item
  results. A non-2xx means the *request* could not be processed at all: too
  large, unauthorised, out of quota, or the telemetry store is unreachable.

Handlers here only enforce the transport limits, parse and delegate. Every
governance, commercial and storage decision lives in ``services.ingest``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from ...core.config import settings
from ...core.errors import PayloadTooLarge
from ...schemas.ingest import (
    MAX_EVENTS_PER_BATCH,
    MAX_SCORES_PER_BATCH,
    MAX_SPANS_PER_BATCH,
    MAX_TRACES_PER_BATCH,
    EventBatchIn,
    EventIn,
    IngestBatchResult,
    IngestConfigRead,
    ScoreBatchIn,
    ScoreIn,
    SpanBatchIn,
    SpanIn,
    TraceBatchIn,
    TraceIn,
    parse_batch,
    request_body_schema,
)
from ...services import ingest as service
from ..deps import CurrentPrincipal, Db, Principal

router = APIRouter(prefix="/ingest", tags=["Ingest"])


async def require_ingest_principal(principal: CurrentPrincipal) -> Principal:
    """Route dependency: an API key carrying the ``ingest`` scope, and nothing else.

    Shared with the OpenTelemetry receiver so both front doors are guarded by
    exactly the same rule.
    """
    service.authorise(principal)
    return principal


IngestPrincipal = Annotated[Principal, Depends(require_ingest_principal)]


async def read_body(request: Request) -> bytes:
    """Read the request body, refusing anything past the configured ceiling.

    The declared length is checked first so an oversized upload is refused
    before it is buffered, and the received length is checked afterwards because
    a chunked body declares nothing at all.
    """
    limit = settings.ingest_max_body_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge(
            f"The body is {int(declared):,} bytes; this endpoint accepts {limit:,}.",
            details={"max_bytes": limit, "declared_bytes": int(declared)},
        )

    body = await request.body()
    if len(body) > limit:
        raise PayloadTooLarge(
            f"The body is {len(body):,} bytes; this endpoint accepts {limit:,}.",
            details={"max_bytes": limit, "received_bytes": len(body)},
        )
    return body


Body = Annotated[bytes, Depends(read_body)]


# ---------------------------------------------------------------------------
# Configuration — fetched first, so it comes first.
# ---------------------------------------------------------------------------


@router.get(
    "/config",
    response_model=IngestConfigRead,
    summary="SDK start-up configuration",
)
async def get_config(
    principal: IngestPrincipal,
    session: Db,
    response: Response,
    if_none_match: Annotated[str | None, Header()] = None,
) -> IngestConfigRead | Response:
    """Sampling, batching, the guardrails in force and the rules to redact locally.

    Every agent process calls this on boot, so it is deliberately small and
    revalidates against an ETag: a fleet restart costs one conditional request
    per process, not one full read. The document changes only when a guardrail,
    a redaction rule or a tenant override does.
    """
    config = await service.sdk_config(session, principal)
    etag = f'W/"{config.revision}"'
    cache_control = f"private, max-age={config.refresh_after_seconds}"
    if if_none_match and etag in {value.strip() for value in if_none_match.split(",")}:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    return config


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@router.post(
    "/traces",
    response_model=IngestBatchResult,
    summary="Report a batch of traces",
    openapi_extra=request_body_schema(TraceBatchIn),
)
async def ingest_traces(
    principal: IngestPrincipal,
    session: Db,
    body: Body,
    request: Request,
) -> IngestBatchResult:
    """Ingest traces, each carrying its spans, feedback scores and metadata.

    The agent comes from the key's binding when it has one, from each trace's
    own `agent`, or from the batch-level default. An unbound key with the
    `agents:write` or `admin` scope registers an agent it has not seen before,
    which is what lets a new service start reporting without a console visit.

    Governance runs before storage: a policy or guardrail set to Block refuses
    the trace and says which control did it, Mask stores it with what matched
    removed, and everything else records the breach and lets it through.

    Ids are optional. One that is supplied must be a version 7 UUID, which is
    what the SDKs mint: the telemetry store addresses nothing else, and a trace
    it refuses comes back `telemetry_rejected` on its own row. When the trace is
    stored but some of its spans are not, the row stays `accepted` and
    `spans_rejected` counts them, with the cause in `reason`.
    """
    parsed = parse_batch(
        body, field="traces", item_model=TraceIn, max_items=MAX_TRACES_PER_BATCH
    )
    return await service.ingest_traces(session, principal, parsed, request=request)


@router.post(
    "/spans",
    response_model=IngestBatchResult,
    summary="Report a batch of spans",
    openapi_extra=request_body_schema(SpanBatchIn),
)
async def ingest_spans(
    principal: IngestPrincipal,
    session: Db,
    body: Body,
    request: Request,
) -> IngestBatchResult:
    """Ingest spans for traces that were reported separately.

    For SDKs that stream a long-running trace's spans as each one closes rather
    than buffering the whole trace, so every span must name its `trace_id`.
    """
    parsed = parse_batch(body, field="spans", item_model=SpanIn, max_items=MAX_SPANS_PER_BATCH)
    return await service.ingest_spans(session, principal, parsed, request=request)


@router.post(
    "/scores",
    response_model=IngestBatchResult,
    summary="Report a batch of feedback scores",
    openapi_extra=request_body_schema(ScoreBatchIn),
)
async def ingest_scores(
    principal: IngestPrincipal,
    session: Db,
    body: Body,
    request: Request,
) -> IngestBatchResult:
    """Attach feedback scores to traces, spans or conversation threads.

    Scores may arrive long after the run they describe — an end-user thumbs-down
    minutes later, a judge's verdict after an offline pass — so they are posted
    on their own rather than folded into the trace.
    """
    parsed = parse_batch(body, field="scores", item_model=ScoreIn, max_items=MAX_SCORES_PER_BATCH)
    return await service.ingest_scores(session, principal, parsed, request=request)


@router.post(
    "/events",
    response_model=IngestBatchResult,
    summary="Report a batch of governance events",
    openapi_extra=request_body_schema(EventBatchIn),
)
async def ingest_events(
    principal: IngestPrincipal,
    session: Db,
    body: Body,
    request: Request,
) -> IngestBatchResult:
    """Record guardrail triggers, policy violations and end-user feedback.

    These are governance state rather than telemetry: they land in the same
    tables the Guardrails, Policy Center and Feedback & Quality Loop screens
    read. Send `ref` to make a retry idempotent — a repeat comes back as a
    duplicate instead of a second row.
    """
    parsed = parse_batch(body, field="events", item_model=EventIn, max_items=MAX_EVENTS_PER_BATCH)
    return await service.ingest_events(session, principal, parsed, request=request)
