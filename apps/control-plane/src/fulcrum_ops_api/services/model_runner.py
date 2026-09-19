"""Calling a model from the control plane.

This is the one place the service talks to a model provider itself. Everywhere
else it governs and records agents that do their own calling, so this module is
deliberately small and deliberately off by default: without an endpoint and a
key in settings, :func:`execute` refuses and says which setting is missing
rather than half-working.

The wire format is OpenAI-compatible chat completions, which OpenAI, Azure
OpenAI and most local gateways all speak. Azure is detected from the endpoint
and its ``api-key`` header and ``api-version`` query parameter are used instead
of a bearer token.

Nothing here is retried. A prompt run is an interactive action with a person
waiting on it; a silent retry would double the spend and the latency they are
measuring.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Final

import httpx

from ..core.config import get_settings
from ..core.errors import ModelUnavailable, ValidationFailed

#: Providers that authenticate with a header rather than a bearer token.
_AZURE_HOSTS: Final[tuple[str, ...]] = (".openai.azure.com", ".cognitiveservices.azure.com")


@dataclasses.dataclass(frozen=True)
class ModelRun:
    """What one execution produced. Token counts are the provider's own."""

    output: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: float
    finish_reason: str | None


def _is_azure(endpoint: str) -> bool:
    return any(host in endpoint for host in _AZURE_HOSTS)


def _url(endpoint: str, model: str) -> tuple[str, dict[str, str]]:
    """Completions URL and query parameters for the configured provider."""
    settings = get_settings()
    base = endpoint.rstrip("/")
    if _is_azure(base):
        version = settings.prompt_studio_api_version or "2024-10-21"
        return f"{base}/openai/deployments/{model}/chat/completions", {"api-version": version}
    if base.endswith("/chat/completions"):
        return base, {}
    return f"{base}/chat/completions", {}


def _headers(endpoint: str, api_key: str) -> dict[str, str]:
    if _is_azure(endpoint):
        return {"api-key": api_key, "content-type": "application/json"}
    return {"authorization": f"Bearer {api_key}", "content-type": "application/json"}


def available() -> bool:
    return get_settings().prompt_studio_enabled


def requirement() -> str:
    """Which setting is missing, said in the operator's own vocabulary."""
    settings = get_settings()
    missing = []
    if not settings.prompt_studio_endpoint:
        missing.append("FULCRUM_OPS_PROMPT_STUDIO_ENDPOINT")
    if not settings.prompt_studio_api_key:
        missing.append("FULCRUM_OPS_PROMPT_STUDIO_API_KEY")
    return (
        "Running prompts is not configured on this deployment. Set "
        + " and ".join(missing)
        + ", then restart the control plane."
    )


async def execute(
    rendered: str,
    *,
    model: str | None = None,
    system: str | None = None,
    max_output_tokens: int | None = None,
) -> ModelRun:
    """Send one rendered prompt to the model and return what came back.

    Raises :class:`ModelUnavailable` when the feature is unconfigured or the
    provider cannot be reached, and :class:`ValidationFailed` when the provider
    refuses the request itself — a 400 from the model is the caller's problem to
    fix, not an outage.
    """
    settings = get_settings()
    if not settings.prompt_studio_enabled:
        raise ModelUnavailable(requirement())

    chosen = (model or settings.prompt_studio_model).strip()
    endpoint = settings.prompt_studio_endpoint or ""
    url, params = _url(endpoint, chosen)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": rendered})

    body: dict[str, Any] = {"messages": messages}
    # Azure resolves the model from the deployment in the path; sending it in
    # the body as well is accepted but redundant, and wrong when the deployment
    # name differs from the model name.
    if not _is_azure(endpoint):
        body["model"] = chosen
    cap = max_output_tokens or settings.prompt_studio_max_output_tokens
    if cap:
        # Reasoning models renamed this field; send both and let the provider
        # ignore the one it does not know.
        body["max_completion_tokens"] = cap
    effort = (settings.prompt_studio_reasoning_effort or "").strip()
    if effort:
        # Only when the operator has said the configured model reasons: every
        # other model answers 400 to a request that carries this field, so it
        # is never sent "just in case".
        body["reasoning_effort"] = effort

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.prompt_studio_timeout_seconds) as http:
            response = await http.post(
                url,
                params=params,
                json=body,
                headers=_headers(endpoint, settings.prompt_studio_api_key or ""),
            )
    except httpx.TimeoutException as exc:
        raise ModelUnavailable(
            f"The model did not answer within {settings.prompt_studio_timeout_seconds:.0f}s."
        ) from exc
    except httpx.HTTPError as exc:
        raise ModelUnavailable(f"The model endpoint could not be reached: {exc}") from exc
    latency_ms = (time.perf_counter() - started) * 1000

    if response.status_code >= 500:
        raise ModelUnavailable(
            f"The model provider answered {response.status_code}. Try again shortly."
        )
    if response.status_code >= 400:
        detail = response.text[:400]
        raise ValidationFailed(
            "The model refused this request.",
            details={"status": response.status_code, "provider": detail},
        )

    payload = response.json()
    choices = payload.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    usage = payload.get("usage") or {}

    def count(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, int):
                return value
        return None

    return ModelRun(
        output=str(message.get("content") or ""),
        model=str(payload.get("model") or chosen),
        prompt_tokens=count("prompt_tokens", "input_tokens"),
        completion_tokens=count("completion_tokens", "output_tokens"),
        total_tokens=count("total_tokens"),
        latency_ms=round(latency_ms, 1),
        finish_reason=first.get("finish_reason"),
    )
