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
import contextlib
import datetime as dt
import json
import logging
import random
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
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

#: Never rebuild the connection pool more often than this, however many calls
#: notice it is full at once.
_RECYCLE_MIN_INTERVAL_SECONDS: Final[float] = 30.0
_ERROR_BODY_LIMIT: Final[int] = 2048

#: A stream body larger than this is parsed on a worker thread (see ``_stream``).
#: Below it the hop to a thread costs more than the parse it would move.
_OFF_LOOP_DECODE_BYTES: Final[int] = 256 * 1024

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


@contextlib.asynccontextmanager
async def deadline(
    seconds: float | None = None, *, what: str = "telemetry read"
) -> AsyncIterator[None]:
    """Bound everything one request asks of the engine, taken together.

    Per-call timeouts bound one exchange. A screen makes many -- a page per
    project, a series per metric, some of them one after another -- and nothing
    bounded their sum, so a slow store held a request (and the worker and the
    database connection under it) long after the console stopped listening at
    30 s. Wrap the *engine* fan-out in this and the request answers a typed
    ``EngineTimeout`` (the API's 503) inside ``engine_fanout_deadline_seconds``::

        async with deadline():
            pages = await asyncio.gather(*(scan(p) for p in projects))

    Expiry cancels whatever is awaited inside. Exchanges already on the wire
    are shielded (see ``EngineClient._exchange``) and finish under their own
    timeouts; what stops is every further page and every call still queued
    behind a semaphore. Keep SQL on the request's session OUTSIDE the block: a
    statement cancelled half way invalidates its connection.
    """
    limit = settings.engine_fanout_deadline_seconds if seconds is None else seconds
    try:
        async with asyncio.timeout(limit) as scope:
            yield
    except TimeoutError as exc:
        if not scope.expired():
            raise  # somebody else's timeout, passing through
        raise EngineTimeout(f"{what} did not finish within {limit:g}s") from exc


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
        read_timeout_seconds: float | None = None,
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

        self._headers = headers
        self._max_connections = max_connections or settings.engine_max_connections
        self._timeout_seconds = timeout_seconds or settings.engine_timeout_seconds
        self._connect_timeout_seconds = (
            connect_timeout_seconds or settings.engine_connect_timeout_seconds
        )
        # The leash on an aggregate a screen is waiting for; never longer than
        # the client-wide timeout it is an exception to.
        self._read_timeout_seconds = min(
            read_timeout_seconds or settings.engine_read_timeout_seconds,
            self._timeout_seconds,
        )
        self._transport = transport
        self._client = self._build_http_client()

        #: Exchanges still running after their caller went away. Held so they
        #: are not garbage-collected mid-flight and so shutdown can wait on them.
        self._inflight: set[asyncio.Future[httpx.Response]] = set()
        self._retiring: set[asyncio.Task[None]] = set()
        self._recycles = 0
        self._last_recycle_at = 0.0

    def _build_http_client(self) -> httpx.AsyncClient:
        pool = self._max_connections
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            limits=httpx.Limits(
                max_connections=pool,
                max_keepalive_connections=max(1, pool // 4),
                keepalive_expiry=30.0,
            ),
            timeout=httpx.Timeout(
                self._timeout_seconds,
                connect=self._connect_timeout_seconds,
                # Waiting for a free pooled connection is not the engine being
                # slow, it is this process being out of connections. Thirty
                # seconds of that per request is how one wedged pool turns into
                # a wedged worker, so fail fast and let the pool heal itself.
                pool=settings.engine_pool_timeout_seconds,
            ),
            transport=self._transport,
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
        if self._inflight:
            # Let orphaned exchanges finish so their connections are released
            # rather than torn down underneath them.
            await asyncio.wait(list(self._inflight), timeout=5.0)
        for task in list(self._retiring):
            task.cancel()
        await self._client.aclose()

    def pool_stats(self) -> dict[str, int]:
        """Connection-pool occupancy, for the health endpoint.

        Reads the transport's private pool, so every access is guarded: a
        library upgrade that moves it must degrade this to zeros, never break
        a health check.
        """
        connections: list[Any] = []
        try:
            pool = self._client._transport._pool  # type: ignore[attr-defined]  # noqa: SLF001
            connections = list(pool.connections)
            idle = sum(1 for connection in connections if connection.is_idle())
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            idle = 0
        return {
            "connections": len(connections),
            "idle": idle,
            "busy": len(connections) - idle,
            "limit": self._max_connections,
            "orphaned_exchanges": len(self._inflight),
            "recycles": self._recycles,
        }

    def _recycle_pool(self, reason: str) -> None:
        """Swap in a fresh connection pool and retire the old one.

        The last line of defence. If connections are ever leaked faster than
        they are released, the pool fills, and every later call in this process
        fails until it restarts. Rebuilding the pool bounds that to seconds:
        new calls use the new pool immediately, and the old one is closed --
        which force-closes whatever it leaked -- once anything legitimately
        still using it has had time to finish.
        """
        if self._transport is not None:
            return  # an injected transport (tests) is not ours to rebuild
        now = asyncio.get_running_loop().time()
        if now - self._last_recycle_at < _RECYCLE_MIN_INTERVAL_SECONDS:
            return
        self._last_recycle_at = now
        self._recycles += 1
        retired, self._client = self._client, self._build_http_client()
        logger.error(
            "engine connection pool recycled (%s); recycle #%d for this process",
            reason,
            self._recycles,
        )

        async def _retire() -> None:
            await asyncio.sleep(self._timeout_seconds + 5.0)
            await retired.aclose()

        task = asyncio.ensure_future(_retire())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

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

    async def _exchange(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One HTTP exchange that its caller's cancellation cannot interrupt.

        A cancelled caller must not tear an exchange down half way. When the
        live-runs stream is dropped by the browser, its generator is cancelled
        in the middle of a scan with a dozen engine calls in flight, and the
        pooled connections under those calls are left marked busy forever --
        never released, never closed. A hundred of those and the pool is full:
        every later call in the process waits out the pool timeout and fails,
        and the worker answers 503 until it is restarted.

        So the exchange runs as its own task and the caller waits on a shield.
        Cancelling the caller abandons the *wait*; the exchange itself runs to
        completion -- bounded by the ordinary timeouts -- and hands its
        connection back the normal way.
        """
        exchange = asyncio.ensure_future(self._client.request(method, path, **kwargs))
        self._inflight.add(exchange)
        exchange.add_done_callback(self._settle)
        return await asyncio.shield(exchange)

    def _settle(self, exchange: asyncio.Future[httpx.Response]) -> None:
        self._inflight.discard(exchange)
        if not exchange.cancelled():
            # Retrieve the outcome so an exchange nobody is waiting for any
            # more does not log "exception was never retrieved".
            exchange.exception()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        retries: int | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        """Issue one call, retrying only what is safe -- and useful -- to replay.

        Connect failures and 5xx are retried on GET/PUT/DELETE; POST is never
        retried because the engine's POSTs create telemetry.

        A read timeout is never retried, on any verb. The request was delivered
        and the engine is still working on it: there is no cancel on this wire,
        so replaying it starts a second copy of the same heavy query beside the
        first, at the moment the store has least to spare. Three 30 s attempts
        are how one slow aggregate became a 90 s request that the console had
        given up on after 30. The caller is told once, and promptly.
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
            extra: dict[str, Any] = {}
            if timeout_seconds is not None:
                extra["timeout"] = httpx.Timeout(
                    timeout_seconds,
                    connect=min(timeout_seconds, self._connect_timeout_seconds),
                    pool=min(timeout_seconds, settings.engine_pool_timeout_seconds),
                )
            try:
                response = await self._exchange(
                    method,
                    path,
                    params=dict(params) if params else None,
                    json=json_body,
                    headers={ENGINE_REQUEST_ID_HEADER: request_id, **(headers or {})},
                    **extra,
                )
            except httpx.PoolTimeout as exc:
                # Not the engine: this process ran out of connections to it.
                # Retrying would only queue behind the same full pool, so fail
                # now and rebuild the pool for whoever calls next.
                self._recycle_pool("no free connection within the pool timeout")
                raise EngineUnavailable(
                    f"no free connection to the engine for {method} {path}"
                ) from exc
            except httpx.ConnectTimeout as exc:
                # Nothing was delivered, so there is nothing to duplicate: this
                # is the engine restarting or a network blip, and worth another
                # go. (Listed before its parent class, which is not.)
                last_error = EngineTimeout(
                    f"engine did not accept a connection for {method} {path}"
                )
                last_cause = exc
            except httpx.TimeoutException as exc:
                raise EngineTimeout(f"engine timed out on {method} {path}") from exc
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
        timeout_seconds: float | None = None,
    ) -> Any:
        response = await self._send(
            method,
            path,
            params=params,
            json_body=json_body,
            retries=retries,
            timeout_seconds=timeout_seconds,
        )
        return _decode(response)

    def _screen_read(self, timeout_seconds: float | None) -> float:
        """The timeout for an aggregate a person is waiting on.

        One 30 s timeout used to cover everything from an ingest write to a
        KPI tile. The rollups behind a screen -- stats, metric series, cost --
        default to ``engine_read_timeout_seconds`` instead; a caller that is
        not a screen (an export, an evaluation) says so by passing its own.
        """
        return self._read_timeout_seconds if timeout_seconds is None else timeout_seconds

    async def _stream(
        self,
        path: str,
        payload: JsonObject,
        *,
        timeout_seconds: float | None = None,
    ) -> list[JsonObject]:
        """Call one of the engine's streaming search endpoints.

        These do not return a JSON document: they return newline-delimited JSON
        as an octet stream. The client's default ``Accept: application/json``
        makes the engine refuse the request outright with 406 Not Acceptable —
        the search never runs, so the failure looks like an empty result rather
        than a rejected one until you read the engine's access log.

        ``timeout_seconds`` is left to the caller: the same search feeds a
        screen's scan (which should pass ``settings.engine_read_timeout_seconds``)
        and an export paging 2000 rows at a time (which should not).
        """
        response = await self._send(
            "POST",
            path,
            json_body=payload,
            headers={"accept": "application/octet-stream"},
            timeout_seconds=timeout_seconds,
        )
        if len(response.content) > _OFF_LOOP_DECODE_BYTES:
            # An untruncated page of 500 spans is megabytes of prompt and
            # completion JSON, and parsing it row by row here held the event
            # loop -- every other request on this worker -- until the last row.
            # On a thread the parse gives the interpreter back between rows.
            return await asyncio.to_thread(_decode_lines, response)
        return _decode_lines(response)

    async def _create(self, path: str, payload: JsonObject) -> str | None:
        """POST a create and return the new id read off Location."""
        response = await self._send("POST", path, json_body=payload)
        return _id_from_location(response)

    # -- health ------------------------------------------------------------

    async def health(self, *, timeout_seconds: float = 3.0) -> bool:
        """Liveness only -- a probe must fail fast, so it never retries.

        A probe that can take the full request timeout is worse than none: the
        container healthcheck and every uptime monitor hang with it.
        """
        try:
            payload = await self._request(
                "GET", HEALTH_PATH, retries=0, timeout_seconds=timeout_seconds
            )
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
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> JsonObject:
        # ``retries`` is for a caller racing a deadline of its own: a refused
        # connection or a 5xx is otherwise re-dialled with backoff, which a scan
        # that must answer in seconds would rather hear about at once.
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
            retries=retries,
            timeout_seconds=self._screen_read(timeout_seconds),
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=self._screen_read(timeout_seconds),
        )

    # -- traces ------------------------------------------------------------

    async def create_traces_batch(
        self,
        traces: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> None:
        """Ingest path. Never retried: a replay would double-count telemetry.

        The five batch writes (traces, spans and the three score targets) share
        one signature so ingest can drive any of them the same way. Ingest
        answers its reporter inside a budget and the reporter is the retry
        layer, so it passes a ``timeout_seconds`` well under the client-wide
        one: an exchange is shielded from its caller's cancellation, and a write
        ingest has already answered 503 for should not go on occupying the store
        for the rest of thirty seconds. ``retries`` only means something on the
        score PUTs; a POST is sent once whatever is asked.
        """
        await self._request(
            "POST",
            f"{API_ROOT}/traces/batch",
            json_body={"traces": list(traces)},
            retries=retries,
            timeout_seconds=timeout_seconds,
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=timeout_seconds,
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=self._screen_read(timeout_seconds),
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

    async def create_spans_batch(
        self,
        spans: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> None:
        """See :meth:`create_traces_batch` for the two keyword arguments."""
        await self._request(
            "POST",
            f"{API_ROOT}/spans/batch",
            json_body={"spans": list(spans)},
            retries=retries,
            timeout_seconds=timeout_seconds,
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
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> JsonObject:
        """Spans of one trace, or of a whole project when ``trace_id`` is None.

        A page already on the wire outlives its caller (exchanges are shielded),
        so a scan with a deadline of its own passes that deadline as
        ``timeout_seconds`` -- and ``retries=0`` -- or the store goes on
        answering for the client-wide timeout after the screen has been served.
        """
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
            retries=retries,
            timeout_seconds=timeout_seconds,
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=timeout_seconds,
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=self._screen_read(timeout_seconds),
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=timeout_seconds,
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=self._screen_read(timeout_seconds),
        )

    # -- feedback scores ---------------------------------------------------

    # A score write is a PUT, so unlike the creates it IS re-sent after a refused
    # connection or a 5xx. A caller that is itself retried from outside (ingest:
    # the SDK re-sends the batch) passes ``retries=0`` so the two layers do not
    # multiply, and ``timeout_seconds`` for the reason create_traces_batch gives.

    async def score_traces_batch(
        self,
        scores: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> None:
        await self._request(
            "PUT",
            f"{API_ROOT}/traces/feedback-scores",
            json_body={"scores": list(scores)},
            retries=retries,
            timeout_seconds=timeout_seconds,
        )

    async def score_spans_batch(
        self,
        scores: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> None:
        await self._request(
            "PUT",
            f"{API_ROOT}/spans/feedback-scores",
            json_body={"scores": list(scores)},
            retries=retries,
            timeout_seconds=timeout_seconds,
        )

    async def score_threads_batch(
        self,
        scores: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> None:
        await self._request(
            "PUT",
            f"{API_ROOT}/traces/threads/feedback-scores",
            json_body={"scores": list(scores)},
            retries=retries,
            timeout_seconds=timeout_seconds,
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

    async def list_dataset_items_with_experiments(
        self,
        dataset_id: str,
        experiment_ids: Sequence[str],
        *,
        page: int = 1,
        size: int = 50,
        truncate: bool = True,
    ) -> JsonObject:
        """Dataset items joined to their experiment items, one page at a time.

        The plain items listing carries no experiment results; this comparison
        endpoint is the one that answers "how did this experiment do on each
        case", which is what the suite runner grades from.
        """
        return await self._request(
            "GET",
            f"{API_ROOT}/datasets/{dataset_id}/items/experiments/items",
            params=_params(
                experiment_ids=json.dumps(list(experiment_ids)),
                page=page,
                size=size,
                truncate=truncate,
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

    async def create_experiment_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        """Link existing traces to experiment items, one row per case.

        Unlike the bulk endpoint, this one references traces that already live
        in the store — which is how the platform's own evaluation runs pick up
        the traces (and scores) an SDK experiment produced for the same
        dataset. Every item id must be a version 7 UUID; the engine refuses
        anything else.
        """
        await self._request(
            "POST",
            f"{API_ROOT}/experiments/items",
            json_body={"experiment_items": list(items)},
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
        self,
        text: str,
        validations: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
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
            timeout_seconds=timeout_seconds,
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=self._screen_read(timeout_seconds),
        )

    async def get_cost_series(
        self,
        *,
        interval_start: dt.datetime | str,
        interval_end: dt.datetime | str,
        project_ids: Ids = None,
        timeout_seconds: float | None = None,
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
            timeout_seconds=self._screen_read(timeout_seconds),
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
        timeout_seconds: float | None = None,
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
            timeout_seconds=self._screen_read(timeout_seconds),
        )

    async def list_token_usage_names(self, *, project_ids: Ids = None) -> JsonObject:
        """Distinct token-usage keys, used to label the usage breakdown."""
        return await self._request(
            "POST",
            f"{API_ROOT}/workspaces/token-usage/names",
            json_body={"project_ids": list(project_ids) if project_ids else []},
        )
