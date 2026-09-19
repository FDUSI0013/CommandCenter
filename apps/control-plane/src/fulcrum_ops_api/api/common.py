"""Shared building blocks for every list endpoint.

The console's table component asks the same questions of every collection —
search, per-column sort, dropdown filters, pagination, CSV export — so the API
answers them the same way everywhere: identical query parameters and an
identical envelope. That is what lets one table component drive 23 screens.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Sequence
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from ..core.errors import ValidationFailed

T = TypeVar("T")

MAX_PAGE_SIZE = 200


class Page(BaseModel, Generic[T]):
    """Envelope returned by every list endpoint."""

    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(cls, items: Sequence[T], total: int, page: int, page_size: int) -> Page[T]:
        return cls(
            items=list(items),
            total=total,
            page=page,
            page_size=page_size,
            pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


class ListParams(BaseModel):
    """Standard query parameters accepted by every list endpoint."""

    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=MAX_PAGE_SIZE)
    q: str | None = None
    sort: str | None = Field(None, description="Column key; prefix with '-' for descending")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def sort_key(self) -> str | None:
        if not self.sort:
            return None
        return self.sort[1:] if self.sort.startswith("-") else self.sort

    @property
    def descending(self) -> bool:
        return bool(self.sort and self.sort.startswith("-"))


def list_params(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
    q: Annotated[str | None, Query(description="Free-text search")] = None,
    sort: Annotated[str | None, Query(description="Sort key, '-' prefix for descending")] = None,
) -> ListParams:
    return ListParams(page=page, page_size=page_size, q=q, sort=sort)


ListQuery = Annotated[ListParams, Query()]


def apply_search(
    stmt: Select, params: ListParams, columns: Sequence[InstrumentedAttribute]
) -> Select:
    if not params.q or not columns:
        return stmt
    needle = f"%{params.q.strip()}%"
    return stmt.where(or_(*[c.ilike(needle) for c in columns]))


def apply_sort(
    stmt: Select,
    params: ListParams,
    sortable: dict[str, InstrumentedAttribute],
    default: InstrumentedAttribute,
    default_desc: bool = True,
) -> Select:
    """Order the list -- totally, and the same way on every database.

    Two things made a sorted table unreliable. NULLs: Postgres sorts them as
    the largest value and SQLite as the smallest, so ``-last_run_at`` opened
    with every never-run row in production and closed with them under test. A
    value that was never recorded is not the newest or the oldest of anything;
    it goes last whichever way the column is sorted. And ties: ordering by a
    column whose values repeat (a status, a name, a timestamp shared by a bulk
    write) leaves the order *within* a tie to the planner, which may choose
    differently for OFFSET 0 and OFFSET 25 -- a row turns up on two pages and
    another on none. The primary key breaks every tie the same way each time.
    """
    if params.sort_key:
        column = sortable.get(params.sort_key)
        if column is None:
            raise ValidationFailed(
                f"Cannot sort by '{params.sort_key}'.",
                details={"sortable": sorted(sortable)},
            )
        descending = params.descending
    else:
        column, descending = default, default_desc
    ordering = column.desc() if descending else column.asc()
    return stmt.order_by(ordering.nulls_last(), *_tiebreak(stmt, descending))


def _tiebreak(stmt: Select, descending: bool) -> list[Any]:
    """The listed entity's primary key, where the statement can be ordered by it.

    An aggregate or DISTINCT statement cannot: Postgres refuses an ORDER BY
    term that is neither grouped nor selected, and those lists are short enough
    not to page. An aliased entity is left alone too -- its key would have to be
    the alias's, not the table's.
    """
    if getattr(stmt, "_group_by_clauses", ()) or getattr(stmt, "_distinct", False):
        return []
    entity = next(
        (d["entity"] for d in stmt.column_descriptions if d.get("entity") is not None),
        None,
    )
    if not isinstance(entity, type):
        return []
    # Ids are time-ordered (UUIDv7), so following the sort's direction keeps
    # "newest first" reading newest first inside a tie as well.
    return [
        key.desc() if descending else key.asc() for key in sa_inspect(entity).primary_key
    ]


def apply_filters(stmt: Select, filters: dict[InstrumentedAttribute, Any]) -> Select:
    """Apply equality filters, skipping the ones the caller left unset."""
    for column, value in filters.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple, set)):
            values = [v for v in value if v not in (None, "")]
            if values:
                stmt = stmt.where(column.in_(values))
        else:
            stmt = stmt.where(column == value)
    return stmt


async def paginate(
    session: AsyncSession, stmt: Select, params: ListParams
) -> tuple[Sequence[Any], int]:
    """Run the count and the page in two statements and return both."""
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    rows = (
        (await session.execute(stmt.limit(params.page_size).offset(params.offset)))
        .scalars()
        .all()
    )
    return rows, total


def to_csv(rows: Sequence[dict[str, Any]], columns: Sequence[tuple[str, str]]) -> str:
    """Render rows as CSV. ``columns`` is [(key, header), …] in display order."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([header for _, header in columns])
    for row in rows:
        writer.writerow([_csv_value(row.get(key)) for key, _ in columns])
    return buffer.getvalue()


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


class ActionResult(BaseModel):
    """Uniform response for the console's many verb endpoints."""

    ok: bool = True
    message: str
    entity_id: str | None = None
    data: dict[str, Any] | None = None
