"""Typed application errors and the single JSON error envelope the API returns.

Every failure the client can see has a stable machine-readable ``code`` so the
web app and the SDKs can branch on it without string matching.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Base class for every deliberate, client-visible failure."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "Request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        if code:
            self.code = code
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {"code": self.code, "message": self.message},
        }
        if self.details:
            body["error"]["details"] = self.details
        if request_id:
            body["error"]["request_id"] = request_id
        return body


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"
    message = "One or more fields are invalid."


class Unauthenticated(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    message = "Valid credentials are required."


class PermissionDenied(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "Your role does not permit this action."


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource does not exist."


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The resource is in a conflicting state."


class PreconditionFailed(AppError):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    code = "precondition_failed"
    message = "A required precondition was not met."


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "The request body exceeds the maximum accepted size."


class RateLimited(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Slow down and retry."

    def __init__(self, retry_after_seconds: int = 60, **kw: Any) -> None:
        super().__init__(**kw)
        self.retry_after_seconds = retry_after_seconds


class TelemetryBackendUnavailable(AppError):
    """The private telemetry store could not be reached.

    Deliberately named for what it is to the caller; it never names an
    upstream product.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "telemetry_unavailable"
    message = "The telemetry store is temporarily unavailable."
    #: Sent as ``Retry-After``. A slow store is made slower by everything that
    #: asks again at once -- the console's pollers, an SDK's flush loop -- and
    #: both already honour this header, so say how long to stay away.
    retry_after_seconds: int = 5


class ServiceBusy(AppError):
    """This process could not get a database connection in time.

    Every pooled connection is checked out -- usually by requests waiting on
    something slow -- and the wait for a free one (``database_pool_timeout_
    seconds``) ran out. That used to surface as an opaque 500, which reads as a
    bug and is not retried; it is load, it passes, and the caller should simply
    come back.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "busy"
    message = "The service is busy. Retry shortly."
    retry_after_seconds: int = 2


class ModelUnavailable(AppError):
    """A model this service calls on the caller's behalf could not answer.

    Distinct from ``telemetry_unavailable``: that one means the store behind
    the telemetry screens is down, this one means an outbound model call failed
    or was never configured. Keeping them apart matters because the remedies
    are completely different.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "model_unavailable"
    message = "The model could not be reached."


class QuotaExceeded(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "quota_exceeded"
    message = "The workspace has exhausted its entitlement for this resource."


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    headers = {}
    # Any error that knows when it is worth coming back says so: the 429, and
    # the two 503s that mean "slow right now" rather than "broken".
    retry_after = getattr(exc, "retry_after_seconds", None)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(_request_id(request)),
        headers=headers,
    )


async def database_busy_handler(request: Request, exc: Exception) -> JSONResponse:
    """The connection pool's checkout timeout, answered as the 503 it is.

    Registered for ``sqlalchemy.exc.TimeoutError`` in ``main``. It is not an
    ``AppError`` -- the pool raises it from inside whichever dependency or
    service touched the session first -- so it is translated here, once.
    """
    return await app_error_handler(request, ServiceBusy())


async def engine_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """An engine failure no service translated, answered as what it is.

    Registered for ``EngineError`` in ``main``. Every service is supposed to map
    the adapter's errors onto this API's own -- most do -- but one that forgets
    lets the raw exception through, and the catch-all below turns that into a
    500 "internal error": the store being down reported as *our* bug, with no
    ``Retry-After``, on a screen that would otherwise have said "telemetry is
    unavailable, try again". This is the net under all of them.

    Imported here rather than at module top: ``engine`` reads ``core.config``,
    and this module must stay importable by both.
    """
    from ..engine import EngineNotFound, EngineUnavailable

    if isinstance(exc, EngineNotFound):
        return await app_error_handler(request, NotFound("That item does not exist."))
    if isinstance(exc, EngineUnavailable):
        return await app_error_handler(request, TelemetryBackendUnavailable())
    return await app_error_handler(
        request,
        TelemetryBackendUnavailable(
            "The telemetry store rejected the request.", code="telemetry_rejected"
        ),
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {
        400: "bad_request",
        401: "unauthenticated",
        403: "permission_denied",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        429: "rate_limited",
    }
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": code_map.get(exc.status_code, "http_error"),
                "message": str(exc.detail),
                "request_id": _request_id(request),
            }
        },
        headers=getattr(exc, "headers", None),
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    fields = [
        {
            "field": ".".join(str(p) for p in err.get("loc", ())[1:]) or "body",
            "message": err.get("msg", "invalid"),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_failed",
                "message": "One or more fields are invalid.",
                "details": {"fields": fields},
                "request_id": _request_id(request),
            }
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Body intentionally opaque: internals never leak to clients.
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
                "request_id": _request_id(request),
            }
        },
    )
