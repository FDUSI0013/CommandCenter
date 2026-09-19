"""Fulcrum Ops control plane — application factory.

This service is the only publicly reachable component. It owns identity,
tenancy, governance state and the public API contract; the telemetry engine it
talks to sits on a private network and is never addressable from outside.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import re
from collections.abc import Awaitable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import TimeoutError as DatabasePoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.config import settings
from .core.errors import (
    AppError,
    app_error_handler,
    database_busy_handler,
    http_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from .core.logging import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
)
from .db import session as db_session
from .engine import EngineClient, set_engine_client

log = logging.getLogger("fulcrum_ops")

#: Ceiling on each dependency check behind /health. The container healthcheck
#: allows 10 s and an uptime monitor rather less; a probe has to answer inside
#: that even when -- especially when -- something behind it is hanging.
HEALTH_CHECK_SECONDS = 3.0

DESCRIPTION = """
The Fulcrum Ops control plane API.

Agents report telemetry through the ingest endpoints — directly, via the
Fulcrum Ops SDKs, or over OpenTelemetry — and every governance surface in the
console reads and writes through the same contract.

Authenticate with an API key (`Authorization: Bearer fo_…`) for programmatic
access, or with a session token issued by `POST /api/v1/auth/login` for the console.
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level, settings.log_json)

    problems = settings.require_production_hardening()
    if problems:
        for p in problems:
            log.error("refusing to start: %s", p)
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))

    client = EngineClient(
        base_url=settings.engine_base_url,
        workspace=settings.engine_workspace,
        api_key=settings.engine_api_key,
        checker_base_url=settings.engine_checker_url,
    )
    set_engine_client(client)

    healthy = await client.health()
    if not healthy:
        msg = "telemetry engine is not reachable at startup"
        if settings.engine_required and not settings.is_local:
            log.error(msg)
        else:
            log.warning("%s — telemetry features will degrade", msg)

    from .services import scheduler

    scheduler_task = scheduler.start()

    log.info(
        "control plane ready (env=%s, db=%s)",
        settings.environment,
        settings.database_url.split("://", 1)[0],
    )
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        await client.aclose()
        await db_session.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fulcrum Ops API",
        description=DESCRIPTION,
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-Total-Count"],
    )

    app.add_exception_handler(AppError, app_error_handler)
    # A full database pool is load, not a bug: 503 "busy" with Retry-After
    # rather than the opaque 500 the catch-all below would make of it.
    app.add_exception_handler(DatabasePoolTimeout, database_busy_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    from .api.otlp import router as otlp_router
    from .api.v1 import router as v1_router

    app.include_router(v1_router, prefix=settings.api_prefix)
    # OTLP/HTTP has a fixed path in the specification, so it is mounted at the
    # root rather than under the versioned prefix: an OpenTelemetry exporter
    # configured with our base URL must find /v1/traces exactly where it expects.
    app.include_router(otlp_router)

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        from .engine import get_engine_client

        async def bounded(check: Awaitable[bool]) -> bool:
            """A dependency that does not answer in time is down, not pending."""
            try:
                return bool(await asyncio.wait_for(check, timeout=HEALTH_CHECK_SECONDS))
            except Exception:  # noqa: BLE001 - a probe reports, it never raises
                return False

        # Together, not in turn: the probe's worst case is one timeout, not two.
        engine = get_engine_client()
        db_ok, engine_ok = await asyncio.gather(
            bounded(db_session.ping()),
            bounded(engine.health(timeout_seconds=HEALTH_CHECK_SECONDS)),
        )
        ok = db_ok and (engine_ok or not settings.engine_required)
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ok" if ok else "degraded",
                "version": app.version,
                "environment": settings.environment,
                "checks": {"database": db_ok, "telemetry": engine_ok},
                "engine_pool": engine.pool_stats(),
            },
        )

    # Mounted last: a mount at the root matches every path, so it must sit
    # behind the API and health routes rather than in front of them.
    _mount_console(app)

    return app


def _mount_console(app: FastAPI) -> None:
    """Serve the console's static files, when this process is asked to.

    The console is a single page with a hash router, so any path that is not a
    file on disk resolves to ``index.html``; only ``/api`` and ``/health`` are
    excluded, and those are already registered above and therefore match first.
    """
    if not settings.static_dir:
        return

    directory = pathlib.Path(settings.static_dir).resolve()
    if not (directory / "index.html").is_file():
        log.warning("static_dir %s has no index.html; not serving the console", directory)
        return

    index = directory / "index.html"
    build_id = _console_build_id(directory)

    def stamped_index() -> HTMLResponse:
        """index.html with a build stamp on every asset it references.

        The console has no build step, so its files keep fixed names — js/app.js,
        not js/app.<hash>.js. A browser therefore cannot tell a cached copy from
        the current one by URL, and after a deploy it will happily keep running
        yesterday's JavaScript against today's API. Appending ?v=<build id> makes
        the URL change whenever any asset changes, which is what actually
        invalidates the cache; `Cache-Control: no-cache` below is the belt to
        this pair of braces.
        """
        html = index.read_text(encoding="utf-8")
        html = re.sub(
            r'((?:src|href)="(?:js|css)/[^"?]+)"',
            lambda m: f'{m.group(1)}?v={build_id}"',
            html,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    class SpaFiles(StaticFiles):
        async def get_response(self, path: str, scope):  # noqa: ANN001, ANN201
            # Every path that is not a file on disk is the single page, and the
            # page is always served stamped.
            if path in ("", ".", "index.html"):
                return stamped_index()
            response = await super().get_response(path, scope)
            if response.status_code == 404:
                return stamped_index()
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/", SpaFiles(directory=str(directory), html=True), name="console")
    log.info("serving the console from %s (build %s)", directory, build_id)


def _console_build_id(directory: pathlib.Path) -> str:
    """A short digest over the console's assets: their names, sizes and mtimes.

    Recomputed at start-up, which is exactly when a deployment changes them.
    """
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(path.name.encode())
            digest.update(str(stat.st_size).encode())
            digest.update(str(int(stat.st_mtime)).encode())
    return digest.hexdigest()[:12]


app = create_app()
