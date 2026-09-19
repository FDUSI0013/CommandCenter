"""Export generation: real files, written to a spool directory, reaped after a week.

An export job is a promise that becomes a file. Creating one returns
immediately with a ``Queued`` row; generation happens in a background task that
opens its **own** session — the request's session is committed and closed before
background work runs, so it must not be captured.

What can be exported is a closed set. ``_DATASETS`` maps each ``source_screen``
the console offers onto a real table, the columns that leave the building, and
the filters that may be replayed against it. A job naming anything else is
rejected at creation with the list of what is available, so the queue never
holds work that cannot produce a file. That registry is also the security
boundary: the secrets dataset exports rotation and ownership metadata and
deliberately carries neither ``ciphertext`` nor ``vault_reference``.

Files are produced with the standard library only — CSV, JSON, a minimal but
valid XLSX package, and a paginated PDF — so an export never depends on a
document toolchain being installed on the box.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import zipfile
from collections.abc import Iterator, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any

from croniter import croniter
from fastapi import Request
from sqlalchemy import Select, case, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.common import ListParams, apply_filters, apply_search, apply_sort, paginate, to_csv
from ..api.deps import Principal
from ..core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from ..db.base import new_id
from ..db.session import get_sessionmaker
from ..models.governance import ApprovalRequest, AuditEvent, PolicyViolation, Secret
from ..models.identity import Role
from ..models.operations import (
    Alert,
    AlertRule,
    Budget,
    CapacityRecord,
    Deployment,
    ExportFormat,
    ExportJob,
    ExportSchedule,
    ExportStatus,
    Quota,
)
from ..models.registry import Agent
from ..schemas.exports import (
    ExportDatasetRead,
    ExportJobCreate,
    ExportJobUpdate,
    ExportScheduleCreate,
    ExportScheduleUpdate,
    ExportsSummary,
)
from . import audit

log = logging.getLogger("fulcrum_ops.exports")

SCREEN = "Exports"

#: Window every figure on the Exports KPI row is measured over.
SUMMARY_WINDOW_DAYS = 30

_DEFAULT_SPOOL_DIR = "./var/exports"
_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_MAX_ROWS = 50_000


def _env_int(name: str, default: int) -> int:
    """Read a positive integer setting, falling back rather than failing to boot."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else default


def spool_root() -> Path:
    """Directory generated files are written to.

    Configurable with ``FULCRUM_OPS_EXPORT_SPOOL_DIR``; read on each call so a
    deployment (or a test) can point it somewhere else without a restart.
    """
    return Path(os.getenv("FULCRUM_OPS_EXPORT_SPOOL_DIR", _DEFAULT_SPOOL_DIR))


def retention_days() -> int:
    """How long a generated file stays downloadable."""
    return _env_int("FULCRUM_OPS_EXPORT_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS)


def max_rows() -> int:
    """Ceiling on one export, so a single click cannot pull an unbounded table."""
    return _env_int("FULCRUM_OPS_EXPORT_MAX_ROWS", _DEFAULT_MAX_ROWS)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --------------------------------------------------------------------------- #
# Dataset registry
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ExportDataset:
    """One exportable view: which table, which columns, which filters."""

    model: type[Any]
    #: (attribute, column header) in display order. Anything absent never leaves.
    columns: tuple[tuple[str, str], ...]
    #: Attribute rows are ordered by, newest first. Must be non-nullable.
    order_by: str
    #: Attribute the optional ``days`` window filter applies to.
    time_column: str
    #: Attributes a job's ``filters`` document may constrain.
    filterable: tuple[str, ...]
    #: Whether the ``days`` window means anything here. It does for a log of
    #: things that happened -- alerts raised, approvals requested, audit events.
    #: It does not for an inventory, where the only date to window on is the
    #: day the record was created: "Secrets Compliance, last 30 days" silently
    #: left out every older secret, which is where an overdue rotation lives.
    windowed: bool = True

    @property
    def time_label(self) -> str | None:
        """What the window measures, in the words of the file's own header."""
        if not self.windowed:
            return None
        headers = dict(self.columns)
        return headers.get(self.time_column) or self.time_column.replace("_", " ").title()


_DATASETS: dict[str, ExportDataset] = {
    "Alerts": ExportDataset(
        model=Alert,
        columns=(
            ("alert_ref", "Alert ID"),
            ("severity", "Severity"),
            ("title", "Alert"),
            ("description", "Description"),
            ("source", "Source"),
            ("status", "Status"),
            ("raised_at", "Raised At"),
            ("acknowledged_at", "Acknowledged At"),
            ("resolved_at", "Resolved At"),
            ("assigned_to_user_id", "Assigned To"),
            ("occurrence_count", "Occurrences"),
            ("mttr_seconds", "MTTR (seconds)"),
        ),
        order_by="raised_at",
        time_column="raised_at",
        filterable=("severity", "status", "source", "assigned_to_user_id"),
    ),
    "Alert Rules": ExportDataset(
        model=AlertRule,
        columns=(
            ("name", "Rule"),
            ("description", "Description"),
            ("source", "Source"),
            ("severity", "Severity"),
            ("enabled", "Enabled"),
            ("notify_channels", "Channels"),
            ("throttle_minutes", "Throttle (minutes)"),
            ("created_at", "Created At"),
        ),
        order_by="created_at",
        time_column="created_at",
        filterable=("source", "severity", "enabled"),
        windowed=False,
    ),
    "Agent Registry": ExportDataset(
        model=Agent,
        columns=(
            ("name", "Agent"),
            ("slug", "Slug"),
            ("platform", "Platform"),
            ("agent_type", "Type"),
            ("environment", "Environment"),
            ("status", "Status"),
            ("risk", "Risk"),
            ("policy_status", "Policy Status"),
            ("owner_user_id", "Owner"),
            ("team", "Team"),
            ("model", "Model"),
            ("prompt_version", "Prompt Version"),
            ("tools_enabled", "Tools"),
            ("policies_applied", "Policies"),
            ("health", "Health"),
            ("last_used_at", "Last Used"),
        ),
        order_by="created_at",
        time_column="created_at",
        filterable=("status", "environment", "risk", "platform", "team", "policy_status"),
        windowed=False,
    ),
    "Deployments": ExportDataset(
        model=Deployment,
        columns=(
            ("deployment_ref", "Deployment"),
            ("agent_id", "Agent"),
            ("environment_id", "Environment"),
            ("version", "Version"),
            ("strategy", "Strategy"),
            ("status", "Status"),
            ("triggered_by_user_id", "Triggered By"),
            ("started_at", "Started At"),
            ("finished_at", "Finished At"),
            ("duration_seconds", "Duration (seconds)"),
            ("commit_ref", "Commit"),
            ("health", "Health"),
        ),
        order_by="created_at",
        time_column="created_at",
        filterable=("status", "strategy", "environment_id", "agent_id"),
    ),
    "Budgets": ExportDataset(
        model=Budget,
        columns=(
            ("name", "Budget"),
            ("scope", "Scope"),
            ("scope_ref", "Scope Target"),
            ("period", "Period"),
            ("amount_usd", "Amount (USD)"),
            ("spent_usd", "Spent (USD)"),
            ("currency", "Currency"),
            ("period_start", "Period Start"),
            ("period_end", "Period End"),
            ("owner_user_id", "Owner"),
            ("status", "Status"),
        ),
        order_by="created_at",
        time_column="created_at",
        filterable=("scope", "status", "period", "owner_user_id"),
        windowed=False,
    ),
    "Quotas": ExportDataset(
        model=Quota,
        columns=(
            ("name", "Quota"),
            ("resource", "Resource"),
            ("scope", "Scope"),
            ("scope_ref", "Scope Target"),
            ("limit_value", "Limit"),
            ("used_value", "Used"),
            ("unit", "Unit"),
            ("period", "Period"),
            ("enforcement", "Enforcement"),
            ("status", "Status"),
            ("resets_at", "Resets At"),
        ),
        order_by="created_at",
        time_column="created_at",
        filterable=("resource", "scope", "status", "enforcement"),
        windowed=False,
    ),
    "Capacity": ExportDataset(
        model=CapacityRecord,
        columns=(
            ("name", "Resource"),
            ("resource_type", "Type"),
            ("region", "Region"),
            ("provisioned", "Provisioned"),
            ("used", "Used"),
            ("unit", "Unit"),
            ("utilization_percent", "Utilisation %"),
            ("status", "Status"),
            ("measured_at", "Measured At"),
        ),
        order_by="measured_at",
        time_column="measured_at",
        filterable=("resource_type", "region", "status"),
    ),
    "Approvals": ExportDataset(
        model=ApprovalRequest,
        columns=(
            ("request_ref", "Request"),
            ("action", "Action"),
            ("action_detail", "Detail"),
            ("resource", "Resource"),
            ("agent_id", "Agent"),
            ("risk", "Risk"),
            ("status", "Status"),
            ("requested_by_user_id", "Requested By"),
            ("requested_at", "Requested At"),
            ("sla_due_at", "SLA Due"),
            ("decided_by_user_id", "Decided By"),
            ("decided_at", "Decided At"),
        ),
        order_by="requested_at",
        time_column="requested_at",
        filterable=("status", "risk", "agent_id", "requested_by_user_id"),
    ),
    "Audit Trail": ExportDataset(
        model=AuditEvent,
        columns=(
            ("occurred_at", "Occurred At"),
            ("actor", "Actor"),
            ("action", "Action"),
            ("entity_type", "Entity Type"),
            ("entity_label", "Entity"),
            ("entity_id", "Entity ID"),
            ("source_screen", "Screen"),
            ("detail", "Detail"),
            ("ip_address", "IP Address"),
            ("checksum", "Checksum"),
        ),
        order_by="occurred_at",
        time_column="occurred_at",
        filterable=("action", "entity_type", "actor", "source_screen"),
    ),
    "Policy Violations": ExportDataset(
        model=PolicyViolation,
        columns=(
            ("occurred_at", "Occurred At"),
            ("policy_id", "Policy"),
            ("agent_id", "Agent"),
            ("severity", "Severity"),
            ("action_taken", "Action Taken"),
            ("trace_id", "Trace"),
            ("resolved_at", "Resolved At"),
        ),
        order_by="occurred_at",
        time_column="occurred_at",
        filterable=("severity", "action_taken", "policy_id", "agent_id"),
    ),
    # Compliance posture only. The ciphertext and the vault reference are the
    # two things this table exists to protect, and neither is a column here.
    "Secrets Compliance": ExportDataset(
        model=Secret,
        columns=(
            ("name", "Secret"),
            ("secret_type", "Type"),
            ("vault", "Vault"),
            ("environment", "Environment"),
            ("status", "Status"),
            ("risk", "Risk"),
            ("privileged", "Privileged"),
            ("rotation_period_days", "Rotation Period (days)"),
            ("last_rotated_at", "Last Rotated"),
            ("next_rotation_at", "Next Rotation"),
            ("expires_at", "Expires At"),
            ("owner_user_id", "Owner"),
        ),
        order_by="created_at",
        time_column="created_at",
        filterable=("status", "vault", "environment", "risk", "secret_type"),
        windowed=False,
    ),
    "Exports": ExportDataset(
        model=ExportJob,
        columns=(
            ("export_ref", "Export"),
            ("name", "Name"),
            ("source_screen", "Dataset"),
            ("export_format", "Format"),
            ("status", "Status"),
            ("row_count", "Rows"),
            ("size_bytes", "Size (bytes)"),
            ("requested_by_user_id", "Requested By"),
            ("requested_at", "Requested At"),
            ("completed_at", "Completed At"),
            ("download_count", "Downloads"),
        ),
        order_by="requested_at",
        time_column="requested_at",
        filterable=("status", "export_format", "source_screen", "requested_by_user_id"),
    ),
}

#: The window filter a dataset of events accepts on top of its own columns. An
#: inventory dataset (``windowed=False``) tolerates it on the way in, because
#: the console sends one with every request, and never applies or records it.
WINDOW_FILTER = "days"


def available_datasets() -> list[ExportDatasetRead]:
    """Describe every exportable dataset, for the console's dataset picker."""
    return [
        ExportDatasetRead(
            source_screen=name,
            columns=[header for _, header in dataset.columns],
            filterable=list(dataset.filterable),
            windowed=dataset.windowed,
            time_label=dataset.time_label,
        )
        for name, dataset in sorted(_DATASETS.items())
    ]


def _resolve_dataset(source_screen: str, filters: dict[str, Any]) -> ExportDataset:
    """Validate a job's target and filters before anything is queued."""
    dataset = _DATASETS.get(source_screen)
    if dataset is None:
        raise ValidationFailed(
            f"'{source_screen}' is not an exportable dataset.",
            details={"field": "source_screen", "available": sorted(_DATASETS)},
        )
    allowed = set(dataset.filterable) | {WINDOW_FILTER}
    unknown = sorted(set(filters) - allowed)
    if unknown:
        raise ValidationFailed(
            f"{source_screen} cannot be filtered by {', '.join(unknown)}.",
            details={"field": "filters", "filterable": sorted(allowed)},
        )
    window = filters.get(WINDOW_FILTER)
    if window is not None:
        try:
            days = int(window)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(
                "The 'days' filter must be a whole number of days.",
                details={"field": "filters.days"},
            ) from exc
        if days < 1 or days > 3650:
            raise ValidationFailed(
                "The 'days' filter must be between 1 and 3650.",
                details={"field": "filters.days"},
            )
    return dataset


def _recorded_filters(dataset: ExportDataset, filters: dict[str, Any]) -> dict[str, Any]:
    """The filters as they will actually be applied, which is what a row stores.

    A job's ``filters`` are the record of what produced its file. An inventory
    dataset ignores the window, so the window is left off its record too: a row
    reading ``{"days": 30}`` beside a file holding every secret the workspace
    owns would be a record nobody could defend.
    """
    recorded = dict(filters)
    if not dataset.windowed:
        recorded.pop(WINDOW_FILTER, None)
    return recorded


def _dataset_query(dataset: ExportDataset, workspace_id: str, filters: dict[str, Any]) -> Select:
    model = dataset.model
    stmt = select(model).where(model.workspace_id == workspace_id)
    stmt = apply_filters(
        stmt,
        {
            getattr(model, key): filters[key]
            for key in dataset.filterable
            if key in filters and filters[key] not in (None, "")
        },
    )
    # Rows queued or scheduled before the window stopped applying to inventory
    # datasets still carry one, so it is ignored here as well as at the door.
    window = filters.get(WINDOW_FILTER) if dataset.windowed else None
    if window is not None:
        cutoff = _now() - dt.timedelta(days=int(window))
        stmt = stmt.where(getattr(model, dataset.time_column) >= cutoff)
    return stmt.order_by(getattr(model, dataset.order_by).desc(), model.id.desc())


def _cell(value: Any) -> Any:
    """Normalise one attribute into something every renderer can write."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


async def _fetch_rows(
    session: AsyncSession, dataset: ExportDataset, workspace_id: str, filters: dict[str, Any]
) -> list[dict[str, Any]]:
    stmt = _dataset_query(dataset, workspace_id, filters).limit(max_rows())
    rows = (await session.execute(stmt)).scalars().all()
    return [{key: _cell(getattr(row, key)) for key, _ in dataset.columns} for row in rows]


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #

MEDIA_TYPES: dict[str, str] = {
    ExportFormat.CSV.value: "text/csv; charset=utf-8",
    ExportFormat.JSON.value: "application/json",
    ExportFormat.XLSX.value: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    ExportFormat.PDF.value: "application/pdf",
}

FILE_EXTENSIONS: dict[str, str] = {
    ExportFormat.CSV.value: ".csv",
    ExportFormat.JSON.value: ".json",
    ExportFormat.XLSX.value: ".xlsx",
    ExportFormat.PDF.value: ".pdf",
}


def supported_formats() -> list[str]:
    """Formats this deployment can actually produce."""
    return list(FILE_EXTENSIONS)


def _render_csv(dataset: ExportDataset, rows: list[dict[str, Any]]) -> bytes:
    # utf-8-sig: without the BOM, Excel mis-reads non-ASCII on a double click.
    return to_csv(rows, dataset.columns).encode("utf-8-sig")


def _render_json(
    dataset: ExportDataset, rows: list[dict[str, Any]], title: str, source_screen: str
) -> bytes:
    document = {
        "export": title,
        "dataset": source_screen,
        "generated_at": _now().isoformat(),
        "row_count": len(rows),
        "columns": [{"key": key, "label": label} for key, label in dataset.columns],
        "rows": rows,
    }
    return json.dumps(document, indent=2, default=str).encode("utf-8")


_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_escape(value: str) -> str:
    cleaned = _XML_ILLEGAL.sub("", value)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _column_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _xlsx_cell(reference: str, value: Any) -> str:
    if value is None or value == "":
        return f'<c r="{reference}"/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="inlineStr"><is><t>{"Yes" if value else "No"}</t></is></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    text = _xml_escape(_as_text(value))
    return (
        f'<c r="{reference}" t="inlineStr">'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _sheet_name(value: str) -> str:
    # Excel rejects these characters and anything past 31 characters.
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", value).strip() or "Export"
    return cleaned[:31]


def _render_xlsx(dataset: ExportDataset, rows: list[dict[str, Any]], title: str) -> bytes:
    """Write a minimal, valid SpreadsheetML package with the standard library.

    Inline strings avoid a shared-string table, which keeps the package to five
    parts and the writer to one pass over the rows.
    """
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    header_cells = "".join(
        _xlsx_cell(f"{_column_letter(i)}1", label)
        for i, (_, label) in enumerate(dataset.columns, start=1)
    )
    lines.append(f'<row r="1">{header_cells}</row>')
    for row_index, row in enumerate(rows, start=2):
        cells = "".join(
            _xlsx_cell(f"{_column_letter(i)}{row_index}", row.get(key))
            for i, (key, _) in enumerate(dataset.columns, start=1)
        )
        lines.append(f'<row r="{row_index}">{cells}</row>')
    lines.extend(["</sheetData>", "</worksheet>"])
    sheet_xml = "".join(lines)

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xml_escape(_sheet_name(title))}" sheetId="1" r:id="rId1"/>'
        "</sheets></workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", root_rels)
        package.writestr("xl/workbook.xml", workbook)
        package.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        package.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


# PDF page geometry: A4 landscape, Courier so columns line up without a metrics
# table. Courier advances exactly 0.6 em, which is what makes the arithmetic below
# exact rather than approximate.
_PDF_PAGE_WIDTH = 842
_PDF_PAGE_HEIGHT = 595
_PDF_MARGIN = 28
_PDF_FONT_SIZE = 7
_PDF_LEADING = 9.5
_PDF_TOP = _PDF_PAGE_HEIGHT - 35
_PDF_CHARS_PER_LINE = int((_PDF_PAGE_WIDTH - 2 * _PDF_MARGIN) / (_PDF_FONT_SIZE * 0.6))
_PDF_LINES_PER_PAGE = int((_PDF_TOP - _PDF_MARGIN) / _PDF_LEADING)
# Three lines of every page are the title, the header and the rule beneath it.
_PDF_ROWS_PER_PAGE = max(1, _PDF_LINES_PER_PAGE - 4)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_column_widths(
    dataset: ExportDataset, rows: list[dict[str, Any]]
) -> list[int]:
    """Fit the columns to the page, sampling the first rows for natural widths."""
    sample = rows[:200]
    widths: list[int] = []
    for key, label in dataset.columns:
        natural = len(label)
        for row in sample:
            natural = max(natural, len(_as_text(row.get(key))))
        widths.append(max(4, min(natural, 38)))

    gaps = 2 * (len(widths) - 1)
    budget = _PDF_CHARS_PER_LINE - gaps
    total = sum(widths)
    if total > budget and total > 0:
        scale = budget / total
        widths = [max(4, int(width * scale)) for width in widths]
        # Integer rounding can still overshoot; shave the widest columns.
        while sum(widths) > budget:
            widest = widths.index(max(widths))
            if widths[widest] <= 4:
                break
            widths[widest] -= 1
    return widths


def _pdf_row_text(values: Sequence[str], widths: Sequence[int]) -> str:
    cells: list[str] = []
    for value, width in zip(values, widths, strict=False):
        flat = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if len(flat) > width:
            flat = flat[: max(1, width - 2)] + ".." if width >= 4 else flat[:width]
        cells.append(flat.ljust(width))
    return "  ".join(cells).rstrip()


def _render_pdf(
    dataset: ExportDataset, rows: list[dict[str, Any]], title: str, source_screen: str
) -> bytes:
    """Assemble a paginated PDF 1.4 document by hand.

    Hand-assembly keeps exports free of a rendering dependency. The file is a
    plain uncompressed document: a catalog, a page tree, one Courier font, and a
    text stream per page, closed by a byte-accurate cross-reference table.
    """
    widths = _pdf_column_widths(dataset, rows)
    header = _pdf_row_text([label for _, label in dataset.columns], widths)
    rule = "-" * min(len(header), _PDF_CHARS_PER_LINE)
    generated = _now().strftime("%Y-%m-%d %H:%M UTC")
    body = [
        _pdf_row_text([_as_text(row.get(key)) for key, _ in dataset.columns], widths)
        for row in rows
    ]
    chunks = [
        body[index : index + _PDF_ROWS_PER_PAGE]
        for index in range(0, len(body), _PDF_ROWS_PER_PAGE)
    ] or [[]]

    objects: dict[int, bytes] = {}
    page_ids: list[int] = []
    next_id = 4
    for number, chunk in enumerate(chunks, start=1):
        heading = (
            f"{title} - {source_screen} - {len(rows)} rows - generated {generated} "
            f"- page {number} of {len(chunks)}"
        )
        lines = [heading[:_PDF_CHARS_PER_LINE], "", header, rule, *chunk]
        text = "\n".join(
            f"({_pdf_escape(line)}) Tj T*" for line in lines
        )
        stream = (
            f"BT /F1 {_PDF_FONT_SIZE} Tf {_PDF_LEADING} TL "
            f"1 0 0 1 {_PDF_MARGIN} {_PDF_TOP} Tm\n{text}\nET"
        ).encode("latin-1", "replace")

        content_id, page_id = next_id, next_id + 1
        next_id += 2
        objects[content_id] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PDF_PAGE_WIDTH} "
            f"{_PDF_PAGE_HEIGHT}] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("latin-1")
        page_ids.append(page_id)

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    ).encode("latin-1")
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("latin-1") + objects[number] + b"\nendobj\n"

    xref_offset = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def _render(
    export_format: str,
    dataset: ExportDataset,
    rows: list[dict[str, Any]],
    title: str,
    source_screen: str,
) -> bytes:
    if export_format == ExportFormat.CSV.value:
        return _render_csv(dataset, rows)
    if export_format == ExportFormat.JSON.value:
        return _render_json(dataset, rows, title, source_screen)
    if export_format == ExportFormat.XLSX.value:
        return _render_xlsx(dataset, rows, title)
    if export_format == ExportFormat.PDF.value:
        return _render_pdf(dataset, rows, title, source_screen)
    raise ValidationFailed(
        f"{export_format} exports are not available on this deployment.",
        details={"field": "export_format", "supported": supported_formats()},
    )


# --------------------------------------------------------------------------- #
# Spool
# --------------------------------------------------------------------------- #


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "export"


def _storage_key(workspace_id: str, export_ref: str, job_id: str, export_format: str) -> str:
    """Path relative to the spool root, so the root can move without breaking rows."""
    extension = FILE_EXTENSIONS.get(export_format, ".dat")
    return f"{workspace_id}/{export_ref}-{job_id}{extension}"


def _spool_path(storage_key: str) -> Path:
    """Resolve a stored key inside the spool root, refusing anything that escapes it."""
    root = spool_root().resolve()
    candidate = (root / storage_key).resolve()
    if not candidate.is_relative_to(root):
        raise NotFound("That export file is no longer available.")
    return candidate


def _write_spool(storage_key: str, payload: bytes) -> None:
    """Write the file, then publish it with an atomic rename.

    A download can therefore never observe a half-written file, even if the
    process dies mid-generation.
    """
    path = _spool_path(storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(payload)
    partial.replace(path)


def _discard_spool(storage_key: str | None) -> None:
    if not storage_key:
        return
    try:
        _spool_path(storage_key).unlink(missing_ok=True)
    except (OSError, NotFound):  # pragma: no cover - best effort cleanup
        log.warning("could not remove spooled export %s", storage_key)


#: Only names this module wrote are ever swept: ``exp-<n>-<job id>.<ext>`` and the
#: ``.part`` a dying process leaves behind. The spool root is configuration, and
#: a root pointed somewhere shared must not lose anything else that lives there.
_SPOOL_NAME = re.compile(r"^exp-[0-9a-z]+-[0-9a-f-]{36}\.[a-z]+(\.part)?$")

#: A file younger than this is never an orphan: generation publishes the file
#: and only then commits the row that points at it.
_ORPHAN_GRACE = dt.timedelta(hours=1)


def _stale_spool_files(older_than: dt.datetime) -> list[tuple[str, Path]]:
    """Every spooled file last written before ``older_than``, as (key, path)."""
    root = spool_root()
    if not root.is_dir():
        return []
    cutoff = older_than.timestamp()
    found: list[tuple[str, Path]] = []
    for workspace_dir in root.iterdir():
        if not workspace_dir.is_dir():
            continue
        for path in workspace_dir.iterdir():
            try:
                if (
                    _SPOOL_NAME.match(path.name)
                    and path.is_file()
                    and path.stat().st_mtime < cutoff
                ):
                    found.append((f"{workspace_dir.name}/{path.name}", path))
            except OSError:  # pragma: no cover - raced with another worker's sweep
                continue
    return found


def _unlink_all(paths: Sequence[Path]) -> int:
    removed = 0
    for path in paths:
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:  # pragma: no cover - best effort cleanup
            log.warning("could not remove orphaned export file %s", path)
    return removed


def iter_file(path: Path, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield a file in chunks so a large export never lands in memory whole."""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


# --------------------------------------------------------------------------- #
# Export jobs
# --------------------------------------------------------------------------- #

_JOB_SEARCH = (ExportJob.name, ExportJob.source_screen, ExportJob.export_ref)

_JOB_SORTABLE: dict[str, Any] = {
    "name": ExportJob.name,
    "source_screen": ExportJob.source_screen,
    "screen": ExportJob.source_screen,
    "export_format": ExportJob.export_format,
    "format": ExportJob.export_format,
    "type": ExportJob.export_format,
    "status": ExportJob.status,
    "row_count": ExportJob.row_count,
    "rows": ExportJob.row_count,
    "size_bytes": ExportJob.size_bytes,
    "size": ExportJob.size_bytes,
    "requested_at": ExportJob.requested_at,
    "ts": ExportJob.requested_at,
    "created": ExportJob.requested_at,
    "completed_at": ExportJob.completed_at,
    "download_count": ExportJob.download_count,
    "export_ref": ExportJob.export_ref,
}

_SCHEDULE_SORTABLE: dict[str, Any] = {
    "name": ExportSchedule.name,
    "source_screen": ExportSchedule.source_screen,
    "export_format": ExportSchedule.export_format,
    "cron": ExportSchedule.cron,
    "enabled": ExportSchedule.enabled,
    "last_run_at": ExportSchedule.last_run_at,
    "next_run_at": ExportSchedule.next_run_at,
    "created_at": ExportSchedule.created_at,
}


async def _next_export_ref(session: AsyncSession, workspace_id: str) -> str:
    """Allocate the next ``exp-N`` reference for a workspace."""
    used = (
        await session.execute(
            select(func.count())
            .select_from(ExportJob)
            .where(ExportJob.workspace_id == workspace_id)
        )
    ).scalar_one()
    candidate = int(used) + 1
    for _ in range(50):
        ref = f"exp-{candidate}"
        clash = (
            await session.execute(
                select(ExportJob.id)
                .where(ExportJob.workspace_id == workspace_id, ExportJob.export_ref == ref)
                .limit(1)
            )
        ).scalar_one_or_none()
        if clash is None:
            return ref
        candidate += 1
    return f"exp-{new_id()[:8]}"


async def _get_job(session: AsyncSession, principal: Principal, job_id: str) -> ExportJob:
    """Load one job, treating another tenant's row as absent rather than forbidden."""
    job = (
        await session.execute(
            select(ExportJob).where(
                ExportJob.id == job_id, ExportJob.workspace_id == principal.workspace_id
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise NotFound("That export does not exist.")
    return job


async def _get_schedule(
    session: AsyncSession, principal: Principal, schedule_id: str
) -> ExportSchedule:
    schedule = (
        await session.execute(
            select(ExportSchedule).where(
                ExportSchedule.id == schedule_id,
                ExportSchedule.workspace_id == principal.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if schedule is None:
        raise NotFound("That export schedule does not exist.")
    return schedule


async def create_export_job(
    session: AsyncSession,
    principal: Principal,
    payload: ExportJobCreate,
    *,
    request: Request | None = None,
) -> ExportJob:
    """Queue an export.

    The row is committed by the request; generation happens afterwards in
    :func:`run_export_job`. Validation is deliberately eager — an unknown
    dataset, filter or format fails here, in front of the user, rather than
    surfacing minutes later as a failed job.
    """
    principal.require(Role.MEMBER)
    dataset = _resolve_dataset(payload.source_screen, payload.filters)
    if payload.export_format.value not in FILE_EXTENSIONS:
        raise ValidationFailed(
            f"{payload.export_format.value} exports are not available on this deployment.",
            details={"field": "export_format", "supported": supported_formats()},
        )

    job = await _queue_job(
        session,
        workspace_id=principal.workspace_id,
        name=payload.name,
        source_screen=payload.source_screen,
        export_format=payload.export_format.value,
        filters=_recorded_filters(dataset, payload.filters),
        requested_by_user_id=principal.user_id,
    )
    await audit.record(
        session,
        principal=principal,
        action="Export requested",
        entity_type="ExportJob",
        entity_id=job.id,
        entity_label=job.name,
        source_screen=SCREEN,
        detail=f"{job.export_format} export of {job.source_screen}.",
        metadata={"export_ref": job.export_ref, "filters": job.filters},
        request=request,
    )
    return job


async def _queue_job(
    session: AsyncSession,
    *,
    workspace_id: str,
    name: str,
    source_screen: str,
    export_format: str,
    filters: dict[str, Any],
    requested_by_user_id: str | None,
) -> ExportJob:
    requested_at = _now()
    job = ExportJob(
        workspace_id=workspace_id,
        export_ref=await _next_export_ref(session, workspace_id),
        name=name,
        source_screen=source_screen,
        export_format=export_format,
        status=ExportStatus.QUEUED.value,
        filters=filters,
        requested_by_user_id=requested_by_user_id,
        requested_at=requested_at,
        expires_at=requested_at + dt.timedelta(days=retention_days()),
    )
    session.add(job)
    await session.flush()
    return job


async def run_export_job(job_id: str, workspace_id: str) -> None:
    """Generate the file for a queued job. Runs as a background task.

    This opens its own session on purpose. FastAPI finalises dependencies with
    ``yield`` — including the request's database session — before background
    tasks run, so the session that created the job is already committed and
    closed by the time this executes. Passing it in would use a closed session.

    Failure is a state, not an exception: anything that goes wrong is recorded
    on the job as ``Failed`` with the reason, which is what the Exports table
    shows.
    """
    sessionmaker = get_sessionmaker()
    storage_key: str | None = None
    try:
        async with sessionmaker() as session:
            # The claim is one conditional UPDATE, not a read followed by a
            # write. A job can now reach this function twice -- from the task
            # that queued it and from :func:`recover_interrupted` on another
            # worker's clock -- and only one of them may generate the file.
            claimed = await session.execute(
                sa_update(ExportJob)
                .where(
                    ExportJob.id == job_id,
                    ExportJob.workspace_id == workspace_id,
                    ExportJob.status == ExportStatus.QUEUED.value,
                )
                .values(status=ExportStatus.GENERATING.value)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if claimed.rowcount != 1:
                # Already picked up, cancelled, or deleted while queued.
                return
            job = (
                await session.execute(
                    select(ExportJob).where(
                        ExportJob.id == job_id, ExportJob.workspace_id == workspace_id
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                return

            dataset = _DATASETS[job.source_screen]
            rows = await _fetch_rows(session, dataset, workspace_id, job.filters or {})
            payload = await asyncio.to_thread(
                _render, job.export_format, dataset, rows, job.name, job.source_screen
            )
            storage_key = _storage_key(workspace_id, job.export_ref, job.id, job.export_format)
            await asyncio.to_thread(_write_spool, storage_key, payload)

            job.row_count = len(rows)
            job.size_bytes = len(payload)
            job.storage_key = storage_key
            job.status = ExportStatus.READY.value
            job.completed_at = _now()
            job.error = None
            await session.commit()
            log.info(
                "export %s ready: %d rows, %d bytes", job.export_ref, len(rows), len(payload)
            )
    except Exception as exc:  # noqa: BLE001 - the failure belongs on the job row
        log.exception("export job %s failed", job_id)
        # The file is published before the row that points at it is committed.
        # If that commit is what failed -- most often because the job was
        # deleted while it generated -- no row will ever name the file, so
        # nothing would ever reap it. Drop it here.
        await asyncio.to_thread(_discard_spool, storage_key)
        await _mark_failed(job_id, workspace_id, exc)


async def _mark_failed(job_id: str, workspace_id: str, exc: Exception) -> None:
    """Record a generation failure on its own connection, after any rollback."""
    try:
        async with get_sessionmaker()() as session:
            job = (
                await session.execute(
                    select(ExportJob).where(
                        ExportJob.id == job_id, ExportJob.workspace_id == workspace_id
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                return
            job.status = ExportStatus.FAILED.value
            job.completed_at = _now()
            # The message reaches the console, so keep it a reason, not a stack.
            job.error = f"{type(exc).__name__}: {exc}"[:1000]
            await session.commit()
    except Exception:  # pragma: no cover - never mask the original failure
        log.exception("could not record failure for export job %s", job_id)


async def list_export_jobs(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    status: Sequence[str] | None = None,
    export_format: Sequence[str] | None = None,
    source_screen: Sequence[str] | None = None,
    requested_by_user_id: str | None = None,
) -> tuple[Sequence[ExportJob], int]:
    """One page of export jobs, honouring the screen's Format and Dataset filters."""
    stmt = select(ExportJob).where(ExportJob.workspace_id == principal.workspace_id)
    stmt = apply_filters(
        stmt,
        {
            ExportJob.status: list(status) if status else None,
            ExportJob.export_format: list(export_format) if export_format else None,
            ExportJob.source_screen: list(source_screen) if source_screen else None,
            ExportJob.requested_by_user_id: requested_by_user_id,
        },
    )
    stmt = apply_search(stmt, params, _JOB_SEARCH)
    stmt = apply_sort(stmt, params, _JOB_SORTABLE, ExportJob.requested_at)
    stmt = stmt.order_by(ExportJob.id.desc())
    return await paginate(session, stmt, params)


async def get_export_job(
    session: AsyncSession, principal: Principal, job_id: str
) -> ExportJob:
    """Fetch one export job within the caller's workspace."""
    return await _get_job(session, principal, job_id)


async def update_export_job(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    payload: ExportJobUpdate,
    *,
    request: Request | None = None,
) -> ExportJob:
    """Rename a job, or correct its filters while it is still queued.

    Filters are the record of what produced a file, so once a file exists they
    are frozen: changing them would make the stored export undefendable in an
    audit.
    """
    principal.require(Role.MEMBER)
    job = await _get_job(session, principal, job_id)
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("filters") is not None:
        if job.status != ExportStatus.QUEUED.value:
            raise Conflict(
                "This export has already started; its filters record what was "
                "generated and can no longer change."
            )
        dataset = _resolve_dataset(job.source_screen, changes["filters"])
        job.filters = _recorded_filters(dataset, changes["filters"])
    if changes.get("name"):
        job.name = changes["name"]

    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Export updated",
        entity_type="ExportJob",
        entity_id=job.id,
        entity_label=job.name,
        source_screen=SCREEN,
        detail=f"Updated {', '.join(sorted(changes))}.",
        request=request,
    )
    return job


async def delete_export_job(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Delete a job and the file it generated."""
    principal.require(Role.OPERATOR)
    job = await _get_job(session, principal, job_id)
    label, ref, storage_key = job.name, job.export_ref, job.storage_key
    await session.delete(job)
    await session.flush()
    await asyncio.to_thread(_discard_spool, storage_key)
    await audit.record(
        session,
        principal=principal,
        action="Export deleted",
        entity_type="ExportJob",
        entity_id=job_id,
        entity_label=label,
        source_screen=SCREEN,
        detail=f"Deleted export {ref} and its generated file.",
        request=request,
    )


@dataclasses.dataclass(frozen=True)
class ExportDownload:
    """Everything the download route needs to stream one file."""

    path: Path
    filename: str
    media_type: str
    size_bytes: int


async def open_download(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    *,
    request: Request | None = None,
) -> ExportDownload:
    """Authorise and prepare a download, counting it.

    Retention is enforced lazily here as well as by the reaper: a job whose
    window has closed is flipped to ``Expired``, its file dropped, and the
    caller told the export does not exist — the same answer they would get for
    an id that never existed.
    """
    job = await _get_job(session, principal, job_id)
    now = _now()

    expired = job.status == ExportStatus.EXPIRED.value or (
        job.expires_at is not None and job.expires_at <= now
    )
    if expired:
        storage_key, job.storage_key = job.storage_key, None
        job.status = ExportStatus.EXPIRED.value
        await asyncio.to_thread(_discard_spool, storage_key)
        # Committed here, not left to the request: the refusal below is an
        # exception, and the request's session rolls back on any exception. The
        # flip used to be discarded that way while the file was already gone,
        # so the row went on claiming Ready for a file that no longer existed.
        await session.commit()
        raise NotFound(
            f"That export expired after {retention_days()} days. Generate it again."
        )

    if job.status == ExportStatus.FAILED.value:
        raise Conflict(job.error or "That export failed to generate.")
    if job.status != ExportStatus.READY.value:
        raise PreconditionFailed("That export is still being generated.")
    if not job.storage_key:
        raise NotFound("That export file is no longer available.")

    path = _spool_path(job.storage_key)
    if not await asyncio.to_thread(path.is_file):
        # The row says ready but the object is gone: treat it as reaped.
        job.status = ExportStatus.EXPIRED.value
        job.storage_key = None
        # Committed for the same reason as above: the 404 would roll it back.
        await session.commit()
        raise NotFound("That export file is no longer available. Generate it again.")

    job.download_count = (job.download_count or 0) + 1
    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Export downloaded",
        entity_type="ExportJob",
        entity_id=job.id,
        entity_label=job.name,
        source_screen=SCREEN,
        detail=f"Downloaded {job.export_ref} ({job.export_format}).",
        request=request,
    )

    extension = FILE_EXTENSIONS.get(job.export_format, ".dat")
    return ExportDownload(
        path=path,
        filename=f"{_slug(job.name)}-{job.export_ref}{extension}",
        media_type=MEDIA_TYPES.get(job.export_format, "application/octet-stream"),
        size_bytes=job.size_bytes or path.stat().st_size,
    )


# --------------------------------------------------------------------------- #
# Retention and recovery: what the platform clock owes this screen
# --------------------------------------------------------------------------- #

#: Lapsed jobs reaped per tick. The sweep runs on every tick, so a backlog
#: drains across ticks instead of holding the scheduler's lock for one long pass.
_REAP_BATCH = 200

#: The orphan sweep walks the whole spool, which is far too much to do on a
#: 30-second clock; once an hour per process is plenty for what it finds.
_ORPHAN_SWEEP_INTERVAL = dt.timedelta(hours=1)
_last_orphan_sweep: dt.datetime | None = None


async def reap_expired(
    session: AsyncSession, *, now: dt.datetime | None = None, limit: int = _REAP_BATCH
) -> int:
    """Enforce retention: drop the file of every job whose window has closed.

    This is the reaper the module and the model both promise. Without it the
    only thing that ever expired a job was somebody trying to download it, so
    every export ever generated stayed on the spool volume for good, the
    Status = Expired filter matched nothing, and the KPI row counted files as
    downloadable that the download route would refuse.

    The file goes first and the row second. If the caller's commit is lost, the
    row still says Ready and is picked up again on the next tick (the unlink is
    a no-op by then); the other order could commit a row that has forgotten
    where its file is while the file is still on disk. Every tenant is swept
    together, like :func:`run_due_schedules`, so this is for the platform
    scheduler and never for a request. The caller commits.

    A Failed job is left alone: it has no file to drop, and its status is the
    record the Failed KPI counts.
    """
    moment = now or _now()
    lapsed = (
        (
            await session.execute(
                select(ExportJob)
                .where(
                    ExportJob.status == ExportStatus.READY.value,
                    ExportJob.expires_at.is_not(None),
                    ExportJob.expires_at <= moment,
                )
                .order_by(ExportJob.expires_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for job in lapsed:
        await asyncio.to_thread(_discard_spool, job.storage_key)
        job.status = ExportStatus.EXPIRED.value
        job.storage_key = None
    await session.flush()
    return len(lapsed)


async def sweep_orphans(session: AsyncSession, *, now: dt.datetime | None = None) -> int:
    """Delete spooled files that no job row points at, and abandoned ``.part`` files.

    A row is the only thing that leads the reaper to a file, so a file without
    one is kept for ever: a process killed between publishing the file and
    committing its row leaves one, and so does one killed mid-write. Only files
    older than :data:`_ORPHAN_GRACE` are considered, which keeps this well away
    from an export that is being written right now.
    """
    moment = now or _now()
    candidates = await asyncio.to_thread(_stale_spool_files, moment - _ORPHAN_GRACE)
    if not candidates:
        return 0
    known = set(
        (
            await session.execute(
                select(ExportJob.storage_key).where(ExportJob.storage_key.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    orphans = [path for key, path in candidates if key not in known]
    return await asyncio.to_thread(_unlink_all, orphans)


#: A job that has been Generating this long is not generating any more. One
#: export is capped at ``max_rows()`` and renders in seconds; nothing honest
#: takes a quarter of an hour.
GENERATING_DEADLINE = dt.timedelta(minutes=15)

#: How long a Queued job is left for the task that queued it. Generation starts
#: the moment the request commits, so a job still Queued after this has lost the
#: process that was going to pick it up.
QUEUED_GRACE = dt.timedelta(minutes=2)

INTERRUPTED = "Interrupted before it finished (restart or timeout). Run it again."


async def recover_interrupted(
    session: AsyncSession, *, now: dt.datetime | None = None, limit: int = _REAP_BATCH
) -> list[tuple[str, str]]:
    """Settle the jobs a restart orphaned, so none stays in flight for ever.

    Generation lives only in the process that started it: a background task, or
    the scheduler's own tick. A deploy, an OOM kill or a crash between the
    commit that queued a job and the task that would have run it leaves the row
    Queued or Generating with nothing left to move it -- no error, no file, and
    a console poll that never ends.

    * **Generating** past :data:`GENERATING_DEADLINE` is failed with the reason,
      the same words the clock uses for an orphaned test run. It is not retried:
      whatever killed it mid-render may well kill it again, and a Failed row
      offers Re-run to a person who can see why.
    * **Queued** past :data:`QUEUED_GRACE` never started, so it is simply
      started: its ``(job_id, workspace_id)`` is returned for the caller to
      hand to :func:`run_export_job`, whose claim makes a second pickup a no-op.

    Every tenant is swept together, so this is for the platform scheduler and
    never for a request. The caller commits.
    """
    moment = now or _now()
    wedged = (
        (
            await session.execute(
                select(ExportJob)
                .where(
                    ExportJob.status == ExportStatus.GENERATING.value,
                    ExportJob.updated_at < moment - GENERATING_DEADLINE,
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for job in wedged:
        # It may have died after publishing the file and before recording it.
        await asyncio.to_thread(
            _discard_spool,
            _storage_key(job.workspace_id, job.export_ref, job.id, job.export_format),
        )
        job.status = ExportStatus.FAILED.value
        job.completed_at = moment
        job.error = INTERRUPTED
    await session.flush()
    if wedged:
        log.warning("export recovery: %d interrupted job(s) marked Failed", len(wedged))

    stranded = (
        await session.execute(
            select(ExportJob.id, ExportJob.workspace_id)
            .where(
                ExportJob.status == ExportStatus.QUEUED.value,
                ExportJob.requested_at < moment - QUEUED_GRACE,
            )
            .order_by(ExportJob.requested_at.asc())
            .limit(limit)
        )
    ).all()
    if stranded:
        log.warning("export recovery: %d queued job(s) picked up again", len(stranded))
    return [(job_id, workspace_id) for job_id, workspace_id in stranded]


async def _keep_house(session: AsyncSession, moment: dt.datetime) -> list[tuple[str, str]]:
    """Retention and recovery, run ahead of the firings on every scheduler tick.

    Each step sits in its own savepoint and none of them may raise: a spool
    that cannot be listed must not stop Monday's export from firing. Returns
    the stranded jobs the caller should generate alongside the new firings.
    """
    global _last_orphan_sweep
    stranded: list[tuple[str, str]] = []
    try:
        async with session.begin_nested():
            stranded = await recover_interrupted(session, now=moment)
    except Exception:  # noqa: BLE001 - housekeeping never stops the firings
        log.exception("export recovery sweep failed")

    try:
        async with session.begin_nested():
            reaped = await reap_expired(session, now=moment)
        if reaped:
            log.info("export retention: %d job(s) expired and their files dropped", reaped)
    except Exception:  # noqa: BLE001 - housekeeping never stops the firings
        log.exception("export retention sweep failed")

    if _last_orphan_sweep is None or moment - _last_orphan_sweep >= _ORPHAN_SWEEP_INTERVAL:
        _last_orphan_sweep = moment
        try:
            removed = await sweep_orphans(session, now=moment)
            if removed:
                log.info("export retention: %d orphaned spool file(s) removed", removed)
        except Exception:  # noqa: BLE001 - housekeeping never stops the firings
            log.exception("export orphan sweep failed")
    return stranded


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #


def delivery_available() -> bool:
    """Whether a finished export can be sent to a schedule's recipients.

    It cannot: this build has no mail or notification transport, and nothing
    reads ``ExportSchedule.recipients`` after it is stored. The column, the
    editor's input and the audit entry all promised a Monday-morning email that
    nobody was ever sent, and the file then aged out of retention unread. Until
    a transport exists the API says so instead of collecting addresses, and the
    summary carries this flag so the console can stop offering the input.
    """
    return False


def _refuse_undeliverable(recipients: Sequence[str], *, already: Sequence[str] = ()) -> None:
    """Reject recipients nobody would deliver to.

    Addresses a schedule already holds are let through, so a schedule written
    before this check can still be renamed or re-timed by a console that sends
    the whole form back; what is refused is promising delivery to anyone new.
    """
    if delivery_available():
        return
    added = [value for value in recipients if value not in set(already)]
    if added:
        raise ValidationFailed(
            "Export delivery is not configured on this deployment, so nobody would "
            "be sent this export. Remove the recipients: each run stays on the "
            f"Exports screen for {retention_days()} days.",
            details={"field": "recipients", "delivery_available": False},
        )


def next_run_after(cron: str, after: dt.datetime | None = None) -> dt.datetime:
    """Next UTC firing of a cron expression, strictly after ``after``."""
    base = after or _now()
    if base.tzinfo is None:
        base = base.replace(tzinfo=dt.UTC)
    return croniter(cron, base).get_next(dt.datetime)


async def list_schedules(
    session: AsyncSession,
    principal: Principal,
    params: ListParams,
    *,
    enabled: bool | None = None,
    source_screen: Sequence[str] | None = None,
    export_format: Sequence[str] | None = None,
) -> tuple[Sequence[ExportSchedule], int]:
    """One page of recurring exports."""
    stmt = select(ExportSchedule).where(
        ExportSchedule.workspace_id == principal.workspace_id
    )
    stmt = apply_filters(
        stmt,
        {
            ExportSchedule.enabled: enabled,
            ExportSchedule.source_screen: list(source_screen) if source_screen else None,
            ExportSchedule.export_format: list(export_format) if export_format else None,
        },
    )
    stmt = apply_search(
        stmt,
        params,
        (ExportSchedule.name, ExportSchedule.source_screen, ExportSchedule.cron),
    )
    stmt = apply_sort(
        stmt, params, _SCHEDULE_SORTABLE, ExportSchedule.name, default_desc=False
    )
    stmt = stmt.order_by(ExportSchedule.id.desc())
    return await paginate(session, stmt, params)


async def get_schedule(
    session: AsyncSession, principal: Principal, schedule_id: str
) -> ExportSchedule:
    """Fetch one export schedule within the caller's workspace."""
    return await _get_schedule(session, principal, schedule_id)


async def _assert_schedule_name_free(
    session: AsyncSession, principal: Principal, name: str, *, exclude_id: str | None = None
) -> None:
    stmt = select(ExportSchedule.id).where(
        ExportSchedule.workspace_id == principal.workspace_id, ExportSchedule.name == name
    )
    if exclude_id:
        stmt = stmt.where(ExportSchedule.id != exclude_id)
    if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
        raise Conflict(f"An export schedule named '{name}' already exists in this workspace.")


async def create_schedule(
    session: AsyncSession,
    principal: Principal,
    payload: ExportScheduleCreate,
    *,
    request: Request | None = None,
) -> ExportSchedule:
    """Create a recurring export and compute its first firing."""
    principal.require(Role.OPERATOR)
    dataset = _resolve_dataset(payload.source_screen, payload.filters)
    if payload.export_format.value not in FILE_EXTENSIONS:
        raise ValidationFailed(
            f"{payload.export_format.value} exports are not available on this deployment.",
            details={"field": "export_format", "supported": supported_formats()},
        )
    _refuse_undeliverable(payload.recipients)
    await _assert_schedule_name_free(session, principal, payload.name)

    schedule = ExportSchedule(
        workspace_id=principal.workspace_id,
        name=payload.name,
        source_screen=payload.source_screen,
        export_format=payload.export_format.value,
        cron=payload.cron,
        filters=_recorded_filters(dataset, payload.filters),
        recipients=list(payload.recipients),
        enabled=payload.enabled,
        next_run_at=next_run_after(payload.cron) if payload.enabled else None,
        owner_user_id=principal.user_id,
        created_by=principal.actor,
        updated_by=principal.actor,
    )
    session.add(schedule)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(
            f"An export schedule named '{payload.name}' already exists in this workspace."
        ) from exc

    await audit.record(
        session,
        principal=principal,
        action="Export schedule created",
        entity_type="ExportSchedule",
        entity_id=schedule.id,
        entity_label=schedule.name,
        source_screen=SCREEN,
        detail=f"{schedule.export_format} export of {schedule.source_screen} on '{schedule.cron}'.",
        metadata={"recipients": schedule.recipients},
        request=request,
    )
    return schedule


async def update_schedule(
    session: AsyncSession,
    principal: Principal,
    schedule_id: str,
    payload: ExportScheduleUpdate,
    *,
    request: Request | None = None,
) -> ExportSchedule:
    """Partially update a schedule, recomputing the next firing when it moves."""
    principal.require(Role.OPERATOR)
    schedule = await _get_schedule(session, principal, schedule_id)
    changes = payload.model_dump(exclude_unset=True)

    target_screen = changes.get("source_screen") or schedule.source_screen
    target_filters = (
        changes["filters"] if changes.get("filters") is not None else (schedule.filters or {})
    )
    retarget = "source_screen" in changes or changes.get("filters") is not None
    if retarget:
        dataset = _resolve_dataset(target_screen, target_filters)

    if changes.get("recipients") is not None:
        _refuse_undeliverable(changes["recipients"], already=schedule.recipients or [])

    if changes.get("name") and changes["name"] != schedule.name:
        await _assert_schedule_name_free(
            session, principal, changes["name"], exclude_id=schedule.id
        )

    if changes.get("export_format") is not None:
        export_format = changes["export_format"]
        value = export_format.value if isinstance(export_format, ExportFormat) else export_format
        if value not in FILE_EXTENSIONS:
            raise ValidationFailed(
                f"{value} exports are not available on this deployment.",
                details={"field": "export_format", "supported": supported_formats()},
            )
        schedule.export_format = value
    if changes.get("name"):
        schedule.name = changes["name"]
    if changes.get("source_screen"):
        schedule.source_screen = changes["source_screen"]
    if retarget:
        # Moving a schedule onto an inventory dataset drops the window it
        # carried, whether or not this edit sent the filters again.
        schedule.filters = _recorded_filters(dataset, target_filters)
    if changes.get("recipients") is not None:
        schedule.recipients = list(changes["recipients"])
    if changes.get("cron"):
        schedule.cron = changes["cron"]
    if changes.get("enabled") is not None:
        schedule.enabled = changes["enabled"]

    # A disabled schedule has no next firing; anything that moves the cadence
    # re-derives one so the sweeper never fires on a stale timestamp.
    if not schedule.enabled:
        schedule.next_run_at = None
    elif changes.get("cron") or changes.get("enabled") or schedule.next_run_at is None:
        schedule.next_run_at = next_run_after(schedule.cron)
    schedule.updated_by = principal.actor

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("Another export schedule in this workspace already uses that name.") from exc

    await audit.record(
        session,
        principal=principal,
        action="Export schedule updated",
        entity_type="ExportSchedule",
        entity_id=schedule.id,
        entity_label=schedule.name,
        source_screen=SCREEN,
        detail=f"Updated {', '.join(sorted(changes))}.",
        request=request,
    )
    return schedule


async def delete_schedule(
    session: AsyncSession,
    principal: Principal,
    schedule_id: str,
    *,
    request: Request | None = None,
) -> None:
    """Delete a schedule. Files it already produced are kept."""
    principal.require(Role.OPERATOR)
    schedule = await _get_schedule(session, principal, schedule_id)
    label = schedule.name
    await session.delete(schedule)
    await session.flush()
    await audit.record(
        session,
        principal=principal,
        action="Export schedule deleted",
        entity_type="ExportSchedule",
        entity_id=schedule_id,
        entity_label=label,
        source_screen=SCREEN,
        detail=f"Deleted export schedule '{label}'.",
        request=request,
    )


async def run_due_schedules(
    session: AsyncSession, *, limit: int = 50, now: dt.datetime | None = None
) -> list[tuple[str, str]]:
    """Queue a job for every schedule that is due, and advance each cadence.

    The platform scheduler calls this, then awaits :func:`run_export_job` for
    each ``(job_id, workspace_id)`` returned. Jobs created this way carry no
    ``requested_by_user_id``, which is what the console renders as
    "System (Scheduled)".

    This is the export domain's one appointment with the platform clock, so the
    domain's other time-driven duties ride on it. Retention is enforced first
    (:func:`reap_expired`) -- it used to be promised in three docstrings and
    carried out by nothing -- and jobs a restart left in flight are settled
    (:func:`recover_interrupted`). A stranded Queued job is returned with the
    new firings, because what it needs is exactly what they need.
    """
    moment = now or _now()
    stranded = await _keep_house(session, moment)
    due = (
        (
            await session.execute(
                select(ExportSchedule)
                .where(
                    ExportSchedule.enabled.is_(True),
                    ExportSchedule.next_run_at.is_not(None),
                    ExportSchedule.next_run_at <= moment,
                )
                .order_by(ExportSchedule.next_run_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    queued: list[tuple[str, str]] = []
    for schedule in due:
        if schedule.source_screen not in _DATASETS:
            # The dataset was retired after the schedule was written; skip the
            # firing rather than queue work that cannot succeed.
            log.warning(
                "schedule %s targets unknown dataset %s", schedule.id, schedule.source_screen
            )
            schedule.next_run_at = next_run_after(schedule.cron, moment)
            continue
        job = await _queue_job(
            session,
            workspace_id=schedule.workspace_id,
            name=f"{schedule.name} — {moment.strftime('%Y-%m-%d')}",
            source_screen=schedule.source_screen,
            export_format=schedule.export_format,
            filters=_recorded_filters(
                _DATASETS[schedule.source_screen], schedule.filters or {}
            ),
            requested_by_user_id=None,
        )
        schedule.last_run_at = moment
        schedule.next_run_at = next_run_after(schedule.cron, moment)
        queued.append((job.id, schedule.workspace_id))

    await session.flush()
    return stranded + queued


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


async def summary(session: AsyncSession, principal: Principal) -> ExportsSummary:
    """The KPI row above the Exports table.

    Volume, counts and failures come from one aggregate over the 30-day window;
    the schedule count and the filter vocabularies are three small queries
    beside it. Nothing is summed in Python.
    """
    workspace = principal.workspace_id
    now = _now()
    cutoff = now - dt.timedelta(days=SUMMARY_WINDOW_DAYS)
    # "Downloadable now" is what ``ExportJobRead.is_downloadable`` says row by
    # row: Ready *and* still inside retention. Counting on status alone called a
    # file downloadable for up to 30 days when retention is 7.
    downloadable = (ExportJob.status == ExportStatus.READY.value) & (
        ExportJob.expires_at.is_(None) | (ExportJob.expires_at > now)
    )

    jobs = (
        await session.execute(
            select(
                func.count(ExportJob.id).label("total"),
                func.coalesce(func.sum(ExportJob.size_bytes), 0).label("bytes"),
                func.coalesce(func.sum(ExportJob.row_count), 0).label("rows"),
                func.coalesce(
                    func.sum(
                        case((ExportJob.status == ExportStatus.FAILED.value, 1), else_=0)
                    ),
                    0,
                ).label("failed"),
                func.coalesce(func.sum(case((downloadable, 1), else_=0)), 0).label("ready"),
            ).where(ExportJob.workspace_id == workspace, ExportJob.requested_at >= cutoff)
        )
    ).one()

    scheduled = (
        await session.execute(
            select(func.count())
            .select_from(ExportSchedule)
            .where(
                ExportSchedule.workspace_id == workspace, ExportSchedule.enabled.is_(True)
            )
        )
    ).scalar_one()

    formats = (
        (
            await session.execute(
                select(ExportJob.export_format)
                .where(ExportJob.workspace_id == workspace)
                .distinct()
                .order_by(ExportJob.export_format.asc())
            )
        )
        .scalars()
        .all()
    )
    screens = (
        (
            await session.execute(
                select(ExportJob.source_screen)
                .where(ExportJob.workspace_id == workspace)
                .distinct()
                .order_by(ExportJob.source_screen.asc())
            )
        )
        .scalars()
        .all()
    )

    return ExportsSummary(
        exports_30d=int(jobs.total or 0),
        scheduled=int(scheduled or 0),
        total_bytes_30d=int(jobs.bytes or 0),
        total_rows_30d=int(jobs.rows or 0),
        failed_30d=int(jobs.failed or 0),
        ready=int(jobs.ready or 0),
        formats=[value for value in formats if value],
        source_screens=[value for value in screens if value],
        datasets=available_datasets(),
        delivery_available=delivery_available(),
    )
