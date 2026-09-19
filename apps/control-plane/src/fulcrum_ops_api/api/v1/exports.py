"""Exports routes: request a file, watch it being produced, download it.

An export is asynchronous by design. ``POST /exports`` validates the request,
writes a ``Queued`` row and returns immediately; the file is generated in a
background task that opens its own session once this request has committed. The
console then polls ``GET /exports/{id}/status`` until the job reports
``is_downloadable`` and enables its Download button.

``GET /exports/{id}/download`` streams the produced file straight off the spool
in chunks, so a large extract never lands in memory whole, and it carries the
real media type and filename for the format that was generated.

The recurring side of the screen is ``/exports/schedules``: full CRUD plus a
run-now that queues one firing immediately. The platform's own sweeper calls
``services.exports.run_due_schedules`` in-process rather than over HTTP —
deliberately, because that function fires every tenant's due schedules and no
request-scoped principal may reach across workspaces.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.operations import ExportFormat, ExportJob, ExportStatus
from ...schemas.exports import (
    ExportDatasetRead,
    ExportJobCreate,
    ExportJobRead,
    ExportJobUpdate,
    ExportScheduleCreate,
    ExportScheduleRead,
    ExportScheduleUpdate,
    ExportsSummary,
)
from ...services import exports as service
from ..common import (
    MAX_PAGE_SIZE,
    ActionResult,
    ListParams,
    Page,
    list_params,
    to_csv,
)
from ..deps import CurrentPrincipal, Db, Principal

router = APIRouter(prefix="/exports", tags=["Exports"])

ListQuery = Annotated[ListParams, Depends(list_params)]

#: Hard ceiling on the jobs CSV, mirroring the ceiling the other screens use.
MAX_EXPORT_ROWS = 10_000

#: Column order of the export-history CSV, matching the on-screen table.
JOB_CSV_COLUMNS: list[tuple[str, str]] = [
    ("export_ref", "Export ID"),
    ("name", "Export"),
    ("source_screen", "Source Screen"),
    ("export_format", "Format"),
    ("row_count", "Rows"),
    ("size_bytes", "Size (bytes)"),
    ("requested_by", "Requested By"),
    ("requested_at", "Created"),
    ("completed_at", "Completed"),
    ("status", "Status"),
    ("download_count", "Downloads"),
    ("expires_at", "Expires"),
    ("error", "Error"),
]

#: What the console prints in the Requested By column for a scheduled firing.
SYSTEM_REQUESTER = "System (Scheduled)"

StatusFilter = Annotated[
    list[ExportStatus] | None,
    Query(alias="status", description="Repeatable: status=Ready&status=Failed"),
]
FormatFilter = Annotated[
    list[ExportFormat] | None,
    Query(alias="export_format", description="Repeatable: export_format=CSV&export_format=PDF"),
]
ScreenFilter = Annotated[
    list[str] | None,
    Query(alias="source_screen", description="Repeatable dataset name, e.g. 'Audit Trail'"),
]


def _safe_filename(filename: str) -> str:
    """Strip anything that could break out of the Content-Disposition header."""
    return "".join(char for char in filename if char.isprintable() and char not in '"\\;\r\n')


def _attachment(filename: str) -> str:
    return f'attachment; filename="{_safe_filename(filename)}"'


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d")


_Row = TypeVar("_Row")


async def _refreshed(session: Db, row: _Row) -> _Row:
    """Re-read the server-populated columns a write leaves unfetched.

    ``created_at`` and ``updated_at`` carry SQL defaults, so after the service's
    flush they are expired on the instance. Building a read model would then
    attempt IO from Pydantic's synchronous validation and fail; one refresh here
    means every mutation answers with the row as the database now holds it.
    """
    await session.refresh(row)
    return row


def _job_read(job: ExportJob) -> ExportJobRead:
    return ExportJobRead.model_validate(job)


async def _queue(
    session: Db,
    principal: Principal,
    payload: ExportJobCreate,
    background: BackgroundTasks,
    request: Request,
) -> ExportJobRead:
    """Create a job, make it durable, then schedule its generation.

    The generator opens its own session, so the row has to be committed before
    the task is allowed to look for it — background tasks run before the
    request-scoped session commits, and an uncommitted job is invisible to any
    other connection, which would leave it sitting Queued forever. Committing
    here also means a client holding a 202 is holding a promise the database has
    already accepted.
    """
    job = await service.create_export_job(session, principal, payload, request=request)
    await session.commit()
    background.add_task(service.run_export_job, job.id, principal.workspace_id)
    return _job_read(await _refreshed(session, job))


async def _collect_jobs(
    session: Db,
    principal: Principal,
    params: ListParams,
    *,
    job_status: list[str] | None,
    export_format: list[str] | None,
    source_screen: list[str] | None,
    requested_by_user_id: str | None,
) -> list[ExportJob]:
    """Walk the filtered job list to completion for the CSV export.

    The service pages, so the export walks those pages rather than growing a
    second query beside it. The walk stops at :data:`MAX_EXPORT_ROWS` so one
    click cannot pull an unbounded table.
    """
    collected: list[ExportJob] = []
    page = 1
    while len(collected) < MAX_EXPORT_ROWS:
        window = ListParams(
            page=page, page_size=MAX_PAGE_SIZE, q=params.q, sort=params.sort
        )
        rows, total = await service.list_export_jobs(
            session,
            principal,
            window,
            status=job_status,
            export_format=export_format,
            source_screen=source_screen,
            requested_by_user_id=requested_by_user_id,
        )
        collected.extend(rows)
        if not rows or len(collected) >= total:
            break
        page += 1
    return collected[:MAX_EXPORT_ROWS]


def _csv_rows(jobs: Sequence[ExportJob]) -> list[dict[str, Any]]:
    return [
        {
            "export_ref": job.export_ref,
            "name": job.name,
            "source_screen": job.source_screen,
            "export_format": job.export_format,
            "row_count": job.row_count,
            "size_bytes": job.size_bytes,
            "requested_by": job.requested_by_user_id or SYSTEM_REQUESTER,
            "requested_at": job.requested_at.isoformat() if job.requested_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "status": job.status,
            "download_count": job.download_count,
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "error": job.error,
        }
        for job in jobs
    ]


# --------------------------------------------------------------------------- #
# Fixed paths first: they would otherwise be swallowed by /{job_id}.
# --------------------------------------------------------------------------- #


@router.get("/summary", response_model=ExportsSummary, summary="Export KPI summary")
async def get_summary(principal: CurrentPrincipal, session: Db) -> ExportsSummary:
    """The four KPI cards — Exports (30d), Scheduled, Total Volume, Failed.

    Volume is returned as ``total_bytes_30d`` so the console formats it once,
    and the payload also carries the rows exported, the count currently
    downloadable, and the distinct formats and datasets that fill the two
    dropdowns. Every figure is a SQL aggregate over the 30-day window.
    """
    return await service.summary(session, principal)


@router.get(
    "/datasets",
    response_model=list[ExportDatasetRead],
    summary="List exportable datasets",
)
async def list_datasets(principal: CurrentPrincipal) -> list[ExportDatasetRead]:
    """What the New Export dialog's Dataset picker offers.

    Each entry names the columns that will be written and the filters that may
    be replayed against it, so the dialog can only build a job the generator can
    actually produce.
    """
    return service.available_datasets()


@router.get("/formats", response_model=list[str], summary="List supported formats")
async def list_formats(principal: CurrentPrincipal) -> list[str]:
    """Formats this deployment can produce, for the Format picker.

    Every renderer is standard-library only — CSV, JSON, XLSX and PDF — so the
    answer never depends on a document toolchain being installed on the box.
    """
    return service.supported_formats()


@router.get(
    "/export",
    summary="Export the export history as CSV",
    response_class=StreamingResponse,
)
async def export_jobs_csv(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    job_status: StatusFilter = None,
    export_format: FormatFilter = None,
    source_screen: ScreenFilter = None,
    requested_by_user_id: Annotated[
        str | None, Query(description="Only jobs this user requested")
    ] = None,
) -> StreamingResponse:
    """Stream the filtered job history as CSV, matching the grid on screen.

    This is the table's own Export button and is served synchronously; it is not
    a job. Use ``POST /exports`` to generate a file from one of the platform
    datasets instead.
    """
    jobs = await _collect_jobs(
        session,
        principal,
        params,
        job_status=[item.value for item in job_status] if job_status else None,
        export_format=[item.value for item in export_format] if export_format else None,
        source_screen=[item for item in source_screen if item] if source_screen else None,
        requested_by_user_id=requested_by_user_id,
    )
    return StreamingResponse(
        iter([to_csv(_csv_rows(jobs), JOB_CSV_COLUMNS)]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _attachment(f"export-jobs-{_stamp()}.csv")},
    )


# --------------------------------------------------------------------------- #
# Schedules — declared before /{job_id} so "schedules" is never read as an id.
# --------------------------------------------------------------------------- #


@router.get(
    "/schedules",
    response_model=Page[ExportScheduleRead],
    summary="List export schedules",
)
async def list_schedules(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    enabled: Annotated[bool | None, Query(description="Only enabled or only paused")] = None,
    source_screen: ScreenFilter = None,
    export_format: FormatFilter = None,
) -> Page[ExportScheduleRead]:
    """One page of recurring exports, ordered by name by default.

    Free-text search covers the schedule name, its dataset and its cron
    expression.
    """
    rows, total = await service.list_schedules(
        session,
        principal,
        params,
        enabled=enabled,
        source_screen=[item for item in source_screen if item] if source_screen else None,
        export_format=[item.value for item in export_format] if export_format else None,
    )
    return Page.build(
        [ExportScheduleRead.model_validate(row) for row in rows],
        total,
        params.page,
        params.page_size,
    )


@router.post(
    "/schedules",
    response_model=ExportScheduleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an export schedule",
)
async def create_schedule(
    principal: CurrentPrincipal,
    session: Db,
    payload: ExportScheduleCreate,
    request: Request,
) -> ExportScheduleRead:
    """Turn an export into a recurring one — the "Schedule Weekly" action.

    The cron expression is evaluated in UTC and the first firing is computed on
    creation, so the schedule appears with a real ``next_run_at`` rather than a
    placeholder. Schedule names are unique within a workspace. Requires the
    operator role.
    """
    schedule = await service.create_schedule(session, principal, payload, request=request)
    return ExportScheduleRead.model_validate(await _refreshed(session, schedule))


@router.get(
    "/schedules/{schedule_id}",
    response_model=ExportScheduleRead,
    summary="Get an export schedule",
)
async def get_schedule(
    principal: CurrentPrincipal, session: Db, schedule_id: str
) -> ExportScheduleRead:
    """One schedule. A schedule in another workspace answers 404, not 403."""
    schedule = await service.get_schedule(session, principal, schedule_id)
    return ExportScheduleRead.model_validate(schedule)


@router.patch(
    "/schedules/{schedule_id}",
    response_model=ExportScheduleRead,
    summary="Update an export schedule",
)
async def update_schedule(
    principal: CurrentPrincipal,
    session: Db,
    schedule_id: str,
    payload: ExportScheduleUpdate,
    request: Request,
) -> ExportScheduleRead:
    """Partially update a schedule.

    Anything that moves the cadence re-derives ``next_run_at``, and pausing one
    clears it entirely, so the sweeper can never fire on a stale timestamp.
    Requires the operator role.
    """
    schedule = await service.update_schedule(
        session, principal, schedule_id, payload, request=request
    )
    return ExportScheduleRead.model_validate(await _refreshed(session, schedule))


@router.delete(
    "/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an export schedule",
)
async def delete_schedule(
    principal: CurrentPrincipal, session: Db, schedule_id: str, request: Request
) -> None:
    """Stop a recurring export. Files it already produced are kept.

    Requires the operator role.
    """
    await service.delete_schedule(session, principal, schedule_id, request=request)


@router.post(
    "/schedules/{schedule_id}/run",
    response_model=ExportJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run an export schedule now",
)
async def run_schedule_now(
    principal: CurrentPrincipal,
    session: Db,
    schedule_id: str,
    background: BackgroundTasks,
    request: Request,
) -> ExportJobRead:
    """Fire one occurrence of a schedule immediately, off-cadence.

    The job is queued from the schedule's own dataset, format and filters and
    generated in the background exactly as a timed firing would be; the
    schedule's ``next_run_at`` is left untouched, so this does not skip or
    disturb the cadence. Poll ``GET /exports/{id}/status`` for the result.
    """
    schedule = await service.get_schedule(session, principal, schedule_id)
    payload = ExportJobCreate(
        # The same naming the sweeper uses, so a manual firing and a timed one
        # read alike in the table and both fit the column.
        name=service.dated_name(schedule.name),
        source_screen=schedule.source_screen,
        export_format=ExportFormat(schedule.export_format),
        filters=dict(schedule.filters or {}),
    )
    return await _queue(session, principal, payload, background, request)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


@router.get("", response_model=Page[ExportJobRead], summary="List exports")
async def list_exports(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    job_status: StatusFilter = None,
    export_format: FormatFilter = None,
    source_screen: ScreenFilter = None,
    requested_by_user_id: Annotated[
        str | None, Query(description="Only jobs this user requested")
    ] = None,
) -> Page[ExportJobRead]:
    """One page of the export history, newest first.

    Free-text search covers the export name, its dataset and its reference.
    Each row carries ``is_downloadable``, which is what the Download row action
    keys off, and ``is_expired`` once retention has lapsed.
    """
    rows, total = await service.list_export_jobs(
        session,
        principal,
        params,
        status=[item.value for item in job_status] if job_status else None,
        export_format=[item.value for item in export_format] if export_format else None,
        source_screen=[item for item in source_screen if item] if source_screen else None,
        requested_by_user_id=requested_by_user_id,
    )
    return Page.build(
        [_job_read(row) for row in rows], total, params.page, params.page_size
    )


@router.post(
    "",
    response_model=ExportJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request an export",
)
async def create_export(
    principal: CurrentPrincipal,
    session: Db,
    payload: ExportJobCreate,
    background: BackgroundTasks,
    request: Request,
) -> ExportJobRead:
    """Queue an export — the New Export dialog.

    Answers 202 with a ``Queued`` job; generation starts once this request has
    committed. An unknown dataset, an unsupported format or a filter the dataset
    does not accept is rejected here rather than surfacing minutes later as a
    failed job. Poll ``GET /exports/{id}/status`` and download when it reports
    ``is_downloadable``.
    """
    return await _queue(session, principal, payload, background, request)


@router.get("/{job_id}", response_model=ExportJobRead, summary="Get an export")
async def get_export(
    principal: CurrentPrincipal, session: Db, job_id: str
) -> ExportJobRead:
    """One export job. A job in another workspace answers 404, not 403."""
    job = await service.get_export_job(session, principal, job_id)
    return _job_read(job)


@router.get(
    "/{job_id}/status",
    response_model=ExportJobRead,
    summary="Poll an export's progress",
)
async def get_export_status(
    principal: CurrentPrincipal, session: Db, job_id: str
) -> ExportJobRead:
    """The job's live state, for the console's poll loop.

    Progress is reported as what actually happened, not a synthetic percentage:
    ``status`` walks Queued to Generating to Ready or Failed, ``row_count`` and
    ``size_bytes`` are filled in the moment the file is written, ``error``
    carries the reason for a failure, and ``is_downloadable`` says whether the
    download endpoint will serve it right now.
    """
    job = await service.get_export_job(session, principal, job_id)
    return _job_read(job)


@router.patch("/{job_id}", response_model=ExportJobRead, summary="Update an export")
async def update_export(
    principal: CurrentPrincipal,
    session: Db,
    job_id: str,
    payload: ExportJobUpdate,
    request: Request,
) -> ExportJobRead:
    """Rename a job, or correct its filters while it is still queued.

    Once a file exists its filters are frozen: they are the record of what was
    generated, and changing them would make the stored export undefendable in an
    audit. Attempting it answers 409.
    """
    job = await service.update_export_job(session, principal, job_id, payload, request=request)
    return _job_read(await _refreshed(session, job))


@router.delete(
    "/{job_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an export"
)
async def delete_export(
    principal: CurrentPrincipal, session: Db, job_id: str, request: Request
) -> None:
    """Delete a job and the file it generated.

    The spooled file is removed with the row, so a deleted export cannot be
    downloaded afterwards. Requires the operator role.
    """
    await service.delete_export_job(session, principal, job_id, request=request)


@router.post(
    "/{job_id}/rerun",
    response_model=ExportJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run an export",
)
async def rerun_export(
    principal: CurrentPrincipal,
    session: Db,
    job_id: str,
    background: BackgroundTasks,
    request: Request,
) -> ExportJobRead:
    """The "Re-run Export" row action.

    Queues a fresh job against the same dataset, format and filters, so the
    original file and its download history stay intact and the two extracts can
    be compared. Answers 202 with the new job.
    """
    original = await service.get_export_job(session, principal, job_id)
    payload = ExportJobCreate(
        name=original.name,
        source_screen=original.source_screen,
        export_format=ExportFormat(original.export_format),
        filters=dict(original.filters or {}),
    )
    return await _queue(session, principal, payload, background, request)


@router.get(
    "/{job_id}/download",
    summary="Download a generated export",
    response_class=StreamingResponse,
)
async def download_export(
    principal: CurrentPrincipal, session: Db, job_id: str, request: Request
) -> StreamingResponse:
    """Stream the produced file with its real media type and filename.

    The body is read off the spool in chunks, so a large extract never lands in
    memory whole. A job still generating answers 412, one that failed answers
    409 with the reason, and one whose retention window has closed answers 404
    after its file is dropped. Each successful download is counted and audited.
    """
    download = await service.open_download(session, principal, job_id, request=request)
    return StreamingResponse(
        service.iter_file(download.path),
        media_type=download.media_type,
        headers={
            "Content-Disposition": _attachment(download.filename),
            "Content-Length": str(download.size_bytes),
        },
    )


@router.get(
    "/{job_id}/retention",
    response_model=ActionResult,
    summary="Retention policy for a generated export",
)
async def get_retention(
    principal: CurrentPrincipal, session: Db, job_id: str
) -> ActionResult:
    """When this file stops being downloadable, and the policy behind that.

    Retention is a deployment setting rather than a per-job one, so the console
    can explain an expired download without guessing at the window.
    """
    job = await service.get_export_job(session, principal, job_id)
    return ActionResult(
        message=(
            f"Export {job.export_ref} is retained for {service.retention_days()} day(s)."
        ),
        entity_id=job.id,
        data={
            "retention_days": service.retention_days(),
            "max_rows": service.max_rows(),
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "status": job.status,
        },
    )
