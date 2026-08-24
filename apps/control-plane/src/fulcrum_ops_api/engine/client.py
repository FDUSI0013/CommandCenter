"""Typed async adapter to the private telemetry engine.

The telemetry engine is an internal service. It is deployed on the cluster
network with no public route, and in that position it authenticates nobody.
This control plane is therefore the *sole* identity authority in front of it.
Every call that reaches this module has already been authenticated, authorised
and workspace-scoped by the API layer; the project namespace it addresses is
derived from the caller's session or API key -- never from a value the caller
supplied. Nothing in this repository may expose the engine directly.

This is also the only module that knows the engine's HTTP contract. Everything
above it speaks in domain terms and receives plain parsed JSON, so the engine
can be swapped without touching a router, a service or a model.

``base_url`` must already include whatever prefix the internal gateway adds:
the engine roots its own resources at ``/v1/private``, and the standard
gateway republishes them under ``/api/v1/private``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from ..core.config import settings

logger = logging.getLogger(__name__)

# The engine runs single-tenant behind this service. With its own auth disabled
# it resolves an absent workspace header to its default namespace, so we send
# none at all. Tenancy is enforced entirely above this layer: each control plane
# workspace owns a distinct project namespace inside the engine, and every call
# made here has already been scoped to one workspace by the API layer.

#: Correlation id we stamp on every outbound call so an engine-side log line
#: can be tied back to the control-plane request that caused it.
ENGINE_REQUEST_ID_HEADER: Final[str] = "X-Request-Id"

#: Seconds until the engine's throttle window rolls over; present on 429s.
ENGINE_RATE_LIMIT_RESET_HEADER: Final[str] = "RateLimit-Reset"

#: Every resource this adapter speaks to lives under one root.
API_ROOT: Final[str] = "/v1/private"

#: Liveness probe; deliberately outside the versioned tree.
HEALTH_PATH: Final[str] = "/is-alive/ping"

#: Only these verbs are safe to replay. POST creates traces, spans, comments
#: and experiment items -- replaying one duplicates customer telemetry.
IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"GET", "PUT", "DELETE"})

_BACKOFF_BASE_SECONDS: Final[float] = 0.25
_BACKOFF_CAP_SECONDS: Final[float] = 4.0
_ERROR_BODY_LIMIT: Final[int] = 2048

JsonObject = dict[str, Any]
Filters = Sequence[Mapping[str, Any]] | str | None
Ids = Sequence[str] | None


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class EngineError(Exception):
    """Base class for every failure raised while talking to the engine."""


class EngineUnavailable(EngineError):
    """Unreachable, or still answering 5xx after the last retry."""


class EngineTimeout(EngineUnavailable):
    """Connected but did not answer in time.

    Subclasses ``EngineUnavailable`` so callers that only care that telemetry
    is down catch a single class.
    """


class EngineBadRequest(EngineError):
    """The engine refused the request (any 4xx). Carries the raw body."""

    def __init__(
        self, status: int, body: str, *, retry_after: float | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"engine refused the request with status {status}")


class EngineNotFound(EngineBadRequest):
    """404 -- the addressed entity does not exist in this workspace."""

    def __init__(self, body: str = "") -> None:
        super().__init__(404, body)


# --------------------------------------------------------------------------
# wire helpers
# --------------------------------------------------------------------------


def _iso(value: dt.datetime | str | None) -> str | None:
    """The engine parses instants as ISO-8601; naive datetimes mean UTC here."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _json_param(value: Any) -> str | None:
    """``filters``, ``sorting``, ``exclude`` and every id-list query parameter
    travel as a JSON document inside a single query string value."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(list(value), separators=(",", ":"))


def _params(**kwargs: Any) -> dict[str, str]:
    """Drop unset values and render the scalars the query parser expects."""
    out: dict[str, str] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, dt.datetime):
            out[key] = _iso(value) or ""
        else:
            out[key] = str(value)
    return out


def _body(**kwargs: Any) -> JsonObject:
    """Build a request body, omitting the fields the caller left unset."""
    return {key: value for key, value in kwargs.items() if value is not None}


def _decode(response: httpx.Response) -> Any:
    """Most mutations answer 204 with no body; reads answer JSON."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise EngineBadRequest(
            response.status_code, "engine returned a body that is not JSON"
        ) from exc


def _decode_lines(response: httpx.Response) -> list[JsonObject]:
    """The streaming endpoints answer newline-delimited JSON, one row per line.

    A row may be an error envelope rather than an entity; the caller sees it
    verbatim rather than having it silently dropped.
    """
    rows: list[JsonObject] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise EngineBadRequest(
                response.status_code, "engine returned a malformed stream row"
            ) from exc
    return rows


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _id_from_location(response: httpx.Response) -> str | None:
    """Creates answer 201 with an empty body and a Location header."""
    location = response.headers.get("location")
    if not location:
        return None
    return location.rstrip("/").rsplit("/", 1)[-1] or None


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


class EngineClient:
    """Async adapter over the engine's private HTTP API.

    One instance per process. It owns a pooled ``httpx.AsyncClient`` built at
    application startup and closed at shutdown; every method returns parsed
    JSON and knows nothing about the ORM.
    """

    def __init__(
        self,
        base_url: str | None = None,
        workspace: str | None = None,
        api_key: str | None = None,
        *,
        timeout_seconds: float | None = None,
        connect_timeout_seconds: float | None = None,
        max_connections: int | None = None,
        retries: int | None = None,
        checker_base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.engine_base_url).rstrip("/")
        self._workspace = workspace or settings.engine_workspace
        self._api_key = api_key if api_key is not None else settings.engine_api_key
        self._retries = settings.engine_retries if retries is None else retries
        # The inline content checker runs as its own service; when it is not
        # separately addressed we assume the same gateway fronts both.
        self._checker_base_url = (checker_base_url or "").rstrip("/") or None

        # The engine resolves the tenant from this header on every request. It
        # is not optional: omit it and the engine falls back to its default
        # namespace, so every tenant would read and write the same telemetry.
        headers = {
            "accept": "application/json",
        }
        if self._api_key:
            # A shared internal credential, not an end-user token: the engine
            # reads it raw off Authorization.
            headers["authorization"] = self._api_key

        pool = max_connections or settings.engine_max_connections
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            limits=httpx.Limits(
                max_connections=pool,
                max_keepalive_connections=max(1, pool // 4),
                keepalive_expiry=30.0,
            ),
            timeout=httpx.Timeout(
                timeout_seconds or settings.engine_timeout_seconds,
                connect=connect_timeout_seconds
                or settings.engine_connect_timeout_seconds,
            ),
            transport=transport,
            follow_redirects=False,
        )

    # -- lifecycle ---------------------------------------------------------

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> EngineClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- transport ---------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        window = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        # Equal jitter: half fixed, half random, so a fleet of workers does not
        # stampede the engine in lockstep after it restarts.
        return window / 2 + random.uniform(0.0, window / 2)  # noqa: S311 — jitter, not a secret

    @staticmethod
    def _checked(response: httpx.Response) -> httpx.Response:
        """Map 4xx onto the error classes. 5xx never reaches here."""
        status = response.status_code
        if status < 400:
            return response
        body = response.text[:_ERROR_BODY_LIMIT]
        if status == 404:
            raise EngineNotFound(body)
        raise EngineBadRequest(
            status,
            body,
            retry_after=_float_or_none(
                response.headers.get(ENGINE_RATE_LIMIT_RESET_HEADER)
            ),
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        retries: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Issue one call, retrying only what is safe to replay.

        Connect failures and 5xx are retried on GET/PUT/DELETE; POST is never
        retried because the engine's POSTs create telemetry.
        """
        method = method.upper()
        attempts = 1
        if method in IDEMPOTENT_METHODS:
            attempts += self._retries if retries is None else retries

        last_error: EngineError | None = None
        last_cause: Exception | None = None

        for attempt in range(1, attempts + 1):
            request_id = uuid.uuid4().hex
            # Request and response bodies are never logged at any level: they
            # carry customer prompt and completion text.
            logger.debug(
                "engine call %s %s attempt=%d/%d request_id=%s",
                method,
                path,
                attempt,
                attempts,
                request_id,
            )
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=dict(params) if params else None,
                    json=json_body,
                    headers={ENGINE_REQUEST_ID_HEADER: request_id, **(headers or {})},
                )
            except httpx.TimeoutException as exc:
                last_error = EngineTimeout(f"engine timed out on {method} {path}")
                last_cause = exc
            except httpx.TransportError as exc:
                last_error = EngineUnavailable(
                    f"engine is unreachable on {method} {path}"
                )
                last_cause = exc
            else:
                logger.debug(
                    "engine reply %s %s status=%d request_id=%s",
                    method,
                    path,
                    response.status_code,
                    request_id,
                )
                if response.status_code < 500:
                    return self._checked(response)
                last_error = EngineUnavailable(
                    f"engine answered {response.status_code} on {method} {path}"
                )
                last_cause = None

            if attempt >= attempts:
                raise last_error from last_cause
            await asyncio.sleep(self._backoff(attempt))

        raise last_error or EngineUnavailable(f"engine failed on {method} {path}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        retries: int | None = None,
    ) -> Any:
        response = await self._send(
            method, path, params=params, json_body=json_body, retries=retries
        )
        return _decode(response)

    async def _stream(self, path: str, payload: JsonObject) -> list[JsonObject]:
        """Call one of the engine's streaming search endpoints.

        These do not return a JSON document: they return newline-delimited JSON
        as an octet stream. The client's default ``Accept: application/json``
        makes the engine refuse the request outright with 406 Not Acceptable —
        the search never runs, so the failure looks like an empty result rather
        than a rejected one until you read the engine's access log.
        """
        response = await self._send(
            "POST",
            path,
            json_body=payload,
            headers={"accept": "application/octet-stream"},
        )
        return _decode_lines(response)

    async def _create(self, path: str, payload: JsonObject) -> str | None:
        """POST a create and return the new id read off Location."""
        response = await self._send("POST", path, json_body=payload)
        return _id_from_location(response)

    # -- health ------------------------------------------------------------

    async def health(self) -> bool:
        """Liveness only -- a probe must fail fast, so it never retries."""
        try:
            payload = await self._request("GET", HEALTH_PATH, retries=0)
        except EngineError:
            return False
        if isinstance(payload, dict):
            return bool(payload.get("healthy", True))
        return True

    # -- projects ----------------------------------------------------------

    async def list_projects(
        self,
        *,
        page: int = 1,
        size: int = 100,
        name: str | None = None,
        sorting: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/projects",
            params=_params(
                page=page, size=size, name=name, sorting=_json_param(sorting)
            ),
        )

    async def get_project(self, project_id: str) -> JsonObject:
        return await self._request("GET", f"{API_ROOT}/projects/{project_id}")

    async def create_project(
        self,
        name: str,
        *,
        description: str | None = None,
        visibility: str | None = None,
    ) -> JsonObject:
        payload = _body(name=name, description=description, visibility=visibility)
        await self._create(f"{API_ROOT}/projects", payload)
        # The create answers 201 with no body; resolve the full row by name so
        # callers always get the same shape a read would give them.
        created = await self.find_project_by_name(name)
        return created if created is not None else payload

    async def find_project_by_name(
        self, name: str, *, include_stats: bool = False
    ) -> JsonObject | None:
        try:
            return await self._request(
                "POST",
                f"{API_ROOT}/projects/retrieve",
                json_body=_body(name=name, include_stats=include_stats),
            )
        except EngineNotFound:
            return None

    async def ensure_project(self, name: str) -> JsonObject:
        """Get-or-create, called when an agent is registered.

        Registration is idempotent and can race across workers, so a losing
        create is resolved by re-reading rather than surfaced as an error.
        """
        existing = await self.find_project_by_name(name)
        if existing is not None:
            return existing
        try:
            return await self.create_project(name)
        except EngineBadRequest as exc:
            if exc.status != 409:
                raise
        resolved = await self.find_project_by_name(name)
        if resolved is None:
            raise EngineUnavailable(
                f"engine accepted project {name!r} but will not return it"
            )
        return resolved

    async def get_project_stats(
        self,
        *,
        page: int = 1,
        size: int = 100,
        name: str | None = None,
        filters: Filters = None,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
        sorting: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/projects/stats",
            params=_params(
                page=page,
                size=size,
                name=name,
                filters=_json_param(filters),
                from_time=_iso(from_time),
                to_time=_iso(to_time),
                sorting=_json_param(sorting),
            ),
        )

    async def get_project_metrics(
        self,
        project_id: str,
        *,
        metric_type: str,
        interval: str,
        interval_start: dt.datetime | str,
        interval_end: dt.datetime | str | None = None,
        trace_filters: Filters = None,
        span_filters: Filters = None,
        thread_filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "POST",
            f"{API_ROOT}/projects/{project_id}/metrics",
            json_body=_body(
                metric_type=metric_type,
                interval=interval,
                interval_start=_iso(interval_start),
                interval_end=_iso(interval_end),
                trace_filters=trace_filters,
                span_filters=span_filters,
                thread_filters=thread_filters,
            ),
        )

    # -- traces ------------------------------------------------------------

    async def create_traces_batch(self, traces: Sequence[Mapping[str, Any]]) -> None:
        """Ingest path. Never retried: a replay would double-count telemetry."""
        await self._request(
            "POST", f"{API_ROOT}/traces/batch", json_body={"traces": list(traces)}
        )

    async def list_traces(
        self,
        *,
        page: int = 1,
        size: int = 50,
        project_id: str | None = None,
        project_name: str | None = None,
        filters: Filters = None,
        sorting: Filters = None,
        exclude: Ids = None,
        search: str | None = None,
        truncate: bool = False,
        strip_attachments: bool = False,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
        annotation_queue_id: str | None = None,
    ) -> JsonObject:
        """The paged read the trace tables render from."""
        return await self._request(
            "GET",
            f"{API_ROOT}/traces",
            params=_params(
                page=page,
                size=size,
                project_id=project_id,
                project_name=project_name,
                filters=_json_param(filters),
                sorting=_json_param(sorting),
                exclude=_json_param(exclude),
                search=search,
                truncate=truncate,
                strip_attachments=strip_attachments,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
                annotation_queue_id=annotation_queue_id,
            ),
        )

    async def search_traces(
        self,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        filters: Filters = None,
        limit: int = 500,
        last_retrieved_id: str | None = None,
        exclude: Ids = None,
        truncate: bool = True,
        strip_attachments: bool = False,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
    ) -> list[JsonObject]:
        """Cursor-paged export stream; capped at 2000 rows per call."""
        return await self._stream(
            f"{API_ROOT}/traces/search",
            _body(
                project_id=project_id,
                project_name=project_name,
                filters=filters,
                limit=limit,
                last_retrieved_id=last_retrieved_id,
                exclude=list(exclude) if exclude else None,
                truncate=truncate,
                strip_attachments=strip_attachments,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
            ),
        )

    async def get_trace(
        self, trace_id: str, *, strip_attachments: bool = False
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/traces/{trace_id}",
            params=_params(strip_attachments=strip_attachments),
        )

    async def get_trace_stats(
        self,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        filters: Filters = None,
        search: str | None = None,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/traces/stats",
            params=_params(
                project_id=project_id,
                project_name=project_name,
                filters=_json_param(filters),
                search=search,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
            ),
        )

    async def update_trace(self, trace_id: str, **fields: Any) -> None:
        await self._request(
            "PATCH", f"{API_ROOT}/traces/{trace_id}", json_body=_body(**fields)
        )

    async def delete_traces(
        self, ids: Sequence[str], *, project_id: str | None = None
    ) -> None:
        await self._request(
            "POST",
            f"{API_ROOT}/traces/delete",
            json_body=_body(ids=list(ids), project_id=project_id),
        )

    # -- spans -------------------------------------------------------------

    async def create_spans_batch(self, spans: Sequence[Mapping[str, Any]]) -> None:
        await self._request(
            "POST", f"{API_ROOT}/spans/batch", json_body={"spans": list(spans)}
        )

    async def list_spans(
        self,
        *,
        trace_id: str | None = None,
        page: int = 1,
        size: int = 100,
        project_id: str | None = None,
        project_name: str | None = None,
        span_type: str | None = None,
        filters: Filters = None,
        sorting: Filters = None,
        exclude: Ids = None,
        search: str | None = None,
        truncate: bool = False,
        strip_attachments: bool = False,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
    ) -> JsonObject:
        """Spans of one trace, or of a whole project when ``trace_id`` is None."""
        return await self._request(
            "GET",
            f"{API_ROOT}/spans",
            params=_params(
                trace_id=trace_id,
                page=page,
                size=size,
                project_id=project_id,
                project_name=project_name,
                type=span_type,
                filters=_json_param(filters),
                sorting=_json_param(sorting),
                exclude=_json_param(exclude),
                search=search,
                truncate=truncate,
                strip_attachments=strip_attachments,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
            ),
        )

    async def search_spans(
        self,
        *,
        trace_id: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
        span_type: str | None = None,
        filters: Filters = None,
        limit: int = 500,
        last_retrieved_id: str | None = None,
        exclude: Ids = None,
        truncate: bool = True,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
    ) -> list[JsonObject]:
        return await self._stream(
            f"{API_ROOT}/spans/search",
            _body(
                trace_id=trace_id,
                project_id=project_id,
                project_name=project_name,
                type=span_type,
                filters=filters,
                limit=limit,
                last_retrieved_id=last_retrieved_id,
                exclude=list(exclude) if exclude else None,
                truncate=truncate,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
            ),
        )

    async def get_span(
        self, span_id: str, *, strip_attachments: bool = False
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/spans/{span_id}",
            params=_params(strip_attachments=strip_attachments),
        )

    async def get_span_stats(
        self,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        trace_id: str | None = None,
        span_type: str | None = None,
        filters: Filters = None,
        search: str | None = None,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/spans/stats",
            params=_params(
                project_id=project_id,
                project_name=project_name,
                trace_id=trace_id,
                type=span_type,
                filters=_json_param(filters),
                search=search,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
            ),
        )

    # -- threads (Memory and State screen) ---------------------------------

    async def list_threads(
        self,
        *,
        page: int = 1,
        size: int = 50,
        project_id: str | None = None,
        project_name: str | None = None,
        filters: Filters = None,
        sorting: Filters = None,
        search: str | None = None,
        truncate: bool = False,
        strip_attachments: bool = False,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
        annotation_queue_id: str | None = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/traces/threads",
            params=_params(
                page=page,
                size=size,
                project_id=project_id,
                project_name=project_name,
                filters=_json_param(filters),
                sorting=_json_param(sorting),
                search=search,
                truncate=truncate,
                strip_attachments=strip_attachments,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
                annotation_queue_id=annotation_queue_id,
            ),
        )

    async def get_thread(
        self,
        thread_id: str,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        truncate: bool = False,
    ) -> JsonObject:
        """A thread is addressed by its business id, so it is read via POST."""
        return await self._request(
            "POST",
            f"{API_ROOT}/traces/threads/retrieve",
            json_body=_body(
                thread_id=thread_id,
                project_id=project_id,
                project_name=project_name,
                truncate=truncate,
            ),
        )

    async def search_threads(
        self,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        filters: Filters = None,
        limit: int = 500,
        last_retrieved_thread_model_id: str | None = None,
        truncate: bool = True,
        strip_attachments: bool = False,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
    ) -> list[JsonObject]:
        return await self._stream(
            f"{API_ROOT}/traces/threads/search",
            _body(
                project_id=project_id,
                project_name=project_name,
                filters=filters,
                limit=limit,
                last_retrieved_thread_model_id=last_retrieved_thread_model_id,
                truncate=truncate,
                strip_attachments=strip_attachments,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
            ),
        )

    async def get_thread_stats(
        self,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
        filters: Filters = None,
        search: str | None = None,
        from_time: dt.datetime | str | None = None,
        to_time: dt.datetime | str | None = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/traces/threads/stats",
            params=_params(
                project_id=project_id,
                project_name=project_name,
                filters=_json_param(filters),
                search=search,
                from_time=_iso(from_time),
                to_time=_iso(to_time),
            ),
        )

    # -- feedback scores ---------------------------------------------------

    async def score_traces_batch(self, scores: Sequence[Mapping[str, Any]]) -> None:
        await self._request(
            "PUT",
            f"{API_ROOT}/traces/feedback-scores",
            json_body={"scores": list(scores)},
        )

    async def score_spans_batch(self, scores: Sequence[Mapping[str, Any]]) -> None:
        await self._request(
            "PUT",
            f"{API_ROOT}/spans/feedback-scores",
            json_body={"scores": list(scores)},
        )

    async def score_threads_batch(self, scores: Sequence[Mapping[str, Any]]) -> None:
        await self._request(
            "PUT",
            f"{API_ROOT}/traces/threads/feedback-scores",
            json_body={"scores": list(scores)},
        )

    async def list_feedback_score_names(
        self,
        *,
        project_id: str | None = None,
        scope: str = "traces",
        span_type: str | None = None,
    ) -> JsonObject:
        """Distinct score names, used to build the evaluation column pickers.

        ``scope`` selects which entity the names are gathered from: traces,
        spans or threads.
        """
        paths = {
            "traces": f"{API_ROOT}/traces/feedback-scores/names",
            "spans": f"{API_ROOT}/spans/feedback-scores/names",
            "threads": f"{API_ROOT}/traces/threads/feedback-scores/names",
        }
        if scope not in paths:
            raise ValueError(f"unknown feedback score scope {scope!r}")
        return await self._request(
            "GET",
            paths[scope],
            params=_params(
                project_id=project_id,
                type=span_type if scope == "spans" else None,
            ),
        )

    # -- comments ----------------------------------------------------------

    async def add_trace_comment(self, trace_id: str, text: str) -> JsonObject:
        return await self._request(
            "POST", f"{API_ROOT}/traces/{trace_id}/comments", json_body={"text": text}
        )

    async def get_trace_comment(self, trace_id: str, comment_id: str) -> JsonObject:
        """Single-comment read.

        There is no list endpoint: comments come back embedded on the trace,
        so the review screens read them from ``get_trace``.
        """
        return await self._request(
            "GET", f"{API_ROOT}/traces/{trace_id}/comments/{comment_id}"
        )

    async def update_trace_comment(self, comment_id: str, text: str) -> None:
        await self._request(
            "PATCH", f"{API_ROOT}/traces/comments/{comment_id}", json_body={"text": text}
        )

    async def delete_trace_comments(self, ids: Sequence[str]) -> None:
        await self._request(
            "POST", f"{API_ROOT}/traces/comments/delete", json_body={"ids": list(ids)}
        )

    async def add_span_comment(self, span_id: str, text: str) -> JsonObject:
        return await self._request(
            "POST", f"{API_ROOT}/spans/{span_id}/comments", json_body={"text": text}
        )

    # -- prompts -----------------------------------------------------------

    async def list_prompts(
        self,
        *,
        page: int = 1,
        size: int = 50,
        name: str | None = None,
        project_id: str | None = None,
        sorting: Filters = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/prompts",
            params=_params(
                page=page,
                size=size,
                name=name,
                project_id=project_id,
                sorting=_json_param(sorting),
                filters=_json_param(filters),
            ),
        )

    async def create_prompt(
        self,
        name: str,
        *,
        description: str | None = None,
        template: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        tags: Sequence[str] | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> JsonObject:
        prompt_id = await self._create(
            f"{API_ROOT}/prompts",
            _body(
                name=name,
                description=description,
                template=template,
                metadata=metadata,
                tags=list(tags) if tags else None,
                project_id=project_id,
                project_name=project_name,
            ),
        )
        return await self.get_prompt(prompt_id) if prompt_id else {"name": name}

    async def get_prompt(
        self,
        prompt_id: str,
        *,
        mask_id: str | None = None,
        environment: str | None = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/prompts/{prompt_id}",
            params=_params(mask_id=mask_id, environment=environment),
        )

    async def create_version(
        self,
        name: str,
        version: Mapping[str, Any],
        *,
        template_structure: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> JsonObject:
        """Append a commit to a prompt, creating the prompt if it is new."""
        return await self._request(
            "POST",
            f"{API_ROOT}/prompts/versions",
            json_body=_body(
                name=name,
                version=dict(version),
                template_structure=template_structure,
                project_id=project_id,
                project_name=project_name,
            ),
        )

    async def list_versions(
        self,
        prompt_id: str,
        *,
        page: int = 1,
        size: int = 50,
        search: str | None = None,
        sorting: Filters = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/prompts/{prompt_id}/versions",
            params=_params(
                page=page,
                size=size,
                search=search,
                sorting=_json_param(sorting),
                filters=_json_param(filters),
            ),
        )

    async def get_version_by_commit(
        self,
        name: str,
        *,
        commit: str | None = None,
        environment: str | None = None,
        version_number: str | None = None,
        project_name: str | None = None,
    ) -> JsonObject:
        """Resolve one version. Commit, environment and version number are
        mutually exclusive coordinates onto the same row."""
        return await self._request(
            "POST",
            f"{API_ROOT}/prompts/versions/retrieve",
            json_body=_body(
                name=name,
                commit=commit,
                environment=environment,
                version_number=version_number,
                project_name=project_name,
            ),
        )

    async def restore_version(self, prompt_id: str, version_id: str) -> JsonObject:
        """Roll a prompt back by re-committing an earlier version as the head."""
        return await self._request(
            "POST", f"{API_ROOT}/prompts/{prompt_id}/versions/{version_id}/restore"
        )

    # -- datasets ----------------------------------------------------------

    async def list_datasets(
        self,
        *,
        page: int = 1,
        size: int = 50,
        name: str | None = None,
        project_id: str | None = None,
        prompt_id: str | None = None,
        with_experiments_only: bool = False,
        sorting: Filters = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/datasets",
            params=_params(
                page=page,
                size=size,
                name=name,
                project_id=project_id,
                prompt_id=prompt_id,
                with_experiments_only=with_experiments_only,
                sorting=_json_param(sorting),
                filters=_json_param(filters),
            ),
        )

    async def create_dataset(
        self,
        name: str,
        *,
        description: str | None = None,
        dataset_type: str | None = None,
        visibility: str | None = None,
        tags: Sequence[str] | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> JsonObject:
        dataset_id = await self._create(
            f"{API_ROOT}/datasets",
            _body(
                name=name,
                description=description,
                type=dataset_type,
                visibility=visibility,
                tags=list(tags) if tags else None,
                project_id=project_id,
                project_name=project_name,
            ),
        )
        return await self.get_dataset(dataset_id) if dataset_id else {"name": name}

    async def get_dataset(self, dataset_id: str) -> JsonObject:
        return await self._request("GET", f"{API_ROOT}/datasets/{dataset_id}")

    async def find_dataset_by_name(
        self, name: str, *, project_name: str | None = None
    ) -> JsonObject | None:
        try:
            return await self._request(
                "POST",
                f"{API_ROOT}/datasets/retrieve",
                json_body=_body(dataset_name=name, project_name=project_name),
            )
        except EngineNotFound:
            return None

    async def create_dataset_items_batch(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        dataset_id: str | None = None,
        dataset_name: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
        batch_group_id: str | None = None,
    ) -> None:
        """Upsert up to 1000 items. Idempotent on item id, hence PUT."""
        await self._request(
            "PUT",
            f"{API_ROOT}/datasets/items",
            json_body=_body(
                items=list(items),
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                project_id=project_id,
                project_name=project_name,
                batch_group_id=batch_group_id,
            ),
        )

    async def list_dataset_items(
        self,
        dataset_id: str,
        *,
        page: int = 1,
        size: int = 50,
        version: str | None = None,
        filters: Filters = None,
        truncate: bool = False,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/datasets/{dataset_id}/items",
            params=_params(
                page=page,
                size=size,
                version=version,
                filters=_json_param(filters),
                truncate=truncate,
            ),
        )

    async def stream_dataset_items(
        self,
        dataset_name: str,
        *,
        last_retrieved_id: str | None = None,
        limit: int = 2000,
        dataset_version: str | None = None,
        project_name: str | None = None,
        filters: Filters = None,
    ) -> list[JsonObject]:
        return await self._stream(
            f"{API_ROOT}/datasets/items/stream",
            _body(
                dataset_name=dataset_name,
                last_retrieved_id=last_retrieved_id,
                # The engine spells this field "steam_limit" on the wire.
                steam_limit=limit,
                dataset_version=dataset_version,
                project_name=project_name,
                filters=_json_param(filters),
            ),
        )

    async def delete_dataset_items(
        self,
        *,
        item_ids: Sequence[str] | None = None,
        dataset_id: str | None = None,
        filters: Filters = None,
        batch_group_id: str | None = None,
    ) -> None:
        await self._request(
            "POST",
            f"{API_ROOT}/datasets/items/delete",
            json_body=_body(
                item_ids=list(item_ids) if item_ids else None,
                dataset_id=dataset_id,
                filters=filters,
                batch_group_id=batch_group_id,
            ),
        )

    async def delete_dataset(self, dataset_id: str) -> None:
        await self._request("DELETE", f"{API_ROOT}/datasets/{dataset_id}")

    # -- experiments -------------------------------------------------------

    async def list_experiments(
        self,
        *,
        page: int = 1,
        size: int = 50,
        dataset_id: str | None = None,
        name: str | None = None,
        project_id: str | None = None,
        prompt_id: str | None = None,
        optimization_id: str | None = None,
        types: Ids = None,
        experiment_ids: Ids = None,
        sorting: Filters = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/experiments",
            params=_params(
                page=page,
                size=size,
                # The engine spells this one query parameter in camel case.
                datasetId=dataset_id,
                name=name,
                project_id=project_id,
                prompt_id=prompt_id,
                optimization_id=optimization_id,
                types=_json_param(types),
                experiment_ids=_json_param(experiment_ids),
                sorting=_json_param(sorting),
                filters=_json_param(filters),
            ),
        )

    async def create_experiment(
        self,
        name: str,
        *,
        dataset_name: str | None = None,
        experiment_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        prompt_versions: Sequence[Mapping[str, Any]] | None = None,
        optimization_id: str | None = None,
    ) -> JsonObject:
        experiment_id = await self._create(
            f"{API_ROOT}/experiments",
            _body(
                name=name,
                dataset_name=dataset_name,
                type=experiment_type,
                metadata=metadata,
                prompt_versions=list(prompt_versions) if prompt_versions else None,
                optimization_id=optimization_id,
            ),
        )
        return (
            await self.get_experiment(experiment_id) if experiment_id else {"name": name}
        )

    async def get_experiment(self, experiment_id: str) -> JsonObject:
        return await self._request("GET", f"{API_ROOT}/experiments/{experiment_id}")

    async def create_experiment_items_bulk(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        experiment_name: str,
        dataset_name: str,
        experiment_id: str | None = None,
        project_name: str | None = None,
    ) -> None:
        """Items plus their traces and spans in one call; body capped at 4 MB."""
        await self._request(
            "PUT",
            f"{API_ROOT}/experiments/items/bulk",
            json_body=_body(
                experiment_name=experiment_name,
                dataset_name=dataset_name,
                experiment_id=experiment_id,
                project_name=project_name,
                items=list(items),
            ),
        )

    async def list_experiment_groups(
        self,
        *,
        groups: Filters = None,
        types: Ids = None,
        name: str | None = None,
        project_id: str | None = None,
        project_deleted: bool | None = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/experiments/groups",
            params=_params(
                groups=_json_param(groups),
                types=_json_param(types),
                name=name,
                project_id=project_id,
                project_deleted=project_deleted,
                filters=_json_param(filters),
            ),
        )

    async def get_experiment_group_aggregations(
        self,
        *,
        groups: Filters = None,
        types: Ids = None,
        name: str | None = None,
        project_id: str | None = None,
        project_deleted: bool | None = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/experiments/groups/aggregations",
            params=_params(
                groups=_json_param(groups),
                types=_json_param(types),
                name=name,
                project_id=project_id,
                project_deleted=project_deleted,
                filters=_json_param(filters),
            ),
        )

    async def delete_experiments(self, ids: Sequence[str]) -> None:
        await self._request(
            "POST", f"{API_ROOT}/experiments/delete", json_body={"ids": list(ids)}
        )

    # -- guardrails --------------------------------------------------------
    #
    # The engine keeps two things apart: the *rules* (durable definitions,
    # edited on the Guardrails screen) and the *results* (one row per checked
    # entity, written by whatever ran the check).

    async def list_guardrails(
        self,
        *,
        project_id: str | None = None,
        name: str | None = None,
        page: int = 1,
        size: int = 50,
        sorting: Filters = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/automations/evaluators",
            params=_params(
                project_id=project_id,
                name=name,
                page=page,
                size=size,
                sorting=_json_param(sorting),
                filters=_json_param(filters),
            ),
        )

    async def get_guardrail(
        self, rule_id: str, *, project_id: str | None = None
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/automations/evaluators/{rule_id}",
            params=_params(project_id=project_id),
        )

    async def create_guardrail(self, definition: Mapping[str, Any]) -> str | None:
        return await self._create(
            f"{API_ROOT}/automations/evaluators", dict(definition)
        )

    async def update_guardrail(
        self, rule_id: str, definition: Mapping[str, Any]
    ) -> None:
        await self._request(
            "PATCH",
            f"{API_ROOT}/automations/evaluators/{rule_id}",
            json_body=dict(definition),
        )

    async def delete_guardrails(
        self, ids: Sequence[str], *, project_id: str | None = None
    ) -> None:
        await self._request(
            "POST",
            f"{API_ROOT}/automations/evaluators/delete",
            params=_params(project_id=project_id),
            json_body={"ids": list(ids)},
        )

    async def create_guardrail_results_batch(
        self, results: Sequence[Mapping[str, Any]]
    ) -> None:
        """Record up to 1000 check outcomes against traces or spans."""
        await self._request(
            "POST", f"{API_ROOT}/guardrails", json_body={"guardrails": list(results)}
        )

    async def evaluate_guardrails(
        self, text: str, validations: Sequence[Mapping[str, Any]]
    ) -> JsonObject:
        """Run the inline checks against a piece of text, synchronously.

        The checker is a separate internal service; when it is not addressed
        on its own base URL we assume the same gateway fronts it.
        """
        path = "/api/v1/guardrails/validations"
        target = f"{self._checker_base_url}{path}" if self._checker_base_url else path
        return await self._request(
            "POST",
            target,
            json_body={"text": text, "validations": list(validations)},
        )

    # -- alerts ------------------------------------------------------------

    async def list_alerts(
        self,
        *,
        page: int = 1,
        size: int = 50,
        sorting: Filters = None,
        filters: Filters = None,
    ) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/alerts",
            params=_params(
                page=page,
                size=size,
                sorting=_json_param(sorting),
                filters=_json_param(filters),
            ),
        )

    async def get_alert(self, alert_id: str) -> JsonObject:
        return await self._request("GET", f"{API_ROOT}/alerts/{alert_id}")

    async def create_alert(self, alert: Mapping[str, Any]) -> JsonObject:
        alert_id = await self._create(f"{API_ROOT}/alerts", dict(alert))
        return await self.get_alert(alert_id) if alert_id else dict(alert)

    async def update_alert(self, alert_id: str, alert: Mapping[str, Any]) -> None:
        await self._request(
            "PUT", f"{API_ROOT}/alerts/{alert_id}", json_body=dict(alert)
        )

    async def delete_alerts(self, ids: Sequence[str]) -> None:
        await self._request(
            "POST", f"{API_ROOT}/alerts/delete", json_body={"ids": list(ids)}
        )

    async def test_alert_webhook(self, alert: Mapping[str, Any]) -> JsonObject:
        """Fire a synthetic delivery so an operator can prove the hook works."""
        return await self._request(
            "POST", f"{API_ROOT}/alerts/webhooks/tests", json_body=dict(alert)
        )

    async def get_webhook_examples(self, *, alert_type: str | None = None) -> JsonObject:
        return await self._request(
            "GET",
            f"{API_ROOT}/alerts/webhooks/examples",
            params=_params(alert_type=alert_type),
        )

    # -- costs and usage ---------------------------------------------------

    async def get_cost_summary(
        self,
        *,
        interval_start: dt.datetime | str,
        interval_end: dt.datetime | str,
        project_ids: Ids = None,
    ) -> JsonObject:
        """Total spend over a window; backs the cost tiles."""
        return await self._request(
            "POST",
            f"{API_ROOT}/workspaces/costs/summaries",
            json_body=_body(
                project_ids=list(project_ids) if project_ids else None,
                interval_start=_iso(interval_start),
                interval_end=_iso(interval_end),
            ),
        )

    async def get_cost_series(
        self,
        *,
        interval_start: dt.datetime | str,
        interval_end: dt.datetime | str,
        project_ids: Ids = None,
    ) -> JsonObject:
        """Daily spend, for the cost trend chart."""
        return await self._request(
            "POST",
            f"{API_ROOT}/workspaces/costs",
            json_body=_body(
                project_ids=list(project_ids) if project_ids else None,
                name="cost",
                interval_start=_iso(interval_start),
                interval_end=_iso(interval_end),
            ),
        )

    async def get_workspace_usage(
        self,
        *,
        metric_type: str,
        interval: str,
        interval_start: dt.datetime | str,
        interval_end: dt.datetime | str | None = None,
        project_ids: Ids = None,
        filters: Filters = None,
    ) -> JsonObject:
        """Token and volume series aggregated across the workspace."""
        return await self._request(
            "POST",
            f"{API_ROOT}/workspaces/metrics/spans",
            json_body=_body(
                project_ids=list(project_ids) if project_ids else None,
                metric_type=metric_type,
                interval=interval,
                interval_start=_iso(interval_start),
                interval_end=_iso(interval_end),
                filters=filters,
            ),
        )

    async def list_token_usage_names(self, *, project_ids: Ids = None) -> JsonObject:
        """Distinct token-usage keys, used to label the usage breakdown."""
        return await self._request(
            "POST",
            f"{API_ROOT}/workspaces/token-usage/names",
            json_body={"project_ids": list(project_ids) if project_ids else []},
        )
