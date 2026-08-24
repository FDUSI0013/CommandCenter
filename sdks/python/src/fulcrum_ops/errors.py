"""Errors the SDK raises, and the translation from the API's error envelope.

Two rules govern everything here:

1. **Telemetry never raises into the caller's path.** These exceptions surface
   from the calls a developer explicitly awaits — :meth:`FulcrumOps.flush`,
   :meth:`FulcrumOps.config`, :meth:`FulcrumOps.get_prompt` — and are handed to
   the ``on_error`` hook everywhere else. A ``@trace``-decorated function that
   ran fine returns its value even when reporting it failed.
2. **Every error says whether retrying could help.** ``retryable`` is what the
   flusher's backoff loop branches on, so that judgement lives with the error
   rather than being re-derived at each call site.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Type

__all__ = [
    "FulcrumOpsError",
    "ConfigurationError",
    "ApiError",
    "AuthenticationError",
    "PermissionDeniedError",
    "QuotaExceededError",
    "EntitlementError",
    "ValidationError",
    "NotFoundError",
    "PayloadTooLargeError",
    "RateLimitError",
    "ServerError",
    "TelemetryUnavailableError",
    "TransportError",
    "NetworkError",
    "TimeoutError",
    "error_from_response",
    "to_fulcrum_error",
]


class FulcrumOpsError(Exception):
    """Base class for everything the SDK raises."""

    #: Default machine-readable code for the subclass.
    default_code = "sdk_error"
    #: Whether repeating the request unchanged could plausibly succeed.
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        retryable: Optional[bool] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.status = status
        self.request_id = request_id
        self.details = details or {}
        self.retryable = self.default_retryable if retryable is None else retryable
        self.retry_after_seconds = retry_after_seconds

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.message]
        if self.status is not None:
            parts.append("HTTP {0}".format(self.status))
        if self.code and self.code != self.default_code:
            parts.append(self.code)
        if self.request_id:
            parts.append("request {0}".format(self.request_id))
        head, tail = parts[0], parts[1:]
        return head if not tail else "{0} ({1})".format(head, ", ".join(tail))


class ConfigurationError(FulcrumOpsError):
    """The SDK was constructed or called with settings it cannot work with."""

    default_code = "configuration_error"


class ApiError(FulcrumOpsError):
    """A non-2xx response from the control plane."""

    default_code = "api_error"


class AuthenticationError(ApiError):
    """401 — the API key is missing, malformed, expired or revoked."""

    default_code = "unauthenticated"


class PermissionDeniedError(ApiError):
    """403 — the key is valid but lacks the scope this call needs."""

    default_code = "permission_denied"


class QuotaExceededError(ApiError):
    """402/429-with-quota — the workspace is out of entitlement for this resource.

    Not retryable on its own: the batch will keep failing until an operator
    raises the quota or the window resets, so retrying only burns the queue.
    """

    default_code = "quota_exceeded"


#: The licensing surface calls this an entitlement; the quota surface calls it a
#: quota. Same failure, so the same class under both names.
EntitlementError = QuotaExceededError


class ValidationError(ApiError):
    """400/422 — the request body did not satisfy the ingest contract."""

    default_code = "validation_failed"


class NotFoundError(ApiError):
    """404 — the addressed prompt, dataset or evaluation does not exist."""

    default_code = "not_found"


class PayloadTooLargeError(ApiError):
    """413 — the batch body exceeded what the endpoint accepts.

    The flusher responds by halving the batch and trying again rather than by
    dropping it, so a single oversized trace does not take its neighbours down.
    """

    default_code = "payload_too_large"


class RateLimitError(ApiError):
    """429 — slow down. ``retry_after_seconds`` says by how much."""

    default_code = "rate_limited"
    default_retryable = True


class ServerError(ApiError):
    """5xx from the control plane."""

    default_code = "server_error"
    default_retryable = True


class TelemetryUnavailableError(ServerError):
    """503 ``telemetry_unavailable`` — the control plane is up, its store is not.

    Its own class because it is the one server failure a customer will actually
    see during a staged rollout, and "your telemetry engine is not reachable"
    is a far more actionable message than "HTTP 503".
    """

    default_code = "telemetry_unavailable"
    default_retryable = True


class TransportError(FulcrumOpsError):
    """The request never produced a response."""

    default_code = "transport_error"
    default_retryable = True


class NetworkError(TransportError):
    """DNS, TLS, connection refused, connection reset."""

    default_code = "network_error"
    default_retryable = True


class TimeoutError(TransportError):  # noqa: A001 - deliberately shadows the builtin
    """The request was still running when the configured timeout elapsed."""

    default_code = "timeout"
    default_retryable = True


#: Statuses worth another attempt: transient by definition.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

_STATUS_CLASSES: Dict[int, Type[ApiError]] = {
    400: ValidationError,
    401: AuthenticationError,
    402: QuotaExceededError,
    403: PermissionDeniedError,
    404: NotFoundError,
    413: PayloadTooLargeError,
    422: ValidationError,
    429: RateLimitError,
}

#: Envelope codes that mean something more specific than their HTTP status does.
_CODE_CLASSES: Dict[str, Type[ApiError]] = {
    "telemetry_unavailable": TelemetryUnavailableError,
    "quota_exceeded": QuotaExceededError,
    "entitlement_exhausted": QuotaExceededError,
    "entitlement_required": QuotaExceededError,
    "validation_failed": ValidationError,
    "unauthenticated": AuthenticationError,
    "permission_denied": PermissionDeniedError,
    "forbidden": PermissionDeniedError,
    "not_found": NotFoundError,
}


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Read ``Retry-After`` in either of its two legal forms."""
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        pass
    else:
        return seconds if seconds >= 0 else None

    # The header may also be an HTTP-date.
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
    except Exception:  # pragma: no cover - malformed dates are not worth a branch
        return None
    if when is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (when - now).total_seconds())


def error_from_response(
    status: int,
    body: Any,
    headers: Optional[Mapping[str, str]] = None,
) -> ApiError:
    """Build the right exception from a failed response.

    The control plane returns one envelope for every deliberate failure —
    ``{"error": {"code", "message", "details", "request_id"}}`` — so the message
    a developer sees is the sentence the server wrote for a person, not a
    generic "request failed".
    """
    envelope = body if isinstance(body, dict) else {}
    detail = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    code = str(detail.get("code") or "http_{0}".format(status))

    error_class: Type[ApiError] = _CODE_CLASSES.get(code) or _STATUS_CLASSES.get(status) or (
        ServerError if status >= 500 else ApiError
    )

    message = detail.get("message")
    if not message:
        if isinstance(body, str) and body.strip():
            message = body.strip()[:500]
        else:
            message = "The API returned HTTP {0}.".format(status)

    retry_after = None
    if headers is not None:
        # httpx headers are case-insensitive; a plain dict may not be.
        retry_after = _parse_retry_after(
            headers.get("retry-after") or headers.get("Retry-After")  # type: ignore[union-attr]
        )

    request_id = detail.get("request_id")
    if not request_id and headers is not None:
        request_id = headers.get("x-request-id") or headers.get("X-Request-Id")

    details = detail.get("details")
    return error_class(
        str(message),
        code=code,
        status=status,
        request_id=request_id,
        details=details if isinstance(details, dict) else None,
        retryable=status in RETRYABLE_STATUSES,
        retry_after_seconds=retry_after,
    )


def to_fulcrum_error(exc: BaseException, fallback_message: str) -> FulcrumOpsError:
    """Normalise anything raised into a ``FulcrumOpsError`` without losing the original."""
    if isinstance(exc, FulcrumOpsError):
        return exc

    name = type(exc).__name__
    message = str(exc) or fallback_message
    # httpx is the only transport, but it is imported lazily elsewhere and may
    # not be importable at all in a stripped-down test environment, so the
    # classification is by exception name rather than by isinstance.
    if "Timeout" in name:
        error: FulcrumOpsError = TimeoutError(message)
    elif "Connect" in name or "Network" in name or "Protocol" in name or "Proxy" in name:
        error = NetworkError(message)
    elif isinstance(exc, OSError):
        error = NetworkError(message)
    else:
        error = FulcrumOpsError(message)
    error.__cause__ = exc
    return error
