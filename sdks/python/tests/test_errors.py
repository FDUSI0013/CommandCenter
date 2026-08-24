"""Error translation: from the API's envelope to a class a caller can branch on."""

from __future__ import annotations

import datetime as dt

import pytest

from fulcrum_ops.errors import (
    ApiError,
    AuthenticationError,
    EntitlementError,
    FulcrumOpsError,
    NetworkError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    ServerError,
    TelemetryUnavailableError,
    TimeoutError,
    ValidationError,
    error_from_response,
    to_fulcrum_error,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, ValidationError),
        (401, AuthenticationError),
        (402, QuotaExceededError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (413, PayloadTooLargeError),
        (422, ValidationError),
        (429, RateLimitError),
        (500, ServerError),
        (503, ServerError),
        (418, ApiError),
    ],
)
def test_a_status_maps_to_its_class(status: int, expected: type) -> None:
    error = error_from_response(status, {})
    assert isinstance(error, expected)
    assert error.status == status


def test_an_envelope_code_beats_the_status_when_it_says_more() -> None:
    """503 is "something broke"; ``telemetry_unavailable`` is actionable."""
    error = error_from_response(503, {"error": {"code": "telemetry_unavailable"}})
    assert isinstance(error, TelemetryUnavailableError)
    assert error.retryable is True


def test_the_servers_own_message_is_what_the_developer_sees() -> None:
    error = error_from_response(
        403,
        {
            "error": {
                "code": "permission_denied",
                "message": "This key lacks the 'ingest' scope.",
                "request_id": "req-99",
                "details": {"scopes": ["read"]},
            }
        },
    )
    assert str(error).startswith("This key lacks the 'ingest' scope.")
    assert error.request_id == "req-99"
    assert error.details == {"scopes": ["read"]}
    assert error.code == "permission_denied"


def test_a_non_json_body_still_produces_a_readable_message() -> None:
    assert "Bad Gateway" in str(error_from_response(502, "Bad Gateway"))
    assert "HTTP 500" in str(error_from_response(500, None))


def test_retryability_is_decided_once_and_carried_on_the_error() -> None:
    assert error_from_response(503, {}).retryable is True
    assert error_from_response(429, {}).retryable is True
    assert error_from_response(408, {}).retryable is True
    assert error_from_response(401, {}).retryable is False
    assert error_from_response(413, {}).retryable is False
    assert error_from_response(422, {}).retryable is False


def test_retry_after_is_read_in_seconds_or_as_a_date() -> None:
    seconds = error_from_response(429, {}, {"retry-after": "30"})
    assert seconds.retry_after_seconds == 30.0

    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=45)
    stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    dated = error_from_response(429, {}, {"retry-after": stamp})
    assert dated.retry_after_seconds is not None
    assert 30 <= dated.retry_after_seconds <= 60

    assert error_from_response(429, {}, {"retry-after": "soon"}).retry_after_seconds is None
    assert error_from_response(429, {}, {}).retry_after_seconds is None


def test_the_request_id_falls_back_to_the_header() -> None:
    error = error_from_response(500, {}, {"x-request-id": "req-from-header"})
    assert error.request_id == "req-from-header"


def test_entitlement_and_quota_are_the_same_failure() -> None:
    assert EntitlementError is QuotaExceededError
    assert isinstance(error_from_response(200, {"error": {"code": "entitlement_exhausted"}}), QuotaExceededError)


def test_transport_failures_are_classified_by_shape() -> None:
    class ConnectTimeout(Exception):
        pass

    class ConnectError(Exception):
        pass

    assert isinstance(to_fulcrum_error(ConnectTimeout("slow"), "f"), TimeoutError)
    assert isinstance(to_fulcrum_error(ConnectError("refused"), "f"), NetworkError)
    assert isinstance(to_fulcrum_error(OSError("reset"), "f"), NetworkError)
    assert isinstance(to_fulcrum_error(ValueError("odd"), "f"), FulcrumOpsError)


def test_an_already_typed_error_passes_through_untouched() -> None:
    original = NotFoundError("gone", status=404)
    assert to_fulcrum_error(original, "fallback") is original


def test_the_original_exception_is_kept_as_the_cause() -> None:
    """Losing the traceback is how a support ticket becomes unanswerable."""
    original = OSError("connection reset by peer")
    wrapped = to_fulcrum_error(original, "fallback")
    assert wrapped.__cause__ is original
    assert "connection reset" in str(wrapped)


def test_a_blank_message_falls_back_rather_than_being_empty() -> None:
    assert str(to_fulcrum_error(OSError(), "the request failed")) == "the request failed"


def test_every_error_is_catchable_as_the_base_class() -> None:
    """One except clause has to be enough for a caller who just wants to log."""
    for error in (
        AuthenticationError("a"),
        QuotaExceededError("b"),
        ValidationError("c"),
        NetworkError("d"),
        TimeoutError("e"),
        TelemetryUnavailableError("f"),
    ):
        assert isinstance(error, FulcrumOpsError)
        with pytest.raises(FulcrumOpsError):
            raise error
