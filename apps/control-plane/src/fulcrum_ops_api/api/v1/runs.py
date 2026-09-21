"""Live Runs and Replay Studio routes.

Nine endpoints back two screens: the run table with its six filters and CSV
export, the KPI row and its four sparklines, the row inspector, the Full
Response and Execution Trace modals, the flag-for-review action, the live
server-sent-events stream behind the LIVE pill, and the ordered step list the
replay player scrubs through.

Handlers here only parse, delegate and shape. Tenancy, the telemetry scan and
every mapping decision live in ``services.runs``; the only logic in this module
is the stream's framing, which is an HTTP concern.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from ...core.errors import NotFound
from ...models.registry import Platform, RiskLevel
from ...schemas.runs import (
    ReplaySession,
    RunDetail,
    RunFlagRequest,
    RunFlagResult,
    RunHistoryPage,
    RunPolicy,
    RunRead,
    RunResponse,
    RunsSummary,
    RunStatus,
    RunStreamEvent,
    RunTrace,
    ScanInfo,
    TimeRange,
)
from ...services import agents as agents_service
from ...services import runs as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db, session_revoked

router = APIRouter(prefix="/runs", tags=["Runs"])

ListQuery = Annotated[ListParams, Depends(list_params)]

# Column order of the run export, matching the on-screen table.
RUN_CSV_COLUMNS: list[tuple[str, str]] = [
    ("id", "Run ID"),
    ("source", "Source"),
    ("agent", "Agent"),
    ("status", "Status"),
    ("model", "Model"),
    ("input_preview", "Input Preview"),
    ("tools", "Tools"),
    ("tokens", "Tokens"),
    ("cost", "Cost"),
    ("duration", "Duration"),
    ("confidence", "Confidence"),
    ("risk", "Risk"),
    ("policy", "Policy"),
    ("occurred_at", "Time"),
    ("tenant", "Tenant"),
    ("user", "User"),
]


def run_filters(
    params: ListQuery,
    tenant: Annotated[str | None, Query(description="Tenant recorded on the run")] = None,
    source: Annotated[
        Platform | None,
        Query(description="Azure AI Foundry, Copilot Studio, M365 Copilot, Custom Agent…"),
    ] = None,
    run_status: Annotated[
        RunStatus | None,
        Query(alias="status", description="Completed, Warned, Failed or Running"),
    ] = None,
    risk: Annotated[RiskLevel | None, Query(description="Low, Medium or High")] = None,
    policy: Annotated[
        RunPolicy | None, Query(description="Allowed, Warned or Blocked")
    ] = None,
    agent_id: Annotated[
        str | None, Query(description="Restrict to one agent, as Agent Detail does")
    ] = None,
) -> service.RunFilters:
    """The six dropdowns above the table, plus the search box and agent link."""
    return service.RunFilters(
        tenant=tenant,
        source=source,
        status=run_status,
        risk=risk,
        policy=policy,
        agent_id=agent_id,
        q=params.q,
    )


RunFiltersQuery = Annotated[service.RunFilters, Depends(run_filters)]


class RunPage(Page[RunRead]):
    """One page of the run table, and what the table was read from.

    The table is assembled from a capped scan of the window, and `total` counts
    what that scan held. The KPI row has always said when the cap was reached;
    the table beside it printed the same floor as though it were the count.
    `scan.truncated` is how it says so: `total` is then "at least", and the
    oldest runs of the window are not on any page.
    """

    scan: ScanInfo | None = None


TimeRangeQuery = Annotated[
    TimeRange, Query(alias="time_range", description="Window the table covers")
]


def _csv_record(run: RunRead) -> dict[str, object]:
    return {
        "id": run.id,
        "source": run.source.value if run.source else None,
        "agent": run.agent,
        "status": run.status.value,
        "model": run.model,
        "input_preview": run.input_preview,
        "tools": run.tools,
        "tokens": run.tokens,
        "cost": run.cost,
        "duration": run.duration_seconds,
        "confidence": run.confidence,
        "risk": run.risk.value if run.risk else None,
        "policy": run.policy.value,
        "occurred_at": run.occurred_at.isoformat(),
        "tenant": run.tenant,
        "user": run.user,
    }


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{run_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=RunsSummary, summary="Live Runs KPI summary")
async def get_summary(
    principal: CurrentPrincipal,
    session: Db,
    filters: RunFiltersQuery,
    time_range: TimeRangeQuery = TimeRange.LAST_24_HOURS,
) -> RunsSummary:
    """The four KPI cards and the four sparkline mini-KPIs.

    The selected window and the window immediately before it are read
    concurrently, so every "vs last 24h" delta is measured rather than modelled.
    The response carries a `scan` block saying how much telemetry the numbers
    were computed from, and a `previous_scan` block saying the same of the
    earlier window. When either was capped, `comparable` is false and every
    `*_delta_*` field is null: there is a figure for each window and no trend.
    A capped `scan` also carries `covered_from`: the figures, and the sparkline
    buckets, are measurements from that instant on and a floor before it.
    """
    return await service.summarise(
        session, principal, filters=filters, time_range=time_range
    )


@router.get("/export", summary="Export runs as CSV")
async def export_runs(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    filters: RunFiltersQuery,
    time_range: TimeRangeQuery = TimeRange.LAST_24_HOURS,
) -> StreamingResponse:
    """The current view as CSV, with the same filters, search and sort as the
    table, so the file matches what the operator is looking at."""
    runs = await service.export_runs(
        session, principal, params, filters=filters, time_range=time_range
    )
    body = to_csv([_csv_record(run) for run in runs], RUN_CSV_COLUMNS)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="live-runs-{stamp}.csv"'},
    )


@router.get("/stream", summary="Stream new runs")
async def stream_runs(
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    filters: RunFiltersQuery,
) -> EventSourceResponse:
    """Server-sent events carrying each new run as it lands, newest last.

    The stream honours the table's filters, opens with an `open` event so the
    console can light the LIVE pill on a connection it knows is established, and
    emits a heartbeat comment every 15 seconds so intermediate proxies keep an
    idle connection open. It closes on client disconnect or when it ages out;
    the console reconnects.

    A stream is reserved before the response starts, so a process already at its
    concurrency bound answers 429 rather than opening a connection it cannot
    serve.
    """
    if filters.agent_id:
        # Resolve first, so a missing or cross-workspace agent is a clean 404
        # rather than a stream that silently never emits. A key bound to a
        # different agent gets the same answer for the same reason.
        await agents_service.get_agent(session, principal, filters.agent_id)
        if not service.may_read_agent(principal, filters.agent_id):
            raise NotFound(f"Agent '{filters.agent_id}' does not exist.")

    slot = await service.acquire_stream_slot()

    # Everything this request needs from the database it has now read. The
    # request's session would otherwise stay checked out -- idle, inside the
    # transaction authentication opened -- for as long as the stream is open,
    # which is up to half an hour per tab. Ending the transaction hands the
    # connection back; the stream opens a short-lived session for each poll.
    await session.commit()

    def frame(event: str, run: RunRead | None = None) -> ServerSentEvent:
        payload = RunStreamEvent(
            event=event,
            run=run,
            emitted_at=dt.datetime.now(dt.UTC),
            stream_id=slot.stream_id,
        )
        return ServerSentEvent(
            event=event, id=run.id if run else None, data=payload.model_dump_json()
        )

    async def frames() -> AsyncIterator[ServerSentEvent]:
        yield frame("open")
        # The caller was authenticated once, before the first frame. This stream
        # then holds that decision for up to half an hour, so it re-asks -- not
        # per frame, which would put a query behind every run, but on a timer.
        # Without it, changing a password ends every other session except the
        # one already watching Live Runs, which is the one an attacker would
        # have open.
        #
        # What this guarantees precisely: NO RUN IS DELIVERED more than
        # STREAM_RECHECK_SECONDS after the session ended. It is not "the
        # connection closes within 30 seconds" -- the check sits in the loop
        # body, and a workspace reporting nothing never enters it, so a quiet
        # stream can stay open until it ages out. That is the right shape for
        # the risk: an idle stream is handing over nothing, and the first row it
        # would hand over is the one this refuses.
        next_recheck = time.monotonic() + service.STREAM_RECHECK_SECONDS
        async for run in service.stream_runs(principal, slot, filters=filters):
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now >= next_recheck:
                next_recheck = now + service.STREAM_RECHECK_SECONDS
                if await session_revoked(principal):
                    return
            yield frame("run", run)

    return EventSourceResponse(
        frames(),
        ping=int(service.STREAM_HEARTBEAT_SECONDS),
        ping_message_factory=lambda: ServerSentEvent(comment="keep-alive"),
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/history", response_model=RunHistoryPage, summary="Browse one agent's full run history"
)
async def run_history(
    principal: CurrentPrincipal,
    session: Db,
    agent_id: Annotated[str, Query(description="Agent whose history to browse")],
    cursor: Annotated[
        str | None, Query(description="`next_cursor` from the previous page")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunHistoryPage:
    """Replay Studio's browser: every run the store still holds for one agent,
    newest first, one cursor page at a time. Unlike the run table there is no
    time floor — keep passing `next_cursor` until it comes back null. The
    cursor is opaque: it is not a run id, and must be passed back unchanged.
    """
    return await service.run_history(
        session, principal, agent_id, cursor=cursor, limit=limit
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=RunPage, summary="List runs")
async def list_runs(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    filters: RunFiltersQuery,
    time_range: TimeRangeQuery = TimeRange.LAST_24_HOURS,
) -> RunPage:
    """One page of the run table, newest first by default.

    Runs are read from the telemetry engine for the workspace's own projects
    only. Free-text search covers the run id, agent, input preview, model,
    source, status, tenant and user; `sort` accepts any column key the table
    renders. `scan` says how much telemetry the page was assembled from; when
    `scan.truncated` is true, `total` is a floor rather than a count, and
    `scan.covered_from` is the instant from which every run is on a page.
    """
    runs, total, info = await service.list_runs(
        session, principal, params, filters=filters, time_range=time_range
    )
    page = RunPage.build(runs, total, params.page, params.page_size)
    page.scan = info
    return page


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------


@router.get("/{run_id}", response_model=RunDetail, summary="Get a run")
async def get_run(principal: CurrentPrincipal, session: Db, run_id: str) -> RunDetail:
    """The run inspector: execution summary, prompt and response, model and
    cost, tools, guardrails and policy, retrieval and citations, errors and
    fallbacks, and whether a trace and a replay exist.

    A run belonging to another workspace answers 404, not 403.
    """
    return await service.get_run_inspector(session, principal, run_id)


@router.get(
    "/{run_id}/response", response_model=RunResponse, summary="Get a run's full response"
)
async def get_run_response(
    principal: CurrentPrincipal, session: Db, run_id: str
) -> RunResponse:
    """The Full Response modal: the completion in full rather than the preview
    the table and inspector show."""
    return await service.get_response(session, principal, run_id)


@router.get("/{run_id}/trace", response_model=RunTrace, summary="Get a run's execution trace")
async def get_run_trace(principal: CurrentPrincipal, session: Db, run_id: str) -> RunTrace:
    """The Execution Trace modal: the run's real span hierarchy, each node with
    its timing, offset, tokens, cost, payload previews and guardrail verdicts.

    Spans with a missing parent are promoted to roots rather than dropped — a
    partially ingested trace still renders everything that was recorded. A run
    reported without spans has an empty `spans` and is described by the run's
    own `input`, `response`, `error` and `metadata`, which are always present.
    """
    return await service.get_trace_tree(session, principal, run_id)


@router.get("/{run_id}/replay", response_model=ReplaySession, summary="Get a run's replay")
async def get_run_replay(
    principal: CurrentPrincipal, session: Db, run_id: str
) -> ReplaySession:
    """The Replay Studio payload: the span tree flattened into ordered steps.

    Each step carries its prompt, retrieved chunks, tool call and result,
    guardrail verdicts, tokens, cost, duration and offset from the run's start,
    so the player can scrub to any point. `fidelity` is the share of steps whose
    input and output were both captured — a run ingested without payloads
    replays as a timeline and reports that honestly. A run reported without
    spans replays from what the run itself recorded: a Prompt step and a
    Response step with a null `span_id`, and `steps_from_trace` set.
    """
    return await service.get_replay(session, principal, run_id)


@router.post("/{run_id}/flag", response_model=RunFlagResult, summary="Flag a run for review")
async def flag_run(
    principal: CurrentPrincipal,
    session: Db,
    run_id: str,
    payload: RunFlagRequest,
    request: Request,
) -> RunFlagResult:
    """Route a run to the review queue.

    The flag is written as a feedback score on the run itself — where reviewers
    look — and, when a reason is given, as a reviewer comment on the same run.
    The action is recorded in the audit trail. Requires at least the member
    role.
    """
    return await service.flag_run(session, principal, run_id, payload, request=request)
