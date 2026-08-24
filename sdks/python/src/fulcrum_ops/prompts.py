"""Fetching prompts from the Prompt Manager, with a cache.

An agent that pulls its system prompt from the control plane gets versioning,
review and rollback for free — but it also gets a network round trip on a path
that used to be a string literal. So every lookup is cached, and the cache is
the point rather than an optimisation.

Two lookups behave differently, on purpose:

* **By name or id, unpinned** — resolves to the prompt's current head, cached
  for a TTL, because the whole reason to fetch it is that someone may change it
  without redeploying.
* **By commit** — a pinned, immutable version, cached forever, because a commit
  cannot change.

Unlike telemetry, a prompt lookup *does* raise. A missing system prompt is not a
degraded agent, it is a broken one, and failing loudly at start-up beats sending
an empty string to a model.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Mapping, Optional

from .errors import NotFoundError
from .transport import Transport

__all__ = ["Prompt", "PromptCache", "render_template", "template_variables"]

#: Mustache-style placeholder, which is what the control plane's templates use.
_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")

DEFAULT_TTL_SECONDS = 300.0


def template_variables(template: str) -> List[str]:
    """Placeholders a template declares, in first-seen order."""
    seen: List[str] = []
    for match in _VARIABLE_PATTERN.finditer(template or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def render_template(template: str, variables: Optional[Mapping[str, Any]] = None) -> str:
    """Substitute every placeholder present in ``variables``; leave the rest alone.

    A placeholder with no value is left as-is rather than replaced with an empty
    string: a visible ``{{customer_name}}`` in a model's context is a bug
    somebody notices, and a silently missing one is a bug nobody does.
    """
    values = variables or {}

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        value = values[name]
        if value is None:
            return match.group(0)
        return value if isinstance(value, str) else str(value)

    return _VARIABLE_PATTERN.sub(replace, template or "")


class Prompt:
    """A prompt, resolved and ready to render."""

    __slots__ = ("id", "name", "template", "variables", "version", "commit", "status", "raw")

    def __init__(
        self,
        *,
        id: str,  # noqa: A002 - matches the wire field name
        name: str,
        template: str,
        variables: Optional[List[str]] = None,
        version: Optional[str] = None,
        commit: Optional[str] = None,
        status: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.id = id
        self.name = name
        self.template = template or ""
        self.variables = variables or template_variables(self.template)
        self.version = version
        self.commit = commit
        self.status = status
        self.raw = raw or {}

    def format(self, **variables: Any) -> str:
        """Substitute the placeholders and return the rendered text."""
        return render_template(self.template, variables)

    def render(self, variables: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> str:
        """Same as :meth:`format`, for callers who already hold a dict."""
        merged: Dict[str, Any] = dict(variables or {})
        merged.update(kwargs)
        return render_template(self.template, merged)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.template

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Prompt(name={0!r}, version={1!r}, commit={2!r})".format(
            self.name, self.version, self.commit
        )


class _Entry:
    __slots__ = ("prompt", "expires_at")

    def __init__(self, prompt: Prompt, expires_at: float) -> None:
        self.prompt = prompt
        self.expires_at = expires_at


class PromptCache:
    """``client.prompts`` — the Prompt Manager, cached."""

    def __init__(self, transport: Transport, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._transport = transport
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: Dict[str, _Entry] = {}

    def clear(self) -> None:
        """Forget every cached prompt; the next lookup refetches."""
        with self._lock:
            self._entries.clear()

    def get(
        self,
        name: str,
        *,
        commit: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        refresh: bool = False,
    ) -> Prompt:
        """Fetch a prompt by id or by name.

        The id path is tried first because it is the exact one; a name falls
        through to a search, which is how a caller who only knows
        ``"support-copilot-system"`` gets an answer without hard-coding a UUID.
        """
        key = "{0}@{1}".format(name, commit or "head")
        now = time.monotonic()

        if not refresh:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.expires_at > now:
                    return entry.prompt

        prompt = self._fetch(name, commit)

        # A pinned commit is immutable, so it never expires.
        ttl = float("inf") if commit else (ttl_seconds if ttl_seconds is not None else self._ttl)
        with self._lock:
            self._entries[key] = _Entry(
                prompt, float("inf") if ttl == float("inf") else time.monotonic() + ttl
            )
        return prompt

    # ---------------------------------------------------------------- lookup

    def _fetch(self, name: str, commit: Optional[str]) -> Prompt:
        record = self._by_id(name) or self._by_name(name)
        if record is None:
            raise NotFoundError(
                "No prompt named {0!r} exists in this workspace. Create it in the console "
                "under Prompt Manager, or check the API key's workspace.".format(name),
                code="not_found",
                status=404,
            )

        prompt_id = str(record.get("id") or name)
        if commit:
            version = self._transport.request_json(
                "GET", "prompts/{0}/versions/{1}".format(prompt_id, commit)
            )
            if not isinstance(version, dict):
                raise NotFoundError(
                    "Prompt {0!r} has no commit {1!r}.".format(name, commit),
                    code="not_found",
                    status=404,
                )
            return Prompt(
                id=prompt_id,
                name=str(record.get("name") or name),
                template=str(version.get("template") or ""),
                variables=list(version.get("variables") or []) or None,
                version=version.get("version"),
                commit=version.get("commit") or commit,
                status=version.get("status") or record.get("status"),
                raw=version,
            )

        return Prompt(
            id=prompt_id,
            name=str(record.get("name") or name),
            template=str(record.get("template") or ""),
            variables=list(record.get("variables") or []) or None,
            version=record.get("version"),
            commit=record.get("commit"),
            status=record.get("status"),
            raw=record,
        )

    def _by_id(self, identifier: str) -> Optional[Dict[str, Any]]:
        try:
            record = self._transport.request_json("GET", "prompts/{0}".format(identifier))
        except NotFoundError:
            return None
        return record if isinstance(record, dict) else None

    def _by_name(self, name: str) -> Optional[Dict[str, Any]]:
        page = self._transport.request_json(
            "GET", "prompts", params={"q": name, "page_size": 25}
        )
        items = page.get("items") if isinstance(page, dict) else None
        if not items:
            return None
        lowered = name.strip().lower()
        for item in items:
            if isinstance(item, dict) and str(item.get("name", "")).strip().lower() == lowered:
                return item
        first = items[0]
        return first if isinstance(first, dict) else None
