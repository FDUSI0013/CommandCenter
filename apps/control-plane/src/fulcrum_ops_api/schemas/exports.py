"""Wire contracts for the Exports screen.

An export is a request ("give me this dataset, in this shape") that becomes a
file. The request is validated here; the file is produced by
``services.exports``. Two entities appear on the screen: the jobs that have run
and the schedules that keep running them.

``storage_key`` is intentionally absent from :class:`ExportJobRead` — it is a
server-side path, and the client never needs it because the download endpoint
resolves it. Clients get ``is_downloadable`` instead.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any

from croniter import CroniterBadDateError, croniter
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
)

from ..models.operations import ExportFormat, ExportStatus

ExportName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ScreenName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
CronExpr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Recipient = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]

MAX_FILTER_BYTES = 16 * 1024


def _check_filters(value: dict[str, Any]) -> dict[str, Any]:
    """Filters are replayed against the database, so keep them small and flat."""
    encoded = len(json.dumps(value, default=str).encode("utf-8"))
    if encoded > MAX_FILTER_BYTES:
        raise ValueError(f"filters must serialise to at most {MAX_FILTER_BYTES} bytes.")
    for key in value:
        if not isinstance(key, str) or not key:
            raise ValueError("filter keys must be non-empty strings.")
    return value


def _normalise_cron(value: str) -> str:
    """Collapse whitespace and reject anything the platform clock could not honour.

    ``croniter.is_valid`` only checks that each field is in range, and two kinds
    of expression pass it that the sweeper cannot live with:

    * A sixth (seconds) or seventh (year) field. ``* * * * * *`` is due again on
      every tick of a 30-second clock, which is a file on the spool volume every
      30 seconds for as long as the schedule exists. The column is documented as
      five fields, so that is what is accepted -- an ``@daily`` style alias is
      five fields by another name and still is.
    * A date that does not exist. ``0 0 31 4 *`` is in range field by field and
      has no next firing; asking for one raises, which used to reach the client
      as a 500 after the editor had already closed and thrown the form away.
    """
    expression = " ".join(value.split())
    if not croniter.is_valid(expression):
        raise ValueError(
            "cron must be a valid expression, evaluated in UTC — for example "
            "'0 6 * * 1' for every Monday at 06:00."
        )
    if len(croniter.expand(expression)[0]) != 5:
        raise ValueError(
            "cron must have exactly five fields — minute, hour, day of month, month, "
            "day of week — for example '0 6 * * 1'. Seconds and years are not supported."
        )
    try:
        croniter(expression, dt.datetime.now(dt.UTC)).get_next(dt.datetime)
    except CroniterBadDateError as exc:
        raise ValueError(
            f"cron '{expression}' never fires: no date matches it. Check the day "
            "of the month against the month."
        ) from exc
    return expression


def _dedupe(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


# --------------------------------------------------------------------------- #
# Export jobs
# --------------------------------------------------------------------------- #


class ExportJobRead(BaseModel):
    """One generated extract, as the Exports table renders it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    export_ref: str
    name: str
    source_screen: str
    export_format: ExportFormat
    status: ExportStatus
    filters: dict[str, Any] = Field(default_factory=dict)
    row_count: int | None = None
    size_bytes: int | None = None
    requested_by_user_id: str | None = None
    requested_at: dt.datetime
    completed_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    download_count: int
    error: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        """True once retention has lapsed, whether or not the reaper has run."""
        if self.status is ExportStatus.EXPIRED:
            return True
        if self.expires_at is None:
            return False
        return self.expires_at <= dt.datetime.now(dt.UTC)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_downloadable(self) -> bool:
        """Whether the download endpoint will serve this row right now."""
        return self.status is ExportStatus.READY and not self.is_expired


class ExportJobCreate(BaseModel):
    """Request a new extract.

    ``source_screen`` must name a dataset the server can actually read;
    the service rejects anything else and lists what is available, so the
    console never queues a job that cannot produce a file.
    """

    name: ExportName
    source_screen: ScreenName
    export_format: ExportFormat = ExportFormat.CSV
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Column filters replayed against the dataset, plus an optional "
        "'days' window, e.g. {'status': 'Open', 'days': 30}. The window applies "
        "only to datasets that report windowed=true; an inventory dataset exports "
        "every record and drops 'days' from the filters it records.",
    )

    @field_validator("filters")
    @classmethod
    def _filters_sane(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_filters(value)


class ExportJobUpdate(BaseModel):
    """Rename a job, or correct its filters before it has produced a file."""

    name: ExportName | None = None
    filters: dict[str, Any] | None = None

    @field_validator("filters")
    @classmethod
    def _filters_sane(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _check_filters(value)


class ExportDatasetRead(BaseModel):
    """A dataset the exporter can generate, for the console's dataset picker."""

    source_screen: str
    columns: list[str]
    filterable: list[str]
    windowed: bool = Field(
        True,
        description="Whether the 'days' window applies. False for an inventory "
        "(agents, secrets, budgets, quotas, alert rules), which always exports "
        "every record: the picker should hide its time range for these.",
    )
    time_label: str | None = Field(
        None,
        description="The column the 'days' window measures, e.g. 'Raised At'. "
        "Null when the dataset is not windowed.",
    )


# --------------------------------------------------------------------------- #
# Export schedules
# --------------------------------------------------------------------------- #


class ExportScheduleRead(BaseModel):
    """A recurring export. Each firing creates its own job row."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    name: str
    source_screen: str
    export_format: ExportFormat
    cron: str
    filters: dict[str, Any] = Field(default_factory=dict)
    recipients: list[str] = Field(default_factory=list)
    enabled: bool
    last_run_at: dt.datetime | None = None
    next_run_at: dt.datetime | None = None
    owner_user_id: str | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ExportScheduleCreate(BaseModel):
    """Create a recurring export. The cron expression is evaluated in UTC."""

    name: ExportName
    source_screen: ScreenName
    cron: CronExpr
    export_format: ExportFormat = ExportFormat.CSV
    filters: dict[str, Any] = Field(default_factory=dict)
    recipients: list[Recipient] = Field(
        default_factory=list,
        max_length=50,
        description="Delivery targets. Refused with 422 unless the summary reports "
        "delivery_available, because nothing would be sent to them.",
    )
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron_valid(cls, value: str) -> str:
        return _normalise_cron(value)

    @field_validator("filters")
    @classmethod
    def _filters_sane(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_filters(value)

    @field_validator("recipients")
    @classmethod
    def _unique_recipients(cls, value: list[str]) -> list[str]:
        return _dedupe(value)


class ExportScheduleUpdate(BaseModel):
    """Partial edit of a schedule; every field is optional."""

    name: ExportName | None = None
    source_screen: ScreenName | None = None
    cron: CronExpr | None = None
    export_format: ExportFormat | None = None
    filters: dict[str, Any] | None = None
    recipients: list[Recipient] | None = Field(None, max_length=50)
    enabled: bool | None = None

    @field_validator("cron")
    @classmethod
    def _cron_valid(cls, value: str | None) -> str | None:
        return None if value is None else _normalise_cron(value)

    @field_validator("filters")
    @classmethod
    def _filters_sane(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _check_filters(value)

    @field_validator("recipients")
    @classmethod
    def _unique_recipients(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _dedupe(value)


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


class ExportsSummary(BaseModel):
    """The KPI row above the Exports table, aggregated over a 30-day window."""

    exports_30d: int = Field(description="Jobs requested in the last 30 days.")
    scheduled: int = Field(description="Enabled recurring exports.")
    total_bytes_30d: int = Field(description="Bytes generated in the last 30 days.")
    total_rows_30d: int = Field(description="Rows exported in the last 30 days.")
    failed_30d: int = Field(description="Jobs that failed in the last 30 days.")
    ready: int = Field(description="Jobs currently downloadable.")
    formats: list[str] = Field(
        default_factory=list, description="Formats used in the window, for the Format filter."
    )
    source_screens: list[str] = Field(
        default_factory=list,
        description="Distinct datasets exported, for the Source Screen filter.",
    )
    datasets: list[ExportDatasetRead] = Field(
        default_factory=list,
        description="Every exportable dataset, so one summary call can seed the picker.",
    )
    delivery_available: bool = Field(
        False,
        description="Whether a schedule's recipients are actually sent anything. "
        "False on this build: there is no mail or notification transport, a "
        "non-empty 'recipients' is refused with 422, and the console should not "
        "offer the input.",
    )
