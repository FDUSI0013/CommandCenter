"""Declarative base, common column types, and mixins shared by every model."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid

from sqlalchemy import DateTime, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

# Explicit naming convention so Alembic autogenerate produces stable,
# reversible migrations on both SQLite and Postgres.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict: JSON, list: JSON}


class UtcDateTime(TypeDecorator):
    """Store timezone-aware UTC datetimes; SQLite drops tzinfo otherwise."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


def new_id() -> str:
    """A real UUIDv7: 48-bit millisecond timestamp, then randomness.

    Time-ordered, so primary keys cluster by insert time instead of scattering
    writes across the index the way v4 does.

    The version and variant bits are set explicitly, and that is not cosmetic:
    the telemetry engine validates that every trace and span id it is given is
    genuinely v7 and rejects the batch with "Trace id must be a version 7 UUID"
    otherwise. An id that is merely time-ordered looks right in a log and fails
    at the boundary.
    """
    ts_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    raw = bytearray(ts_ms.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(raw)))


class PrimaryKeyMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class TimestampMixin:
    """``created_at`` / ``updated_at``, stamped in this process rather than by the database.

    The obvious spelling is ``onupdate=func.now()``, and it is wrong here. A SQL
    expression means SQLAlchemy does not know the value it just wrote, so it
    *expires* the attribute after the UPDATE; the next read of ``updated_at`` —
    which is every serialisation of the row that was just changed — emits a
    lazy SELECT. Under asyncio that raises ``MissingGreenlet``, so a mutation
    would succeed and then fail while rendering its own response.

    A Python callable is computed in-process, sent in the UPDATE, and left on
    the instance, so no refresh is needed and no endpoint has to remember to ask
    for one. The clock is the application's rather than the database's, which is
    also what we want when a workspace's rows are read across replicas.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), default=utcnow, onupdate=utcnow, nullable=False
    )


class ActorMixin:
    """Who last touched the row — every governance surface displays this."""

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class WorkspaceScopedMixin:
    """Every tenant-owned row carries its workspace; all queries filter on it."""

    workspace_id: Mapped[str] = mapped_column(
        String(36), index=True, nullable=False
    )
