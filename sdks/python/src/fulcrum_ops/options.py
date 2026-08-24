"""Client options: what a caller passes, what the environment supplies, and the
defaults that hold when neither does.

Precedence is constructor argument, then environment variable, then default.
Nothing here touches the network. ``GET /ingest/config`` may narrow the batching
and sampling numbers afterwards, and when it does the *stricter* of the two
wins, because that document expresses a limit the deployment enforces rather
than a preference the developer expressed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigurationError, FulcrumOpsError

__all__ = ["Options", "resolve_options", "normalise_base_url", "DEFAULT_BASE_URL"]

DEFAULT_BASE_URL = "http://127.0.0.1:8080/api/v1"

ENV_API_KEY = "FULCRUM_OPS_API_KEY"
ENV_BASE_URL = "FULCRUM_OPS_BASE_URL"
ENV_WORKSPACE = "FULCRUM_OPS_WORKSPACE"
ENV_ENVIRONMENT = "FULCRUM_OPS_ENVIRONMENT"
ENV_AGENT = "FULCRUM_OPS_AGENT"
ENV_DEBUG = "FULCRUM_OPS_DEBUG"
ENV_DISABLED = "FULCRUM_OPS_DISABLED"
ENV_SAMPLING = "FULCRUM_OPS_SAMPLING_RATE"
ENV_TIMEOUT = "FULCRUM_OPS_TIMEOUT_SECONDS"
ENV_CAPTURE_INPUT = "FULCRUM_OPS_CAPTURE_INPUT"
ENV_CAPTURE_OUTPUT = "FULCRUM_OPS_CAPTURE_OUTPUT"

#: Called for every failure the SDK absorbs rather than raising.
ErrorHandler = Callable[[FulcrumOpsError, str], None]

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str) -> Optional[bool]:
    raw = _env(name)
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


def _env_float(name: str) -> Optional[float]:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _given(argument: Optional[float], from_env: Optional[float], default: float) -> float:
    """Argument, then environment, then default — treating 0 as a real answer.

    The obvious ``argument or from_env or default`` is wrong for every numeric
    option here, because ``0`` is falsy and is also a value a caller may
    legitimately mean. Each caller clamps the result to its own floor
    afterwards, which is where a nonsensical zero is dealt with.
    """
    for candidate in (argument, from_env):
        if candidate is None:
            continue
        try:
            return float(candidate)
        except (TypeError, ValueError):
            continue
    return float(default)


def normalise_base_url(raw: str) -> str:
    """Normalise the base URL.

    A trailing slash and a missing ``/api/v1`` are the two things people get
    wrong, and both fail silently later as a 404 on every ingest call, so both
    are fixed here. An outright unparseable URL is a construction error — worth
    raising for, because nothing the SDK does afterwards can work.
    """
    trimmed = (raw or "").strip().rstrip("/")
    if not trimmed:
        raise ConfigurationError("base_url must not be empty.")

    parts = urlsplit(trimmed)
    if parts.scheme not in ("http", "https"):
        raise ConfigurationError(
            "base_url must be http or https; received {0!r}. Expected something like "
            '"https://controlplane.example.com/api/v1".'.format(raw)
        )
    if not parts.netloc:
        raise ConfigurationError(
            "base_url is missing a host: {0!r}. Expected something like "
            '"https://controlplane.example.com/api/v1".'.format(raw)
        )

    # Point a bare host at the versioned API root rather than at the web app.
    path = parts.path.rstrip("/")
    if not path:
        path = "/api/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass
class Options:
    """Resolved settings, after arguments, environment and defaults are merged."""

    api_key: Optional[str] = None
    base_url: str = DEFAULT_BASE_URL
    workspace: Optional[str] = None
    environment: Optional[str] = None
    agent: Optional[str] = None

    timeout_seconds: float = 30.0

    # Batching.
    batch_max_items: int = 100
    batch_max_spans: int = 1_000
    batch_max_bytes: int = 4 * 1024 * 1024
    flush_interval_seconds: float = 5.0
    max_queue_size: int = 10_000

    # Retry.
    retry_max_attempts: int = 3
    retry_backoff_seconds: float = 0.5
    retry_max_backoff_seconds: float = 30.0

    # Capture.
    sampling_rate: float = 1.0
    capture_input: bool = True
    capture_output: bool = True
    redaction: List[Dict[str, Any]] = field(default_factory=list)

    # Behaviour.
    bootstrap: bool = True
    flush_on_exit: bool = True
    stream_spans: bool = False
    enabled: bool = True
    debug: bool = False
    set_as_default: bool = True

    on_error: Optional[ErrorHandler] = None
    headers: Dict[str, str] = field(default_factory=dict)
    http_client: Optional[Any] = None

    def redacted(self) -> Dict[str, Any]:
        """A representation safe to log: everything except the key itself."""
        out = dict(self.__dict__)
        out["api_key"] = "set" if self.api_key else None
        out.pop("on_error", None)
        out.pop("http_client", None)
        return out


def resolve_options(  # noqa: PLR0913 - this is the public surface; each name matters
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    workspace: Optional[str] = None,
    environment: Optional[str] = None,
    agent: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    batch_max_items: Optional[int] = None,
    batch_max_spans: Optional[int] = None,
    batch_max_bytes: Optional[int] = None,
    flush_interval_seconds: Optional[float] = None,
    max_queue_size: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    retry_backoff_seconds: Optional[float] = None,
    retry_max_backoff_seconds: Optional[float] = None,
    sampling_rate: Optional[float] = None,
    capture_input: Optional[bool] = None,
    capture_output: Optional[bool] = None,
    redaction: Optional[List[Dict[str, Any]]] = None,
    bootstrap: Optional[bool] = None,
    flush_on_exit: Optional[bool] = None,
    stream_spans: Optional[bool] = None,
    enabled: Optional[bool] = None,
    debug: Optional[bool] = None,
    set_as_default: Optional[bool] = None,
    on_error: Optional[ErrorHandler] = None,
    headers: Optional[Dict[str, str]] = None,
    http_client: Optional[Any] = None,
) -> Options:
    """Apply argument → environment → default precedence, and validate the result."""
    key = (api_key or "").strip() or _env(ENV_API_KEY)
    resolved_base = normalise_base_url(
        (base_url or "").strip() or _env(ENV_BASE_URL) or DEFAULT_BASE_URL
    )

    # A client with no key is not an error: it is a developer running the app on
    # their laptop without credentials. Reporting turns itself off and every
    # other line of their code keeps working.
    if enabled is None:
        disabled_by_env = _env_bool(ENV_DISABLED)
        resolved_enabled = bool(key) and not bool(disabled_by_env)
    else:
        resolved_enabled = bool(enabled)

    options = Options(
        api_key=key,
        base_url=resolved_base,
        workspace=(workspace or "").strip() or _env(ENV_WORKSPACE),
        environment=(environment or "").strip() or _env(ENV_ENVIRONMENT),
        agent=(agent or "").strip() or _env(ENV_AGENT),
        # Every numeric option below goes through ``_given``, never ``or``. A
        # caller who passes 0 means zero, and ``0 or 30`` silently means 30 —
        # so ``timeout_seconds=0`` would have become a thirty-second timeout
        # rather than being clamped to the floor the SDK actually supports.
        timeout_seconds=max(0.1, _given(timeout_seconds, _env_float(ENV_TIMEOUT), 30.0)),
        batch_max_items=max(1, int(_given(batch_max_items, None, 100))),
        batch_max_spans=max(1, int(_given(batch_max_spans, None, 1_000))),
        batch_max_bytes=max(1_024, int(_given(batch_max_bytes, None, 4 * 1024 * 1024))),
        flush_interval_seconds=max(0.05, _given(flush_interval_seconds, None, 5.0)),
        max_queue_size=max(1, int(_given(max_queue_size, None, 10_000))),
        retry_max_attempts=int(_clamp(int(_given(retry_max_attempts, None, 3)), 0, 10)),
        retry_backoff_seconds=max(0.001, _given(retry_backoff_seconds, None, 0.5)),
        retry_max_backoff_seconds=max(0.001, _given(retry_max_backoff_seconds, None, 30.0)),
        sampling_rate=_clamp(
            float(
                sampling_rate
                if sampling_rate is not None
                else (_env_float(ENV_SAMPLING) if _env_float(ENV_SAMPLING) is not None else 1.0)
            ),
            0.0,
            1.0,
        ),
        capture_input=(
            capture_input
            if capture_input is not None
            else (_env_bool(ENV_CAPTURE_INPUT) if _env_bool(ENV_CAPTURE_INPUT) is not None else True)
        ),
        capture_output=(
            capture_output
            if capture_output is not None
            else (
                _env_bool(ENV_CAPTURE_OUTPUT)
                if _env_bool(ENV_CAPTURE_OUTPUT) is not None
                else True
            )
        ),
        redaction=list(redaction or []),
        bootstrap=True if bootstrap is None else bool(bootstrap),
        flush_on_exit=True if flush_on_exit is None else bool(flush_on_exit),
        stream_spans=bool(stream_spans),
        enabled=resolved_enabled,
        debug=(
            debug if debug is not None else (_env_bool(ENV_DEBUG) if _env_bool(ENV_DEBUG) is not None else False)
        ),
        set_as_default=True if set_as_default is None else bool(set_as_default),
        on_error=on_error,
        headers=dict(headers or {}),
        http_client=http_client,
    )

    if options.retry_max_backoff_seconds < options.retry_backoff_seconds:
        options.retry_max_backoff_seconds = options.retry_backoff_seconds

    return options
