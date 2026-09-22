"""The one place the SDK talks HTTP.

Everything above this file deals in dictionaries and exceptions; everything
below it is httpx. Keeping that boundary sharp is what makes the tests able to
point the whole SDK at a stub server in-process, and what makes swapping in a
customer's proxy-aware client a one-argument change.
"""

from __future__ import annotations

import json as _json
import logging
import random
import threading
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from ._version import SDK_NAME, SDK_VERSION, USER_AGENT
from .errors import ApiError, FulcrumOpsError, to_fulcrum_error
from .errors import error_from_response
from .options import Options

__all__ = ["Transport", "Response", "backoff_delay"]

logger = logging.getLogger("fulcrum_ops")

#: The redirects that mean "the same request, over there". 303 is left out: it
#: means "now GET this", which is not what re-posting a batch would be.
_REDIRECTS = (301, 302, 307, 308)


class Response:
    """A completed HTTP exchange, decoded far enough to act on."""

    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body: Any, headers: Mapping[str, str]) -> None:
        self.status = status
        self.body = body
        self.headers = headers

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Response(status={0})".format(self.status)


def backoff_delay(
    attempt: int,
    base_seconds: float,
    max_seconds: float,
    retry_after: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> float:
    """Exponential backoff with full jitter.

    Full jitter rather than a fixed multiplier because the failure mode that
    matters is a fleet of agent processes losing the server at the same
    instant and then retrying in lockstep. Spreading each attempt uniformly
    across its window is what stops the recovery from being a second outage.

    A server-supplied ``Retry-After`` overrides the computed delay: it is a
    statement of fact about when capacity returns, not an estimate.
    """
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, max_seconds)
    window = min(base_seconds * (2**attempt), max_seconds)
    source = rng or random
    return source.uniform(0.0, window)


class Transport:
    """A thin, thread-safe wrapper over one httpx client."""

    def __init__(self, options: Options) -> None:
        self._options = options
        self._lock = threading.Lock()
        self._closed = False
        self._owns_client = options.http_client is None
        self._client: Optional[Any] = options.http_client
        self._redirect_reported = False

    # ------------------------------------------------------------------ setup

    @property
    def base_url(self) -> str:
        return self._options.base_url

    def _headers(self) -> Dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": USER_AGENT,
            "x-fulcrum-sdk": SDK_NAME,
            "x-fulcrum-sdk-version": SDK_VERSION,
        }
        if self._options.api_key:
            headers["authorization"] = "Bearer {0}".format(self._options.api_key)
        if self._options.workspace:
            headers["x-fulcrum-workspace"] = self._options.workspace
        headers.update({str(k).lower(): str(v) for k, v in (self._options.headers or {}).items()})
        return headers

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                try:
                    import httpx
                except ImportError as exc:  # pragma: no cover - declared dependency
                    raise FulcrumOpsError(
                        "httpx is required to talk to the FD AI Command Center server. "
                        "Install it with: pip install httpx"
                    ) from exc
                self._client = httpx.Client(
                    timeout=httpx.Timeout(self._options.timeout_seconds),
                    follow_redirects=True,
                )
            return self._client

    # --------------------------------------------------------------- requests

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/api/v1/"):
            # The config document names endpoints with their full prefix; the
            # base URL already carries it, so use the base URL's host and the
            # document's path.
            root = self.base_url.split("/api/v1")[0].rstrip("/")
            return "{0}{1}".format(root, path)
        return "{0}/{1}".format(self.base_url.rstrip("/"), path.lstrip("/"))

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        """Perform one request. Raises :class:`TransportError` only when no response arrived."""
        if self._closed:
            raise FulcrumOpsError("This client has been closed.")

        client = self._ensure_client()
        merged = self._headers()
        if headers:
            merged.update({str(k).lower(): str(v) for k, v in headers.items()})

        content = None
        if body is not None:
            content = body if isinstance(body, (str, bytes)) else _json.dumps(body)
            if isinstance(content, str):
                content = content.encode("utf-8")

        url = self._url(path)
        try:
            raw = client.request(
                method.upper(),
                url,
                content=content,
                params=dict(params) if params else None,
                headers=merged,
                timeout=timeout if timeout is not None else self._options.timeout_seconds,
            )
            target = self._redirect_target(url, raw)
            if target is not None:
                raw = client.request(
                    method.upper(),
                    target,
                    content=content,
                    params=dict(params) if params else None,
                    headers=merged,
                    timeout=timeout if timeout is not None else self._options.timeout_seconds,
                )
        except Exception as exc:
            raise to_fulcrum_error(
                exc, "The request to the FD AI Command Center server failed."
            ) from exc

        return Response(raw.status_code, _decode(raw), raw.headers)

    def _redirect_target(self, url: str, raw: Any) -> Optional[str]:
        """Where to re-send a redirected request, when the HTTP client will not.

        The client this SDK builds follows redirects. One the caller injects —
        for a proxy, or a custom trust store — usually does not, because httpx
        does not by default; and an ``http://`` base URL in front of a proxy
        that upgrades to ``https://`` then answers every batch with a 308, which
        is not retryable, so every batch was dropped as ``The API returned HTTP
        308``. Followed once, and only to the same host: the request carries the
        API key, and a redirect is not allowed to send that somewhere else.
        """
        if self._owns_client or getattr(raw, "status_code", None) not in _REDIRECTS:
            return None
        try:
            location = raw.headers.get("location")
        except Exception:  # noqa: BLE001
            location = None
        if not location:
            return None
        target = urljoin(url, str(location))
        if (urlsplit(target).hostname or "").lower() != (urlsplit(url).hostname or "").lower():
            logger.warning(
                "fulcrum-ops: the server at %s redirects to another host (%s); not "
                "following it with the API key. Set base_url to the address it should use.",
                url,
                target,
            )
            return None
        if not self._redirect_reported:
            self._redirect_reported = True
            logger.warning(
                "fulcrum-ops: %s redirects to %s. Following it, at the cost of a second request "
                "every time; set base_url (FULCRUM_OPS_BASE_URL) to the final address, or build "
                "your http_client with follow_redirects=True.",
                url,
                target,
            )
        return target

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Perform one request and raise an :class:`ApiError` unless it succeeded."""
        response = self.request(
            method, path, body=body, params=params, headers=headers, timeout=timeout
        )
        if not response.ok:
            raise error_from_response(response.status, response.body, response.headers)
        return response.body

    def send_with_retry(
        self,
        method: str,
        path: str,
        *,
        body: Any,
        sleep: Any,
        rng: Optional[random.Random] = None,
    ) -> Tuple[Optional[Response], Optional[FulcrumOpsError], int]:
        """Send a batch, retrying the failures that could plausibly clear.

        Returns ``(response, error, attempts)``. It never raises: the flusher's
        job is to log and continue, and returning the failure rather than
        throwing it keeps that decision in one place.
        """
        attempts = 0
        last_error: Optional[FulcrumOpsError] = None
        max_attempts = self._options.retry_max_attempts

        while attempts <= max_attempts:
            attempts += 1
            try:
                response = self.request(method, path, body=body)
            except FulcrumOpsError as exc:
                last_error = exc
            else:
                if response.ok:
                    return response, None, attempts
                error = error_from_response(response.status, response.body, response.headers)
                last_error = error
                if not error.retryable:
                    return response, error, attempts

            if attempts > max_attempts:
                break

            retry_after = getattr(last_error, "retry_after_seconds", None)
            delay = backoff_delay(
                attempts - 1,
                self._options.retry_backoff_seconds,
                self._options.retry_max_backoff_seconds,
                retry_after,
                rng,
            )
            logger.debug(
                "fulcrum-ops: %s %s failed (%s); retrying in %.2fs (attempt %d/%d)",
                method,
                path,
                last_error,
                delay,
                attempts,
                max_attempts + 1,
            )
            sleep(delay)

        return None, last_error, attempts

    # ---------------------------------------------------------------- cleanup

    def _reset_after_fork(self) -> None:
        """Stop sharing the parent's connections. Called in a forked child only.

        The pooled sockets are the parent's too, and two processes writing
        requests down one connection corrupts both. The client is let go of
        rather than closed — closing would say goodbye on sockets the parent is
        still using — and the next request builds a fresh one. A client the
        caller injected is theirs to make fork-safe.
        """
        self._lock = threading.Lock()
        if self._owns_client:
            self._client = None

    def close(self) -> None:
        """Release the underlying connection pool, if this transport owns it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
        if client is not None and self._owns_client:
            try:
                client.close()
            except Exception:  # pragma: no cover - shutdown races are not worth raising over
                pass


def _decode(raw: Any) -> Any:
    """Decode a response body without ever raising over its content type."""
    try:
        text = raw.text
    except Exception:  # pragma: no cover
        return None
    if not text:
        return None
    content_type = ""
    try:
        content_type = raw.headers.get("content-type", "")
    except Exception:  # pragma: no cover
        pass
    if "json" in content_type or text.lstrip()[:1] in ("{", "["):
        try:
            return _json.loads(text)
        except ValueError:
            return text
    return text


# Re-exported so callers can catch the API failure class without importing two
# modules to make one request.
__all__.append("ApiError")
