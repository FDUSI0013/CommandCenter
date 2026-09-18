"""An in-process stand-in for the private telemetry engine.

The real engine cannot run here, so this module implements the HTTP contract
that ``fulcrum_ops_api.engine.client.EngineClient`` actually speaks, backed by
dictionaries. It is deliberately a *store*, not a canned-response stub: what you
POST is what a later GET, search or stats call answers with, so a test can push
a batch through ``/ingest/traces`` and then assert it comes back out of
``/runs`` without either side being faked.

Three properties matter, and every one of them is a property the adapter relies
on rather than a convenience:

* **Shapes.** Paged reads answer ``{"content": [...], "total": n, "page": p,
  "size": s}``. Creates answer ``201`` with an empty body and a ``Location``
  header, because :meth:`EngineClient._create` reads the new id off it. Most
  mutations answer ``204``. The three search endpoints answer newline-delimited
  JSON, one row per line, which is what ``_decode_lines`` parses.
* **Cursor paging.** ``search_traces``/``search_spans``/``search_threads``
  stream by id *descending* (newest first, as the real engine does) and honour
  ``last_retrieved_id``/``last_retrieved_thread_model_id`` as an
  older-rows-only resume point, returning fewer rows than ``limit`` once
  exhausted — that "short page means done" signal is exactly how the scan loop
  in ``services.runs`` terminates.
* **Failure.** :meth:`EngineDouble.fail` makes every route answer 503 so the
  fail-closed behaviour of the telemetry surfaces can be exercised, and
  :attr:`EngineDouble.calls` records every request for assertions about *what*
  was asked, not just what came back.

Nothing here imports the application, so the double can be reasoned about on
its own terms: it is a second implementation of the contract, and where the two
disagree one of them is wrong.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs

JsonObject = dict[str, Any]

API_ROOT = "/v1/private"
HEALTH_PATH = "/is-alive/ping"

#: Path the inline content checker is published under when no separate base URL
#: is configured for it. It is outside ``API_ROOT`` on purpose — see
#: ``EngineClient.evaluate_guardrails``.
CHECKER_PATH = "/api/v1/guardrails/validations"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_instant(value: Any) -> dt.datetime | None:
    """Parse an instant the way the engine would, tolerating a trailing Z."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _json_param(raw: str | None) -> Any:
    """``filters``/``sorting``/``exclude`` travel as JSON inside one query value."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _text_of(*documents: Any) -> str:
    parts: list[str] = []
    for document in documents:
        if document is None:
            continue
        if isinstance(document, str):
            parts.append(document)
        else:
            parts.append(json.dumps(document, default=str, ensure_ascii=False))
    return "\n".join(parts)


def _matches_search(row: Mapping[str, Any], needle: str | None, fields: Sequence[str]) -> bool:
    if not needle:
        return True
    lowered = needle.strip().lower()
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        if lowered in _text_of(value).lower():
            return True
    return False


def _dig(row: Mapping[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _matches_filters(row: Mapping[str, Any], filters: Any) -> bool:
    """Apply the engine's filter documents: ``{field, operator, value, key}``.

    Only the operators the control plane actually sends are implemented; an
    operator this double does not know refuses to match rather than silently
    passing everything, so a filter that is quietly wrong shows up as an empty
    result instead of an unfiltered one.
    """
    if not filters:
        return True
    if not isinstance(filters, list):
        return True
    for entry in filters:
        if not isinstance(entry, Mapping):
            continue
        field = str(entry.get("field") or "")
        key = entry.get("key")
        operator = str(entry.get("operator") or "=").lower()
        wanted = entry.get("value")
        actual = _dig(row, f"{field}.{key}") if key else row.get(field)
        if not _compare(operator, actual, wanted):
            return False
    return True


def _compare(operator: str, actual: Any, wanted: Any) -> bool:
    if operator in {"=", "eq", "equal"}:
        return str(actual) == str(wanted)
    if operator in {"!=", "ne", "not_equal"}:
        return str(actual) != str(wanted)
    if operator == "contains":
        return str(wanted).lower() in _text_of(actual).lower()
    if operator == "not_contains":
        return str(wanted).lower() not in _text_of(actual).lower()
    if operator in {">", "gt", ">=", "gte", "<", "lt", "<=", "lte"}:
        try:
            left, right = float(actual), float(wanted)
        except (TypeError, ValueError):
            return False
        return {
            ">": left > right,
            "gt": left > right,
            ">=": left >= right,
            "gte": left >= right,
            "<": left < right,
            "lt": left < right,
            "<=": left <= right,
            "lte": left <= right,
        }[operator]
    if operator == "is_empty":
        return actual in (None, "", [], {})
    if operator == "is_not_empty":
        return actual not in (None, "", [], {})
    return False


def _sorted_rows(rows: list[JsonObject], sorting: Any) -> list[JsonObject]:
    """Apply the engine's sort documents: ``[{"field": …, "direction": …}]``."""
    if not isinstance(sorting, list):
        return rows
    ordered = list(rows)
    for entry in reversed(sorting):
        if not isinstance(entry, Mapping):
            continue
        field = str(entry.get("field") or "")
        if not field:
            continue
        descending = str(entry.get("direction") or "ASC").upper().startswith("DESC")
        ordered.sort(key=lambda row: _sort_key(row.get(field)), reverse=descending)
    return ordered


def _sort_key(value: Any) -> tuple[int, float, str]:
    """Order mixed types without ever raising: numbers, then text, then nulls."""
    if value is None:
        return (2, 0.0, "")
    if isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, (int, float)):
        return (0, float(value), "")
    return (1, 0.0, str(value))


def _page(rows: Sequence[JsonObject], page: int, size: int, key: str = "content") -> JsonObject:
    """The engine's paged envelope. ``total`` counts the filtered set, not the page."""
    start = max(page - 1, 0) * size
    return {
        key: [dict(row) for row in rows[start : start + size]],
        "total": len(rows),
        "page": page,
        "size": size,
    }


def _within(row: Mapping[str, Any], field: str, since: Any, until: Any) -> bool:
    start = _parse_instant(row.get(field))
    if start is None:
        return True
    lower, upper = _parse_instant(since), _parse_instant(until)
    if lower is not None and start < lower:
        return False
    return not (upper is not None and start > upper)


# ---------------------------------------------------------------------------
# Recorded traffic
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RecordedCall:
    """One request the double served, for tests that assert on the traffic."""

    method: str
    path: str
    query: dict[str, list[str]]
    body: Any

    def param(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None


class EngineFailure(Exception):
    """Raised inside a handler to answer with a specific status and body."""

    def __init__(self, status: int, body: Any = None) -> None:
        self.status = status
        self.body = body if body is not None else {"errors": [f"status {status}"]}
        super().__init__(f"engine double answering {status}")


# ---------------------------------------------------------------------------
# The double
# ---------------------------------------------------------------------------


class EngineDouble:
    """An ASGI application implementing the private telemetry engine's contract.

    Mount it under :class:`httpx.ASGITransport` and hand that transport to
    :class:`EngineClient`; no socket is opened and no server runs.
    """

    def __init__(self) -> None:
        self.projects: dict[str, JsonObject] = {}
        self.traces: dict[str, JsonObject] = {}
        self.spans: dict[str, JsonObject] = {}
        self.thread_scores: list[JsonObject] = []
        self.comments: dict[str, JsonObject] = {}
        self.prompts: dict[str, JsonObject] = {}
        self.prompt_versions: dict[str, list[JsonObject]] = {}
        self.datasets: dict[str, JsonObject] = {}
        self.dataset_items: dict[str, list[JsonObject]] = {}
        self.experiments: dict[str, JsonObject] = {}
        self.experiment_items: dict[str, list[JsonObject]] = {}
        self.guardrail_rules: dict[str, JsonObject] = {}
        self.guardrail_results: list[JsonObject] = []
        self.alerts: dict[str, JsonObject] = {}
        self.webhook_tests: list[JsonObject] = []

        self.calls: list[RecordedCall] = []
        #: When set, every route answers with this status instead of serving.
        self.failure_status: int | None = None
        #: Per-path-fragment overrides, checked before ``failure_status``.
        self.path_failures: dict[str, int] = {}
        #: Guardrail verdicts the inline checker hands back, newest wins.
        self.checker_verdicts: list[JsonObject] = []
        self.checker_requests: list[JsonObject] = []
        self.healthy = True

        self._routes = self._build_routes()

    # -- controls ----------------------------------------------------------

    def fail(self, status: int = 503) -> None:
        """Make every subsequent request answer ``status``."""
        self.failure_status = status

    def recover(self) -> None:
        self.failure_status = None
        self.path_failures.clear()

    def fail_path(self, fragment: str, status: int = 503) -> None:
        """Fail only the routes whose path contains ``fragment``."""
        self.path_failures[fragment] = status

    def calls_to(self, fragment: str) -> list[RecordedCall]:
        return [call for call in self.calls if fragment in call.path]

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- seeding -----------------------------------------------------------

    def add_project(self, name: str, *, project_id: str | None = None) -> JsonObject:
        """Create a project directly, as a test fixture rather than over HTTP."""
        existing = self._project_by_name(name)
        if existing is not None:
            return existing
        project: JsonObject = {
            "id": project_id or _new_id(),
            "name": name,
            "description": None,
            "visibility": "private",
            "created_at": _iso(),
            "last_updated_at": _iso(),
            # A project nothing has reported to yet has no last-trace time.
            "last_updated_trace_at": None,
        }
        self.projects[project["id"]] = project
        return project

    def add_trace(
        self,
        *,
        project_name: str,
        name: str = "run",
        trace_id: str | None = None,
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
        **fields: Any,
    ) -> JsonObject:
        """Store one trace, creating its project if the name is new."""
        started = start_time or _now()
        payload: JsonObject = {
            "id": trace_id or _new_id(),
            "project_name": project_name,
            "name": name,
            "start_time": _iso(started),
            "end_time": _iso(end_time) if end_time else _iso(started),
            **fields,
        }
        self._store_trace(payload)
        return self.traces[payload["id"]]

    def trace_count(self, project_name: str | None = None) -> int:
        if project_name is None:
            return len(self.traces)
        return sum(1 for row in self.traces.values() if row.get("project_name") == project_name)

    # -- ASGI --------------------------------------------------------------

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":  # pragma: no cover - transport never sends one
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":  # pragma: no cover - HTTP only
            raise RuntimeError(f"the engine double serves HTTP, not {scope['type']!r}")

        body = await self._read_body(receive)
        method: str = scope["method"].upper()
        path: str = scope["path"]
        query = parse_qs(scope.get("query_string", b"").decode("utf-8"), keep_blank_values=True)

        parsed_body: Any = None
        if body:
            try:
                parsed_body = json.loads(body)
            except ValueError:
                parsed_body = body.decode("utf-8", "replace")
        self.calls.append(RecordedCall(method=method, path=path, query=query, body=parsed_body))

        status, payload, headers = self._dispatch(method, path, query, parsed_body)
        await self._respond(send, status, payload, headers)

    @staticmethod
    async def _lifespan(receive: Callable, send: Callable) -> None:  # pragma: no cover
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _read_body(receive: Callable) -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":  # pragma: no cover - disconnect
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    async def _respond(
        send: Callable, status: int, payload: Any, headers: dict[str, str]
    ) -> None:
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        if payload is None:
            body = b""
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload, default=str).encode()
            raw_headers.append((b"content-type", b"application/json"))
        raw_headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": raw_headers})
        await send({"type": "http.response.body", "body": body})

    def _dispatch(
        self, method: str, path: str, query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any, dict[str, str]]:
        for fragment, status in self.path_failures.items():
            if fragment in path:
                return status, {"errors": [f"path {fragment} is failing"]}, {}
        if self.failure_status is not None:
            return self.failure_status, {"errors": ["the telemetry engine is unavailable"]}, {}

        for route_method, pattern, handler in self._routes:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if match is None:
                continue
            try:
                result = handler(match, query, body)
            except EngineFailure as failure:
                return failure.status, failure.body, {}
            if isinstance(result, tuple):
                status, payload, headers = result
                return status, payload, headers
            if result is None:
                return 204, None, {}
            return 200, result, {}
        return 404, {"errors": [f"no route for {method} {path}"]}, {}

    # -- routing table -----------------------------------------------------

    def _build_routes(self) -> list[tuple[str, re.Pattern[str], Callable]]:
        root = re.escape(API_ROOT)
        uid = r"([^/]+)"

        def route(method: str, path: str, handler: Callable) -> tuple[str, re.Pattern[str], Callable]:
            return method, re.compile(path), handler

        # Order matters: literal paths are registered ahead of the parameterised
        # ones they would otherwise be captured by (/traces/threads before
        # /traces/{id}, /prompts/versions before /prompts/{id}, and so on).
        return [
            route("GET", re.escape(HEALTH_PATH), self._health),
            # -- projects --
            route("GET", rf"{root}/projects", self._list_projects),
            route("POST", rf"{root}/projects", self._create_project),
            route("POST", rf"{root}/projects/retrieve", self._retrieve_project),
            route("GET", rf"{root}/projects/stats", self._project_stats),
            route("POST", rf"{root}/projects/{uid}/metrics", self._project_metrics),
            route("GET", rf"{root}/projects/{uid}", self._get_project),
            # -- threads (before /traces/{id}) --
            route("GET", rf"{root}/traces/threads", self._list_threads),
            route("POST", rf"{root}/traces/threads/retrieve", self._retrieve_thread),
            route("POST", rf"{root}/traces/threads/search", self._search_threads),
            route("GET", rf"{root}/traces/threads/stats", self._thread_stats),
            route("PUT", rf"{root}/traces/threads/feedback-scores", self._score_threads),
            route(
                "GET",
                rf"{root}/traces/threads/feedback-scores/names",
                self._thread_score_names,
            ),
            # -- traces --
            route("POST", rf"{root}/traces/batch", self._create_traces),
            route("POST", rf"{root}/traces/search", self._search_traces),
            route("GET", rf"{root}/traces/stats", self._trace_stats),
            route("POST", rf"{root}/traces/delete", self._delete_traces),
            route("PUT", rf"{root}/traces/feedback-scores", self._score_traces),
            route("GET", rf"{root}/traces/feedback-scores/names", self._trace_score_names),
            route("POST", rf"{root}/traces/comments/delete", self._delete_trace_comments),
            route("PATCH", rf"{root}/traces/comments/{uid}", self._update_trace_comment),
            route("POST", rf"{root}/traces/{uid}/comments", self._add_trace_comment),
            route("GET", rf"{root}/traces/{uid}/comments/{uid}", self._get_trace_comment),
            route("GET", rf"{root}/traces", self._list_traces),
            route("GET", rf"{root}/traces/{uid}", self._get_trace),
            route("PATCH", rf"{root}/traces/{uid}", self._update_trace),
            # -- spans --
            route("POST", rf"{root}/spans/batch", self._create_spans),
            route("POST", rf"{root}/spans/search", self._search_spans),
            route("GET", rf"{root}/spans/stats", self._span_stats),
            route("PUT", rf"{root}/spans/feedback-scores", self._score_spans),
            route("GET", rf"{root}/spans/feedback-scores/names", self._span_score_names),
            route("POST", rf"{root}/spans/{uid}/comments", self._add_span_comment),
            route("GET", rf"{root}/spans", self._list_spans),
            route("GET", rf"{root}/spans/{uid}", self._get_span),
            # -- prompts --
            route("POST", rf"{root}/prompts/versions/retrieve", self._retrieve_version),
            route("POST", rf"{root}/prompts/versions", self._create_version),
            route("GET", rf"{root}/prompts", self._list_prompts),
            route("POST", rf"{root}/prompts", self._create_prompt),
            route("GET", rf"{root}/prompts/{uid}/versions", self._list_versions),
            route(
                "POST",
                rf"{root}/prompts/{uid}/versions/{uid}/restore",
                self._restore_version,
            ),
            route("GET", rf"{root}/prompts/{uid}", self._get_prompt),
            # -- datasets --
            route("POST", rf"{root}/datasets/retrieve", self._retrieve_dataset),
            route("PUT", rf"{root}/datasets/items", self._upsert_dataset_items),
            route("POST", rf"{root}/datasets/items/stream", self._stream_dataset_items),
            route("POST", rf"{root}/datasets/items/delete", self._delete_dataset_items),
            route("GET", rf"{root}/datasets", self._list_datasets),
            route("POST", rf"{root}/datasets", self._create_dataset),
            route(
                "GET",
                rf"{root}/datasets/{uid}/items/experiments/items",
                self._list_dataset_items_with_experiments,
            ),
            route("GET", rf"{root}/datasets/{uid}/items", self._list_dataset_items),
            route("GET", rf"{root}/datasets/{uid}", self._get_dataset),
            route("DELETE", rf"{root}/datasets/{uid}", self._delete_dataset),
            # -- experiments --
            route("PUT", rf"{root}/experiments/items/bulk", self._create_experiment_items),
            route("POST", rf"{root}/experiments/items", self._link_experiment_items),
            route(
                "GET",
                rf"{root}/experiments/groups/aggregations",
                self._experiment_group_aggregations,
            ),
            route("GET", rf"{root}/experiments/groups", self._experiment_groups),
            route("POST", rf"{root}/experiments/delete", self._delete_experiments),
            route("GET", rf"{root}/experiments", self._list_experiments),
            route("POST", rf"{root}/experiments", self._create_experiment),
            route("GET", rf"{root}/experiments/{uid}", self._get_experiment),
            # -- guardrails --
            route(
                "POST",
                rf"{root}/automations/evaluators/delete",
                self._delete_guardrails,
            ),
            route("GET", rf"{root}/automations/evaluators", self._list_guardrails),
            route("POST", rf"{root}/automations/evaluators", self._create_guardrail),
            route("GET", rf"{root}/automations/evaluators/{uid}", self._get_guardrail),
            route("PATCH", rf"{root}/automations/evaluators/{uid}", self._update_guardrail),
            route("POST", rf"{root}/guardrails", self._create_guardrail_results),
            route("POST", re.escape(CHECKER_PATH), self._evaluate_guardrails),
            # -- alerts --
            route("POST", rf"{root}/alerts/webhooks/tests", self._test_webhook),
            route("GET", rf"{root}/alerts/webhooks/examples", self._webhook_examples),
            route("POST", rf"{root}/alerts/delete", self._delete_alerts),
            route("GET", rf"{root}/alerts", self._list_alerts),
            route("POST", rf"{root}/alerts", self._create_alert),
            route("GET", rf"{root}/alerts/{uid}", self._get_alert),
            route("PUT", rf"{root}/alerts/{uid}", self._update_alert),
            # -- costs and usage --
            route("POST", rf"{root}/workspaces/costs/summaries", self._cost_summary),
            route("POST", rf"{root}/workspaces/costs", self._cost_series),
            route("POST", rf"{root}/workspaces/metrics/spans", self._workspace_usage),
            route("POST", rf"{root}/workspaces/token-usage/names", self._token_usage_names),
        ]

    # -- response builders -------------------------------------------------

    @staticmethod
    def _created(location: str) -> tuple[int, None, dict[str, str]]:
        """A create: 201, empty body, and the new id on ``Location``."""
        return 201, None, {"Location": location}

    @staticmethod
    def _ndjson(rows: Iterable[Mapping[str, Any]]) -> tuple[int, bytes, dict[str, str]]:
        """A stream: newline-delimited JSON, one row per line."""
        body = "\n".join(json.dumps(dict(row), default=str) for row in rows)
        return (
            200,
            (body + "\n" if body else "").encode(),
            {"content-type": "application/x-ndjson"},
        )

    @staticmethod
    def _query_one(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    # -- health ------------------------------------------------------------

    def _health(self, _match: re.Match, _query: dict, _body: Any) -> JsonObject:
        return {"healthy": self.healthy}

    # -- projects ----------------------------------------------------------

    def _project_by_name(self, name: str | None) -> JsonObject | None:
        if not name:
            return None
        for project in self.projects.values():
            if project["name"] == name:
                return project
        return None

    def _project_for(self, row: Mapping[str, Any]) -> JsonObject | None:
        identifier = row.get("project_id")
        if isinstance(identifier, str) and identifier in self.projects:
            return self.projects[identifier]
        return self._project_by_name(row.get("project_name"))

    def _list_projects(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        name = self._query_one(query, "name")
        rows = [
            dict(project)
            for project in self.projects.values()
            if not name or project["name"].startswith(name)
        ]
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 100),
        )

    def _get_project(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        project = self.projects.get(match.group(1))
        if project is None:
            raise EngineFailure(404, {"errors": ["project not found"]})
        return dict(project)

    def _create_project(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        name = (body or {}).get("name")
        if not name:
            raise EngineFailure(400, {"errors": ["name is required"]})
        if self._project_by_name(name) is not None:
            raise EngineFailure(409, {"errors": ["a project with that name exists"]})
        project = self.add_project(name)
        project["description"] = (body or {}).get("description")
        project["visibility"] = (body or {}).get("visibility") or "private"
        return self._created(f"{API_ROOT}/projects/{project['id']}")

    def _retrieve_project(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        project = self._project_by_name((body or {}).get("name"))
        if project is None:
            raise EngineFailure(404, {"errors": ["project not found"]})
        payload = dict(project)
        if (body or {}).get("include_stats"):
            payload["stats"] = self._stats_for_project(project)
        return payload

    def _stats_for_project(
        self, project: Mapping[str, Any], *, since: Any = None, until: Any = None
    ) -> JsonObject:
        """Measurements over one project's traces, in the engine's stats shape."""
        traces = [
            row
            for row in self.traces.values()
            if row.get("project_id") == project["id"] and _within(row, "start_time", since, until)
        ]
        errors = sum(1 for row in traces if row.get("error_info"))
        usage_totals: dict[str, float] = {}
        usage_rows: dict[str, int] = {}
        cost = 0.0
        durations: list[float] = []
        for row in traces:
            usage = row.get("usage")
            if isinstance(usage, Mapping):
                for key, value in usage.items():
                    try:
                        usage_totals[key] = usage_totals.get(key, 0.0) + float(value)
                    except (TypeError, ValueError):
                        continue
                    usage_rows[key] = usage_rows.get(key, 0) + 1
            cost += float(row.get("total_estimated_cost") or 0)
            duration = row.get("duration")
            if isinstance(duration, (int, float)):
                durations.append(float(duration))
        durations.sort()

        def percentile(fraction: float) -> float | None:
            if not durations:
                return None
            index = min(len(durations) - 1, int(round(fraction * (len(durations) - 1))))
            return durations[index]

        # Shapes mirror the real engine exactly: the failure tally is an object
        # with a deviation, `usage` is an AVERAGE per key (avgMap) while
        # `total_estimated_cost` is the average and only `_sum` is the total,
        # and durations come as p50/p90/p99 — there is no p95. Reading the
        # average as a total is exactly right for one trace and silently wrong
        # for two, which is why this shape must not be simplified.
        return {
            "trace_count": len(traces),
            "error_count": {"count": errors, "deviation": 0},
            "usage": {
                key: total / usage_rows[key]
                for key, total in usage_totals.items()
                if usage_rows.get(key)
            },
            "total_estimated_cost": round(cost / len(traces), 9) if traces else 0.0,
            "total_estimated_cost_sum": round(cost, 9),
            "duration": {"p50": percentile(0.5), "p90": percentile(0.9), "p99": percentile(0.99)},
            "feedback_scores": [],
            "guardrails_failed_count": 0,
        }

    def _project_stats(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        since = self._query_one(query, "from_time")
        until = self._query_one(query, "to_time")
        name = self._query_one(query, "name")
        rows: list[JsonObject] = []
        for project in self.projects.values():
            if name and not project["name"].startswith(name):
                continue
            rows.append(
                {
                    "id": project["id"],
                    "project_id": project["id"],
                    "name": project["name"],
                    **self._stats_for_project(project, since=since, until=until),
                }
            )
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 100),
        )

    #: The real engine's metric-type vocabulary. A name outside this enum is a
    #: deserialization failure there, so it must be one here too — otherwise a
    #: control-plane typo passes the suite and 400s in production.
    METRIC_TYPES: frozenset[str] = frozenset(
        {
            "FEEDBACK_SCORES",
            "TRACE_COUNT",
            "TOKEN_USAGE",
            "DURATION",
            "COST",
            "GUARDRAILS_FAILED_COUNT",
            "THREAD_COUNT",
            "THREAD_DURATION",
            "THREAD_FEEDBACK_SCORES",
            "SPAN_FEEDBACK_SCORES",
            "SPAN_COUNT",
            "SPAN_DURATION",
            "SPAN_TOKEN_USAGE",
            "TRACE_AVERAGE_DURATION",
            "TRACE_ERROR_RATE",
            "SPAN_AVERAGE_DURATION",
            "SPAN_COST",
            "SPAN_ERROR_RATE",
            "THREAD_AVERAGE_DURATION",
            "THREAD_COST",
        }
    )

    def _project_metrics(self, match: re.Match, _query: dict, body: Any) -> JsonObject:
        project = self.projects.get(match.group(1))
        if project is None:
            raise EngineFailure(404, {"errors": ["project not found"]})
        metric = str((body or {}).get("metric_type") or "TRACE_COUNT")
        if metric not in self.METRIC_TYPES:
            raise EngineFailure(
                400,
                {"errors": [f'Cannot deserialize value of type `MetricType` from "{metric}"']},
            )
        start = _parse_instant((body or {}).get("interval_start")) or _now()
        end = _parse_instant((body or {}).get("interval_end")) or _now()
        traces = [
            row
            for row in self.traces.values()
            if row.get("project_id") == project["id"]
            and _within(row, "start_time", start, end)
        ]

        def bucket_of(row: Mapping[str, Any]) -> str | None:
            moment = _parse_instant(row.get("start_time"))
            if moment is None:
                return None
            return _iso(moment.replace(minute=0, second=0, microsecond=0))

        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in traces:
            key = bucket_of(row)
            if key is not None:
                grouped.setdefault(key, []).append(row)

        def line(name: str, value_of: Any) -> JsonObject:
            return {
                "name": name,
                "data": [
                    {"time": moment, "value": value_of(rows)}
                    for moment, rows in sorted(grouped.items())
                ],
            }

        def durations(rows: list[Mapping[str, Any]]) -> list[float]:
            return sorted(
                float(row["duration"])
                for row in rows
                if isinstance(row.get("duration"), (int, float))
            )

        def pct(rows: list[Mapping[str, Any]], fraction: float) -> float | None:
            values = durations(rows)
            if not values:
                return None
            return values[min(len(values) - 1, int(round(fraction * (len(values) - 1))))]

        if metric == "DURATION":
            results = [
                line("duration.p50", lambda rows: pct(rows, 0.5)),
                line("duration.p90", lambda rows: pct(rows, 0.9)),
                line("duration.p99", lambda rows: pct(rows, 0.99)),
            ]
        elif metric == "TRACE_ERROR_RATE":
            # Percentage of the bucket's traces that failed, as the real SQL
            # computes it: countIf(error) * 100 / count().
            results = [
                line(
                    "trace_error_rate",
                    lambda rows: round(
                        sum(1 for row in rows if row.get("error_info")) * 100.0 / len(rows), 6
                    )
                    if rows
                    else 0.0,
                )
            ]
        elif metric == "COST":
            results = [
                line(
                    "cost",
                    lambda rows: round(
                        sum(float(row.get("total_estimated_cost") or 0) for row in rows), 6
                    ),
                )
            ]
        elif metric == "TOKEN_USAGE":
            def tokens_of(rows: list[Mapping[str, Any]]) -> float:
                total = 0.0
                for row in rows:
                    usage = row.get("usage")
                    if isinstance(usage, Mapping):
                        total += float(usage.get("total_tokens") or 0)
                return total

            results = [line("total_tokens", tokens_of)]
        else:
            results = [line("trace_count", lambda rows: float(len(rows)))]
        return {"results": results}

    # -- traces ------------------------------------------------------------

    def _store_trace(self, payload: Mapping[str, Any]) -> JsonObject:
        row = dict(payload)
        project_name = row.get("project_name")
        project = self._project_for(row)
        if project is None and project_name:
            project = self.add_project(str(project_name))
        if project is not None:
            row["project_id"] = project["id"]
            row["project_name"] = project["name"]
            # Stamped when a trace is *written*, not with the trace's own start
            # time -- which is what lets a reader skip a project that has had
            # nothing written to it since before the window it is asking about.
            project["last_updated_trace_at"] = _iso()
        row.setdefault("id", _new_id())
        row.setdefault("start_time", _iso())
        existing = self.traces.get(row["id"], {})
        merged = {**existing, **row}
        merged.setdefault("feedback_scores", list(existing.get("feedback_scores") or []))
        merged.setdefault("comments", list(existing.get("comments") or []))
        merged["duration"] = self._duration_ms(merged)
        merged["span_count"] = self._span_count(merged["id"])
        merged["llm_span_count"] = self._span_count(merged["id"], span_type="llm")
        merged["usage"] = self._trace_usage(merged["id"], merged)
        merged["total_estimated_cost"] = self._trace_cost(merged["id"], merged)
        merged.setdefault("last_updated_at", _iso())
        self.traces[merged["id"]] = merged
        return merged

    @staticmethod
    def _duration_ms(row: Mapping[str, Any]) -> float | None:
        start = _parse_instant(row.get("start_time"))
        end = _parse_instant(row.get("end_time"))
        if start is None or end is None:
            return None
        return round((end - start).total_seconds() * 1000, 3)

    def _span_count(self, trace_id: str, *, span_type: str | None = None) -> int:
        return sum(
            1
            for span in self.spans.values()
            if span.get("trace_id") == trace_id
            and (span_type is None or span.get("type") == span_type)
        )

    def _trace_usage(self, trace_id: str, row: Mapping[str, Any]) -> JsonObject:
        """A trace's usage is the sum of its spans', as the engine rolls it up."""
        totals: dict[str, float] = {}
        for span in self.spans.values():
            if span.get("trace_id") != trace_id:
                continue
            usage = span.get("usage")
            if not isinstance(usage, Mapping):
                continue
            for key, value in usage.items():
                try:
                    totals[key] = totals.get(key, 0.0) + float(value)
                except (TypeError, ValueError):
                    continue
        if not totals:
            declared = row.get("usage")
            return dict(declared) if isinstance(declared, Mapping) else {}
        if "total_tokens" not in totals:
            totals["total_tokens"] = totals.get("prompt_tokens", 0.0) + totals.get(
                "completion_tokens", 0.0
            )
        return {key: int(value) for key, value in totals.items()}

    def _trace_cost(self, trace_id: str, row: Mapping[str, Any]) -> float:
        total = 0.0
        seen = False
        for span in self.spans.values():
            if span.get("trace_id") != trace_id:
                continue
            cost = span.get("total_estimated_cost")
            if isinstance(cost, (int, float)):
                total += float(cost)
                seen = True
        if seen:
            return round(total, 6)
        declared = row.get("total_estimated_cost")
        return float(declared) if isinstance(declared, (int, float)) else 0.0

    def _refresh_trace(self, trace_id: str) -> None:
        row = self.traces.get(trace_id)
        if row is not None:
            self._store_trace(row)

    def _create_traces(self, _match: re.Match, _query: dict, body: Any) -> None:
        rows = (body or {}).get("traces")
        if not isinstance(rows, list):
            raise EngineFailure(400, {"errors": ["'traces' must be an array"]})
        for row in rows:
            if not isinstance(row, Mapping):
                raise EngineFailure(400, {"errors": ["each trace must be an object"]})
            if not row.get("project_name") and not row.get("project_id"):
                raise EngineFailure(400, {"errors": ["a trace needs a project"]})
            self._store_trace(row)
        return None

    def _trace_scope(self, query: dict) -> list[JsonObject]:
        project_id = self._query_one(query, "project_id")
        project_name = self._query_one(query, "project_name")
        rows = list(self.traces.values())
        if project_id:
            rows = [row for row in rows if row.get("project_id") == project_id]
        if project_name:
            rows = [row for row in rows if row.get("project_name") == project_name]
        return rows

    def _list_traces(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        rows = self._trace_scope(query)
        since = self._query_one(query, "from_time")
        until = self._query_one(query, "to_time")
        exclude = set(_json_param(self._query_one(query, "exclude")) or [])
        filters = _json_param(self._query_one(query, "filters"))
        search = self._query_one(query, "search")
        thread = self._query_one(query, "annotation_queue_id")
        selected = [
            row
            for row in rows
            if _within(row, "start_time", since, until)
            and row["id"] not in exclude
            and _matches_filters(row, filters)
            and _matches_search(row, search, ("name", "input", "output", "thread_id"))
            and (thread is None or row.get("thread_id") == thread)
        ]
        selected.sort(key=lambda row: str(row.get("start_time")), reverse=True)
        selected = _sorted_rows(selected, _json_param(self._query_one(query, "sorting")))
        return _page(
            selected,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _search_traces(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = body or {}
        rows = list(self.traces.values())
        if payload.get("project_id"):
            rows = [row for row in rows if row.get("project_id") == payload["project_id"]]
        if payload.get("project_name"):
            rows = [row for row in rows if row.get("project_name") == payload["project_name"]]
        exclude = set(payload.get("exclude") or [])
        rows = [
            row
            for row in rows
            if _within(row, "start_time", payload.get("from_time"), payload.get("to_time"))
            and row["id"] not in exclude
            and _matches_filters(row, payload.get("filters"))
        ]
        return self._ndjson(self._cursor(rows, payload.get("last_retrieved_id"), payload))

    @staticmethod
    def _cursor(
        rows: list[JsonObject],
        after: Any,
        payload: Mapping[str, Any],
        *,
        key: str = "id",
    ) -> list[JsonObject]:
        """Stream newest-first and return the page after ``after``.

        The real engine's search endpoints stream by id *descending* — a
        UUIDv7 id is a timestamp, so the newest row comes first — and
        ``last_retrieved_id`` resumes with strictly older rows. A short page
        is the end-of-stream signal the control plane's scan loops terminate
        on, so the cap is applied to the *remaining* rows, not the whole set.
        """
        ordered = sorted(rows, key=lambda row: str(row.get(key)), reverse=True)
        if after:
            ordered = [row for row in ordered if str(row.get(key)) < str(after)]
        limit = _as_int(payload.get("limit"), 500)
        return ordered[: max(limit, 0)]

    def _get_trace(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        trace = self.traces.get(match.group(1))
        if trace is None:
            raise EngineFailure(404, {"errors": ["trace not found"]})
        return dict(trace)

    def _trace_stats(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        project_id = self._query_one(query, "project_id")
        project_name = self._query_one(query, "project_name")
        project: JsonObject | None = None
        if project_id:
            project = self.projects.get(project_id)
        elif project_name:
            project = self._project_by_name(project_name)
        if project is None:
            return {"stats": []}
        stats = self._stats_for_project(
            project,
            since=self._query_one(query, "from_time"),
            until=self._query_one(query, "to_time"),
        )
        count = stats["trace_count"]
        # The real endpoint answers a flat list of dotted names: per-trace
        # AVERAGES under `usage.*`/`total_estimated_cost`, and TOTALS only
        # under `usage_sum.*`/`total_estimated_cost_sum`.
        rows: list[JsonObject] = [
            {"name": "trace_count", "value": count, "type": "COUNT"},
            {"name": "duration", "value": stats["duration"], "type": "PERCENTAGE"},
            {"name": "error_count", "value": stats["error_count"], "type": "COUNT"},
            {
                "name": "total_estimated_cost",
                "value": stats["total_estimated_cost"],
                "type": "AVG",
            },
            {
                "name": "total_estimated_cost_sum",
                "value": stats["total_estimated_cost_sum"],
                "type": "AVG",
            },
        ]
        for key, average in stats["usage"].items():
            rows.append({"name": f"usage.{key}", "value": average, "type": "AVG"})
        traces = [
            row
            for row in self.traces.values()
            if row.get("project_id") == project["id"]
            and _within(
                row,
                "start_time",
                self._query_one(query, "from_time"),
                self._query_one(query, "to_time"),
            )
        ]
        sums: dict[str, float] = {}
        span_counts = 0
        for row in traces:
            usage = row.get("usage")
            if isinstance(usage, Mapping):
                for key, value in usage.items():
                    try:
                        sums[key] = sums.get(key, 0.0) + float(value)
                    except (TypeError, ValueError):
                        continue
            span_counts += int(row.get("span_count") or 0)
        for key, total in sums.items():
            rows.append({"name": f"usage_sum.{key}", "value": total, "type": "AVG"})
        if count:
            rows.append({"name": "span_count", "value": span_counts / count, "type": "AVG"})
        return {"stats": rows}

    def _update_trace(self, match: re.Match, _query: dict, body: Any) -> None:
        trace = self.traces.get(match.group(1))
        if trace is None:
            raise EngineFailure(404, {"errors": ["trace not found"]})
        trace.update(body or {})
        trace["last_updated_at"] = _iso()
        return None

    def _delete_traces(self, _match: re.Match, _query: dict, body: Any) -> None:
        for trace_id in (body or {}).get("ids") or []:
            self.traces.pop(str(trace_id), None)
            for span_id in [
                span_id
                for span_id, span in self.spans.items()
                if span.get("trace_id") == str(trace_id)
            ]:
                self.spans.pop(span_id, None)
        return None

    # -- spans -------------------------------------------------------------

    def _store_span(self, payload: Mapping[str, Any]) -> JsonObject:
        row = dict(payload)
        project = self._project_for(row)
        if project is None and row.get("project_name"):
            project = self.add_project(str(row["project_name"]))
        if project is not None:
            row["project_id"] = project["id"]
            row["project_name"] = project["name"]
        row.setdefault("id", _new_id())
        row.setdefault("type", "general")
        existing = self.spans.get(row["id"], {})
        merged = {**existing, **row}
        merged.setdefault("feedback_scores", list(existing.get("feedback_scores") or []))
        merged["duration"] = self._duration_ms(merged)
        self.spans[merged["id"]] = merged
        return merged

    def _create_spans(self, _match: re.Match, _query: dict, body: Any) -> None:
        rows = (body or {}).get("spans")
        if not isinstance(rows, list):
            raise EngineFailure(400, {"errors": ["'spans' must be an array"]})
        touched: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise EngineFailure(400, {"errors": ["each span must be an object"]})
            if not row.get("trace_id"):
                raise EngineFailure(400, {"errors": ["a span needs its trace_id"]})
            stored = self._store_span(row)
            touched.add(str(stored.get("trace_id")))
        for trace_id in touched:
            self._refresh_trace(trace_id)
        return None

    def _list_spans(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        rows = list(self.spans.values())
        trace_id = self._query_one(query, "trace_id")
        project_id = self._query_one(query, "project_id")
        project_name = self._query_one(query, "project_name")
        span_type = self._query_one(query, "type")
        if trace_id:
            rows = [row for row in rows if row.get("trace_id") == trace_id]
        if project_id:
            rows = [row for row in rows if row.get("project_id") == project_id]
        if project_name:
            rows = [row for row in rows if row.get("project_name") == project_name]
        if span_type:
            rows = [row for row in rows if row.get("type") == span_type]
        exclude = set(_json_param(self._query_one(query, "exclude")) or [])
        filters = _json_param(self._query_one(query, "filters"))
        search = self._query_one(query, "search")
        rows = [
            row
            for row in rows
            if _within(
                row,
                "start_time",
                self._query_one(query, "from_time"),
                self._query_one(query, "to_time"),
            )
            and row["id"] not in exclude
            and _matches_filters(row, filters)
            and _matches_search(row, search, ("name", "input", "output", "model"))
        ]
        rows.sort(key=lambda row: str(row.get("start_time")))
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 100),
        )

    def _search_spans(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = body or {}
        rows = list(self.spans.values())
        for field, value in (
            ("trace_id", payload.get("trace_id")),
            ("project_id", payload.get("project_id")),
            ("project_name", payload.get("project_name")),
            ("type", payload.get("type")),
        ):
            if value:
                rows = [row for row in rows if row.get(field) == value]
        exclude = set(payload.get("exclude") or [])
        rows = [
            row
            for row in rows
            if _within(row, "start_time", payload.get("from_time"), payload.get("to_time"))
            and row["id"] not in exclude
            and _matches_filters(row, payload.get("filters"))
        ]
        return self._ndjson(self._cursor(rows, payload.get("last_retrieved_id"), payload))

    def _get_span(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        span = self.spans.get(match.group(1))
        if span is None:
            raise EngineFailure(404, {"errors": ["span not found"]})
        return dict(span)

    def _span_stats(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        rows = list(self.spans.values())
        for field, param in (
            ("project_id", "project_id"),
            ("project_name", "project_name"),
            ("trace_id", "trace_id"),
            ("type", "type"),
        ):
            value = self._query_one(query, param)
            if value:
                rows = [row for row in rows if row.get(field) == value]
        tokens = 0.0
        cost = 0.0
        for row in rows:
            usage = row.get("usage")
            if isinstance(usage, Mapping):
                tokens += float(usage.get("total_tokens") or 0)
            cost += float(row.get("total_estimated_cost") or 0)
        return {
            "stats": [
                {"name": "span_count", "value": len(rows)},
                {"name": "total_tokens", "value": tokens},
                {"name": "total_estimated_cost", "value": round(cost, 6)},
            ]
        }

    # -- threads -----------------------------------------------------------

    def _threads(self) -> list[JsonObject]:
        """Threads are derived from the traces that carry a ``thread_id``."""
        grouped: dict[tuple[str, str], list[JsonObject]] = {}
        for trace in self.traces.values():
            thread_id = trace.get("thread_id")
            if not thread_id:
                continue
            grouped.setdefault((str(trace.get("project_id")), str(thread_id)), []).append(trace)

        threads: list[JsonObject] = []
        for (project_id, thread_id), traces in grouped.items():
            traces.sort(key=lambda row: str(row.get("start_time")))
            project = self.projects.get(project_id)
            scores = [
                score
                for score in self.thread_scores
                if score.get("thread_id") == thread_id
                and (
                    score.get("project_name") is None
                    or project is None
                    or score.get("project_name") == project["name"]
                )
            ]
            threads.append(
                {
                    # The engine gives a thread its own surrogate id as well as
                    # the business id the caller addressed it by.
                    "id": f"{project_id}:{thread_id}",
                    "thread_model_id": f"{project_id}:{thread_id}",
                    "thread_id": thread_id,
                    "project_id": project_id,
                    "project_name": project["name"] if project else None,
                    "start_time": traces[0].get("start_time"),
                    "end_time": traces[-1].get("end_time"),
                    "duration": self._duration_ms(
                        {
                            "start_time": traces[0].get("start_time"),
                            "end_time": traces[-1].get("end_time"),
                        }
                    ),
                    "number_of_messages": len(traces),
                    "trace_count": len(traces),
                    "total_estimated_cost": round(
                        sum(float(row.get("total_estimated_cost") or 0) for row in traces), 6
                    ),
                    "usage": {
                        "total_tokens": sum(
                            int((row.get("usage") or {}).get("total_tokens") or 0)
                            for row in traces
                        )
                    },
                    "status": "inactive",
                    "last_updated_at": traces[-1].get("last_updated_at"),
                    "feedback_scores": scores,
                    "tags": sorted(
                        {tag for row in traces for tag in (row.get("tags") or []) if tag}
                    ),
                }
            )
        return threads

    def _thread_scope(self, project_id: Any, project_name: Any) -> list[JsonObject]:
        rows = self._threads()
        if project_id:
            rows = [row for row in rows if row.get("project_id") == project_id]
        if project_name:
            rows = [row for row in rows if row.get("project_name") == project_name]
        return rows

    def _list_threads(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        rows = self._thread_scope(
            self._query_one(query, "project_id"), self._query_one(query, "project_name")
        )
        filters = _json_param(self._query_one(query, "filters"))
        search = self._query_one(query, "search")
        rows = [
            row
            for row in rows
            if _within(
                row,
                "start_time",
                self._query_one(query, "from_time"),
                self._query_one(query, "to_time"),
            )
            and _matches_filters(row, filters)
            and _matches_search(row, search, ("thread_id",))
        ]
        rows.sort(key=lambda row: str(row.get("start_time")), reverse=True)
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _retrieve_thread(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        payload = body or {}
        rows = self._thread_scope(payload.get("project_id"), payload.get("project_name"))
        for row in rows:
            if row.get("thread_id") == payload.get("thread_id"):
                return dict(row)
        raise EngineFailure(404, {"errors": ["thread not found"]})

    def _search_threads(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = body or {}
        rows = self._thread_scope(payload.get("project_id"), payload.get("project_name"))
        rows = [
            row
            for row in rows
            if _within(row, "start_time", payload.get("from_time"), payload.get("to_time"))
            and _matches_filters(row, payload.get("filters"))
        ]
        return self._ndjson(
            self._cursor(
                rows,
                payload.get("last_retrieved_thread_model_id"),
                payload,
                key="thread_model_id",
            )
        )

    def _thread_stats(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        rows = self._thread_scope(
            self._query_one(query, "project_id"), self._query_one(query, "project_name")
        )
        return {
            "stats": [
                {"name": "thread_count", "value": len(rows)},
                {
                    "name": "number_of_messages",
                    "value": sum(int(row.get("number_of_messages") or 0) for row in rows),
                },
            ]
        }

    # -- feedback scores ---------------------------------------------------

    @staticmethod
    def _merge_score(row: JsonObject, score: Mapping[str, Any]) -> None:
        """Scores are keyed by name: a second write of the same name replaces it."""
        source = str(score.get("source") or "sdk").lower()
        if source not in ("ui", "sdk", "online_scoring"):
            # The engine's ScoreSource enum; an unknown value 400s the whole
            # batch (seen in production with "experiment" on 2026-08-24).
            raise EngineFailure(
                400,
                {"message": f"{source} was not one of [UI, SDK, ONLINE_SCORING]"},
            )
        scores = [
            existing
            for existing in (row.get("feedback_scores") or [])
            if existing.get("name") != score.get("name")
        ]
        scores.append(
            {
                "name": score.get("name"),
                "value": score.get("value"),
                "category_name": score.get("category_name"),
                "reason": score.get("reason"),
                "source": score.get("source") or "sdk",
                "created_at": _iso(),
            }
        )
        row["feedback_scores"] = scores

    def _score_traces(self, _match: re.Match, _query: dict, body: Any) -> None:
        for score in (body or {}).get("scores") or []:
            trace = self.traces.get(str(score.get("id")))
            if trace is None:
                raise EngineFailure(404, {"errors": ["trace not found for a score"]})
            self._merge_score(trace, score)
        return None

    def _score_spans(self, _match: re.Match, _query: dict, body: Any) -> None:
        for score in (body or {}).get("scores") or []:
            span = self.spans.get(str(score.get("id")))
            if span is None:
                raise EngineFailure(404, {"errors": ["span not found for a score"]})
            self._merge_score(span, score)
        return None

    def _score_threads(self, _match: re.Match, _query: dict, body: Any) -> None:
        for score in (body or {}).get("scores") or []:
            self.thread_scores = [
                existing
                for existing in self.thread_scores
                if not (
                    existing.get("thread_id") == score.get("thread_id")
                    and existing.get("name") == score.get("name")
                )
            ]
            self.thread_scores.append({**dict(score), "created_at": _iso()})
        return None

    def _score_names(self, rows: Iterable[Mapping[str, Any]]) -> JsonObject:
        names: set[str] = set()
        for row in rows:
            for score in row.get("feedback_scores") or []:
                name = score.get("name")
                if name:
                    names.add(str(name))
        return {"scores": [{"name": name} for name in sorted(names)]}

    def _trace_score_names(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        project_id = self._query_one(query, "project_id")
        return self._score_names(
            row
            for row in self.traces.values()
            if not project_id or row.get("project_id") == project_id
        )

    def _span_score_names(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        project_id = self._query_one(query, "project_id")
        span_type = self._query_one(query, "type")
        return self._score_names(
            row
            for row in self.spans.values()
            if (not project_id or row.get("project_id") == project_id)
            and (not span_type or row.get("type") == span_type)
        )

    def _thread_score_names(self, _match: re.Match, _query: dict, _body: Any) -> JsonObject:
        names = sorted({str(score.get("name")) for score in self.thread_scores if score.get("name")})
        return {"scores": [{"name": name} for name in names]}

    # -- comments ----------------------------------------------------------

    def _add_comment(self, *, text: Any, trace_id: str | None, span_id: str | None) -> JsonObject:
        if not isinstance(text, str) or not text.strip():
            raise EngineFailure(400, {"errors": ["a comment needs text"]})
        comment = {
            "id": _new_id(),
            "text": text,
            "trace_id": trace_id,
            "span_id": span_id,
            "created_at": _iso(),
            "created_by": "control-plane",
        }
        self.comments[comment["id"]] = comment
        owner = self.traces.get(trace_id or "") or self.spans.get(span_id or "")
        if owner is not None:
            owner.setdefault("comments", []).append(comment)
        return comment

    def _add_trace_comment(self, match: re.Match, _query: dict, body: Any) -> JsonObject:
        trace_id = match.group(1)
        if trace_id not in self.traces:
            raise EngineFailure(404, {"errors": ["trace not found"]})
        return self._add_comment(text=(body or {}).get("text"), trace_id=trace_id, span_id=None)

    def _get_trace_comment(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        comment = self.comments.get(match.group(2))
        if comment is None or comment.get("trace_id") != match.group(1):
            raise EngineFailure(404, {"errors": ["comment not found"]})
        return dict(comment)

    def _update_trace_comment(self, match: re.Match, _query: dict, body: Any) -> None:
        comment = self.comments.get(match.group(1))
        if comment is None:
            raise EngineFailure(404, {"errors": ["comment not found"]})
        comment["text"] = (body or {}).get("text")
        return None

    def _delete_trace_comments(self, _match: re.Match, _query: dict, body: Any) -> None:
        for comment_id in (body or {}).get("ids") or []:
            comment = self.comments.pop(str(comment_id), None)
            if comment is None:
                continue
            owner = self.traces.get(comment.get("trace_id") or "")
            if owner is not None:
                owner["comments"] = [
                    row for row in owner.get("comments") or [] if row.get("id") != comment_id
                ]
        return None

    def _add_span_comment(self, match: re.Match, _query: dict, body: Any) -> JsonObject:
        span_id = match.group(1)
        if span_id not in self.spans:
            raise EngineFailure(404, {"errors": ["span not found"]})
        return self._add_comment(text=(body or {}).get("text"), trace_id=None, span_id=span_id)

    # -- prompts -----------------------------------------------------------

    def _prompt_by_name(self, name: Any) -> JsonObject | None:
        for prompt in self.prompts.values():
            if prompt["name"] == name:
                return prompt
        return None

    def _list_prompts(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        name = self._query_one(query, "name")
        project_id = self._query_one(query, "project_id")
        rows = [
            dict(prompt)
            for prompt in self.prompts.values()
            if (not name or str(prompt["name"]).startswith(name))
            and (not project_id or prompt.get("project_id") == project_id)
        ]
        rows = [row for row in rows if _matches_filters(row, _json_param(self._query_one(query, "filters")))]
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _new_prompt(self, payload: Mapping[str, Any]) -> JsonObject:
        prompt: JsonObject = {
            "id": _new_id(),
            "name": payload.get("name"),
            "description": payload.get("description"),
            "tags": list(payload.get("tags") or []),
            "metadata": dict(payload.get("metadata") or {}),
            "project_id": payload.get("project_id"),
            "created_at": _iso(),
            "last_updated_at": _iso(),
            "version_count": 0,
            "latest_version": None,
        }
        self.prompts[prompt["id"]] = prompt
        self.prompt_versions[prompt["id"]] = []
        if payload.get("template"):
            self._append_version(prompt, {"template": payload.get("template")})
        return prompt

    def _append_version(self, prompt: JsonObject, version: Mapping[str, Any]) -> JsonObject:
        versions = self.prompt_versions.setdefault(prompt["id"], [])
        commit = str(version.get("commit") or _new_id().replace("-", "")[:8])
        row: JsonObject = {
            "id": _new_id(),
            "prompt_id": prompt["id"],
            "name": prompt["name"],
            "commit": commit,
            "template": version.get("template"),
            "type": version.get("type") or "mustache",
            "metadata": dict(version.get("metadata") or {}),
            "change_description": version.get("change_description"),
            "variables": list(version.get("variables") or []),
            "version_number": len(versions) + 1,
            "created_at": _iso(),
            "created_by": "control-plane",
        }
        versions.append(row)
        prompt["latest_version"] = dict(row)
        prompt["version_count"] = len(versions)
        prompt["last_updated_at"] = _iso()
        return row

    def _create_prompt(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = body or {}
        if not payload.get("name"):
            raise EngineFailure(400, {"errors": ["a prompt needs a name"]})
        if self._prompt_by_name(payload["name"]) is not None:
            raise EngineFailure(409, {"errors": ["a prompt with that name exists"]})
        prompt = self._new_prompt(payload)
        return self._created(f"{API_ROOT}/prompts/{prompt['id']}")

    def _get_prompt(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        prompt = self.prompts.get(match.group(1))
        if prompt is None:
            raise EngineFailure(404, {"errors": ["prompt not found"]})
        return dict(prompt)

    def _create_version(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        payload = body or {}
        name = payload.get("name")
        if not name:
            raise EngineFailure(400, {"errors": ["a version needs its prompt name"]})
        prompt = self._prompt_by_name(name)
        if prompt is None:
            prompt = self._new_prompt({"name": name, "project_id": payload.get("project_id")})
        version = payload.get("version")
        if not isinstance(version, Mapping):
            raise EngineFailure(400, {"errors": ["'version' must be an object"]})
        return self._append_version(prompt, version)

    def _list_versions(self, match: re.Match, query: dict, _body: Any) -> JsonObject:
        prompt_id = match.group(1)
        if prompt_id not in self.prompts:
            raise EngineFailure(404, {"errors": ["prompt not found"]})
        rows = [dict(row) for row in self.prompt_versions.get(prompt_id, [])]
        search = self._query_one(query, "search")
        rows = [
            row for row in rows if _matches_search(row, search, ("commit", "template", "name"))
        ]
        rows.sort(key=lambda row: int(row.get("version_number") or 0), reverse=True)
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _retrieve_version(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        payload = body or {}
        prompt = self._prompt_by_name(payload.get("name"))
        if prompt is None:
            raise EngineFailure(404, {"errors": ["prompt not found"]})
        versions = self.prompt_versions.get(prompt["id"], [])
        if not versions:
            raise EngineFailure(404, {"errors": ["prompt has no versions"]})
        commit = payload.get("commit")
        number = payload.get("version_number")
        if commit:
            for row in versions:
                if row["commit"] == commit:
                    return dict(row)
            raise EngineFailure(404, {"errors": ["commit not found"]})
        if number:
            for row in versions:
                if str(row["version_number"]) == str(number):
                    return dict(row)
            raise EngineFailure(404, {"errors": ["version not found"]})
        return dict(versions[-1])

    def _restore_version(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        prompt = self.prompts.get(match.group(1))
        if prompt is None:
            raise EngineFailure(404, {"errors": ["prompt not found"]})
        for row in self.prompt_versions.get(prompt["id"], []):
            if row["id"] == match.group(2):
                # Restoring re-commits the old body as a new head, which is what
                # makes a rollback appear in the version history rather than
                # rewriting it.
                return self._append_version(
                    prompt,
                    {
                        "template": row.get("template"),
                        "type": row.get("type"),
                        "metadata": row.get("metadata"),
                        "change_description": f"Restored commit {row['commit']}",
                    },
                )
        raise EngineFailure(404, {"errors": ["version not found"]})

    # -- datasets ----------------------------------------------------------

    def _dataset_by_name(self, name: Any) -> JsonObject | None:
        for dataset in self.datasets.values():
            if dataset["name"] == name:
                return dataset
        return None

    def _list_datasets(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        name = self._query_one(query, "name")
        rows = [
            self._dataset_row(dataset)
            for dataset in self.datasets.values()
            if not name or str(dataset["name"]).startswith(name)
        ]
        if _as_bool(self._query_one(query, "with_experiments_only")):
            named = {
                experiment.get("dataset_name") for experiment in self.experiments.values()
            }
            rows = [row for row in rows if row["name"] in named]
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _dataset_row(self, dataset: Mapping[str, Any]) -> JsonObject:
        items = self.dataset_items.get(dataset["id"], [])
        return {**dict(dataset), "dataset_items_count": len(items), "size": len(items)}

    def _create_dataset(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = body or {}
        if not payload.get("name"):
            raise EngineFailure(400, {"errors": ["a dataset needs a name"]})
        existing = self._dataset_by_name(payload["name"])
        if existing is not None:
            raise EngineFailure(409, {"errors": ["a dataset with that name exists"]})
        dataset: JsonObject = {
            "id": _new_id(),
            "name": payload["name"],
            "description": payload.get("description"),
            "type": payload.get("type"),
            "visibility": payload.get("visibility") or "private",
            "tags": list(payload.get("tags") or []),
            "created_at": _iso(),
            "last_updated_at": _iso(),
        }
        self.datasets[dataset["id"]] = dataset
        self.dataset_items[dataset["id"]] = []
        return self._created(f"{API_ROOT}/datasets/{dataset['id']}")

    def _get_dataset(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        dataset = self.datasets.get(match.group(1))
        if dataset is None:
            raise EngineFailure(404, {"errors": ["dataset not found"]})
        return self._dataset_row(dataset)

    def _retrieve_dataset(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        dataset = self._dataset_by_name((body or {}).get("dataset_name"))
        if dataset is None:
            raise EngineFailure(404, {"errors": ["dataset not found"]})
        return self._dataset_row(dataset)

    def _resolve_dataset(self, payload: Mapping[str, Any]) -> JsonObject:
        dataset_id = payload.get("dataset_id")
        if dataset_id and dataset_id in self.datasets:
            return self.datasets[dataset_id]
        dataset = self._dataset_by_name(payload.get("dataset_name"))
        if dataset is not None:
            return dataset
        raise EngineFailure(404, {"errors": ["dataset not found"]})

    def _upsert_dataset_items(self, _match: re.Match, _query: dict, body: Any) -> None:
        payload = body or {}
        dataset = self._resolve_dataset(payload)
        items = payload.get("items")
        if not isinstance(items, list):
            raise EngineFailure(400, {"errors": ["'items' must be an array"]})
        stored = self.dataset_items.setdefault(dataset["id"], [])
        for item in items:
            if not isinstance(item, Mapping):
                raise EngineFailure(400, {"errors": ["each item must be an object"]})
            if not item.get("source"):
                # The engine requires a provenance on every case; production
                # answered 422 for exactly this omission on 2026-08-24.
                raise EngineFailure(422, {"errors": ["items[0].source must not be null"]})
            row = dict(item)
            row.setdefault("id", _new_id())
            row["batch_group_id"] = payload.get("batch_group_id")
            row.setdefault("created_at", _iso())
            # The upsert is idempotent on item id, which is why the adapter uses
            # PUT rather than POST for this call.
            for index, existing in enumerate(stored):
                if existing.get("id") == row["id"]:
                    stored[index] = row
                    break
            else:
                stored.append(row)
        return None

    def _list_dataset_items(self, match: re.Match, query: dict, _body: Any) -> JsonObject:
        dataset_id = match.group(1)
        if dataset_id not in self.datasets:
            raise EngineFailure(404, {"errors": ["dataset not found"]})
        rows = [dict(row) for row in self.dataset_items.get(dataset_id, [])]
        # The plain listing deliberately carries NO experiment_items — the real
        # engine only joins them on the comparison endpoint below, and the
        # suite runner shipped broken because the double blurred that line.
        for row in rows:
            row.pop("experiment_items", None)
        rows = [
            row
            for row in rows
            if _matches_filters(row, _json_param(self._query_one(query, "filters")))
        ]
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _list_dataset_items_with_experiments(
        self, match: re.Match, query: dict, _body: Any
    ) -> JsonObject:
        """Dataset items joined to the named experiments' items — the shape the
        suite runner and the evaluation detail read their verdicts from."""
        dataset_id = match.group(1)
        if dataset_id not in self.datasets:
            raise EngineFailure(404, {"errors": ["dataset not found"]})
        wanted = _json_param(self._query_one(query, "experiment_ids")) or []
        wanted_ids = {str(x) for x in wanted} if isinstance(wanted, list) else set()
        joined: dict[str, list[JsonObject]] = {}
        for experiment_id, items in self.experiment_items.items():
            if wanted_ids and experiment_id not in wanted_ids:
                continue
            for item in items:
                key = str(item.get("dataset_item_id") or "")
                if key:
                    stamped = dict(item)
                    stamped.setdefault("experiment_id", experiment_id)
                    joined.setdefault(key, []).append(stamped)
        rows = []
        for row in self.dataset_items.get(dataset_id, []):
            merged = dict(row)
            merged["experiment_items"] = joined.get(str(row.get("id")), [])
            rows.append(merged)
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _stream_dataset_items(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = body or {}
        dataset = self._dataset_by_name(payload.get("dataset_name"))
        if dataset is None:
            raise EngineFailure(404, {"errors": ["dataset not found"]})
        rows = [dict(row) for row in self.dataset_items.get(dataset["id"], [])]
        # The engine spells the cap "steam_limit" on the wire; the adapter
        # mirrors the typo deliberately, so the double has to answer to it.
        limit = _as_int(payload.get("steam_limit"), 2000)
        return self._ndjson(
            self._cursor(rows, payload.get("last_retrieved_id"), {"limit": limit})
        )

    def _delete_dataset_items(self, _match: re.Match, _query: dict, body: Any) -> None:
        payload = body or {}
        wanted = {str(item) for item in payload.get("item_ids") or []}
        group = payload.get("batch_group_id")
        for dataset_id, rows in self.dataset_items.items():
            if payload.get("dataset_id") and payload["dataset_id"] != dataset_id:
                continue
            self.dataset_items[dataset_id] = [
                row
                for row in rows
                if not (
                    (wanted and str(row.get("id")) in wanted)
                    or (group and row.get("batch_group_id") == group)
                )
            ]
        return None

    def _delete_dataset(self, match: re.Match, _query: dict, _body: Any) -> None:
        dataset_id = match.group(1)
        self.datasets.pop(dataset_id, None)
        self.dataset_items.pop(dataset_id, None)
        return None

    # -- experiments -------------------------------------------------------

    def _list_experiments(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        rows = [self._experiment_row(row) for row in self.experiments.values()]
        dataset_id = self._query_one(query, "datasetId")
        name = self._query_one(query, "name")
        types = _json_param(self._query_one(query, "types"))
        wanted = _json_param(self._query_one(query, "experiment_ids"))
        if dataset_id:
            rows = [row for row in rows if row.get("dataset_id") == dataset_id]
        if name:
            rows = [row for row in rows if name.lower() in str(row.get("name", "")).lower()]
        if isinstance(types, list) and types:
            rows = [row for row in rows if row.get("type") in types]
        if isinstance(wanted, list) and wanted:
            rows = [row for row in rows if row.get("id") in wanted]
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _experiment_row(self, experiment: Mapping[str, Any]) -> JsonObject:
        items = self.experiment_items.get(experiment["id"], [])
        scores: dict[str, list[float]] = {}
        for item in items:
            for score in item.get("feedback_scores") or []:
                name = score.get("name")
                value = score.get("value")
                if name is None or not isinstance(value, (int, float)):
                    continue
                scores.setdefault(str(name), []).append(float(value))
        return {
            **dict(experiment),
            "trace_count": len(items),
            "feedback_scores": [
                {"name": name, "value": round(sum(values) / len(values), 6)}
                for name, values in sorted(scores.items())
            ],
        }

    def _create_experiment(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = body or {}
        if not payload.get("name"):
            raise EngineFailure(400, {"errors": ["an experiment needs a name"]})
        dataset = self._dataset_by_name(payload.get("dataset_name"))
        experiment: JsonObject = {
            "id": _new_id(),
            "name": payload["name"],
            "dataset_name": payload.get("dataset_name"),
            "dataset_id": dataset["id"] if dataset else None,
            "type": payload.get("type") or "regular",
            "metadata": dict(payload.get("metadata") or {}),
            "prompt_versions": list(payload.get("prompt_versions") or []),
            "optimization_id": payload.get("optimization_id"),
            "created_at": _iso(),
            "last_updated_at": _iso(),
        }
        self.experiments[experiment["id"]] = experiment
        self.experiment_items[experiment["id"]] = []
        return self._created(f"{API_ROOT}/experiments/{experiment['id']}")

    def _get_experiment(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        experiment = self.experiments.get(match.group(1))
        if experiment is None:
            raise EngineFailure(404, {"errors": ["experiment not found"]})
        return self._experiment_row(experiment)

    def _link_experiment_items(self, _match: re.Match, _query: dict, body: Any) -> None:
        """The classic endpoint: link existing traces, one row per case.

        Mirrors the engine's two sharp edges — every id must be a version 7
        UUID, and the linked trace's feedback scores become the item's, which
        is what the experiment aggregation reads.
        """
        payload = body or {}
        rows = payload.get("experiment_items")
        if not isinstance(rows, list) or not rows:
            raise EngineFailure(400, {"errors": ["'experiment_items' must be an array"]})
        for row in rows:
            if not isinstance(row, Mapping):
                raise EngineFailure(400, {"errors": ["each experiment item must be an object"]})
            item_id = str(row.get("id") or "")
            if len(item_id) != 36 or item_id[14] != "7":
                raise EngineFailure(
                    400, {"message": "Experiment Item id must be a version 7 UUID"}
                )
            experiment = self.experiments.get(str(row.get("experiment_id")))
            if experiment is None:
                raise EngineFailure(404, {"errors": ["experiment not found"]})
            trace = self.traces.get(str(row.get("trace_id")))
            stored = {**dict(row)}
            if trace is not None:
                stored["feedback_scores"] = list(trace.get("feedback_scores") or [])
            self.experiment_items.setdefault(experiment["id"], []).append(stored)
            experiment["last_updated_at"] = _iso()
        return None

    def _create_experiment_items(self, _match: re.Match, _query: dict, body: Any) -> None:
        payload = body or {}
        name = payload.get("experiment_name")
        experiment = None
        if payload.get("experiment_id"):
            experiment = self.experiments.get(str(payload["experiment_id"]))
        if experiment is None:
            experiment = next(
                (row for row in self.experiments.values() if row["name"] == name), None
            )
        if experiment is None:
            raise EngineFailure(404, {"errors": ["experiment not found"]})
        items = payload.get("items")
        if not isinstance(items, list):
            raise EngineFailure(400, {"errors": ["'items' must be an array"]})
        stored = self.experiment_items.setdefault(experiment["id"], [])
        for item in items:
            if isinstance(item, Mapping):
                stored.append({**dict(item), "id": item.get("id") or _new_id()})
        experiment["last_updated_at"] = _iso()
        return None

    def _experiment_groups(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        name = self._query_one(query, "name")
        rows = [
            self._experiment_row(row)
            for row in self.experiments.values()
            if not name or name.lower() in str(row.get("name", "")).lower()
        ]
        return {"content": rows, "total": len(rows)}

    def _experiment_group_aggregations(
        self, _match: re.Match, _query: dict, _body: Any
    ) -> JsonObject:
        return {
            "content": [
                {
                    "group": experiment["name"],
                    "experiment_count": 1,
                    "trace_count": len(self.experiment_items.get(experiment["id"], [])),
                }
                for experiment in self.experiments.values()
            ]
        }

    def _delete_experiments(self, _match: re.Match, _query: dict, body: Any) -> None:
        for experiment_id in (body or {}).get("ids") or []:
            self.experiments.pop(str(experiment_id), None)
            self.experiment_items.pop(str(experiment_id), None)
        return None

    # -- guardrails --------------------------------------------------------

    def _list_guardrails(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        project_id = self._query_one(query, "project_id")
        name = self._query_one(query, "name")
        rows = [
            dict(rule)
            for rule in self.guardrail_rules.values()
            if (not project_id or rule.get("project_id") == project_id)
            and (not name or name.lower() in str(rule.get("name", "")).lower())
        ]
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _get_guardrail(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        rule = self.guardrail_rules.get(match.group(1))
        if rule is None:
            raise EngineFailure(404, {"errors": ["evaluator not found"]})
        return dict(rule)

    def _create_guardrail(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        payload = dict(body or {})
        # The real store is a discriminated union keyed by `type`; an unknown
        # type is a deserialization failure, and the python-metric subtype
        # requires runnable code with a non-empty argument mapping.
        evaluator_types = {
            "llm_as_judge",
            "user_defined_metric_python",
            "trace_thread_llm_as_judge",
            "trace_thread_user_defined_metric_python",
            "span_llm_as_judge",
            "span_user_defined_metric_python",
        }
        kind = payload.get("type")
        if kind not in evaluator_types:
            raise EngineFailure(
                400,
                {"errors": [f"Cannot deserialize evaluator type from {kind!r}"]},
            )
        if not str(payload.get("name") or "").strip():
            raise EngineFailure(400, {"errors": ["name must not be blank"]})
        if "metric_python" in str(kind):
            code = payload.get("code")
            metric = (code or {}).get("metric") if isinstance(code, dict) else None
            arguments = (code or {}).get("arguments") if isinstance(code, dict) else None
            if not (isinstance(metric, str) and metric.strip()):
                raise EngineFailure(400, {"errors": ["code.metric must not be blank"]})
            if not (isinstance(arguments, dict) and arguments):
                raise EngineFailure(400, {"errors": ["code.arguments must not be empty"]})
        rule = {**payload, "id": _new_id(), "created_at": _iso()}
        self.guardrail_rules[rule["id"]] = rule
        return self._created(f"{API_ROOT}/automations/evaluators/{rule['id']}")

    def _update_guardrail(self, match: re.Match, _query: dict, body: Any) -> None:
        rule = self.guardrail_rules.get(match.group(1))
        if rule is None:
            raise EngineFailure(404, {"errors": ["evaluator not found"]})
        rule.update(body or {})
        rule["last_updated_at"] = _iso()
        return None

    def _delete_guardrails(self, _match: re.Match, _query: dict, body: Any) -> None:
        for rule_id in (body or {}).get("ids") or []:
            self.guardrail_rules.pop(str(rule_id), None)
        return None

    def _create_guardrail_results(self, _match: re.Match, _query: dict, body: Any) -> None:
        rows = (body or {}).get("guardrails")
        if not isinstance(rows, list):
            raise EngineFailure(400, {"errors": ["'guardrails' must be an array"]})
        self.guardrail_results.extend(dict(row) for row in rows if isinstance(row, Mapping))
        return None

    def _evaluate_guardrails(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        """The inline checker: answers with whatever verdicts a test staged."""
        payload = body or {}
        self.checker_requests.append(dict(payload))
        return {
            "text": payload.get("text"),
            "validations": [dict(row) for row in self.checker_verdicts],
        }

    # -- alerts ------------------------------------------------------------

    def _list_alerts(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        rows = [dict(alert) for alert in self.alerts.values()]
        rows = [
            row
            for row in rows
            if _matches_filters(row, _json_param(self._query_one(query, "filters")))
        ]
        rows = _sorted_rows(rows, _json_param(self._query_one(query, "sorting")))
        return _page(
            rows,
            _as_int(self._query_one(query, "page"), 1),
            _as_int(self._query_one(query, "size"), 50),
        )

    def _get_alert(self, match: re.Match, _query: dict, _body: Any) -> JsonObject:
        alert = self.alerts.get(match.group(1))
        if alert is None:
            raise EngineFailure(404, {"errors": ["alert not found"]})
        return dict(alert)

    def _create_alert(self, _match: re.Match, _query: dict, body: Any) -> tuple:
        alert = {**dict(body or {}), "id": _new_id(), "created_at": _iso()}
        self.alerts[alert["id"]] = alert
        return self._created(f"{API_ROOT}/alerts/{alert['id']}")

    def _update_alert(self, match: re.Match, _query: dict, body: Any) -> None:
        alert = self.alerts.get(match.group(1))
        if alert is None:
            raise EngineFailure(404, {"errors": ["alert not found"]})
        alert.update(body or {})
        return None

    def _delete_alerts(self, _match: re.Match, _query: dict, body: Any) -> None:
        for alert_id in (body or {}).get("ids") or []:
            self.alerts.pop(str(alert_id), None)
        return None

    def _test_webhook(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        self.webhook_tests.append(dict(body or {}))
        return {"status": "delivered", "status_code": 200, "delivered_at": _iso()}

    def _webhook_examples(self, _match: re.Match, query: dict, _body: Any) -> JsonObject:
        alert_type = self._query_one(query, "alert_type") or "trace:score"
        return {"examples": [{"alert_type": alert_type, "payload": {"event": alert_type}}]}

    # -- costs and usage ---------------------------------------------------

    def _spans_for(self, project_ids: Sequence[str] | None) -> list[JsonObject]:
        wanted = set(project_ids or [])
        return [
            span
            for span in self.spans.values()
            if not wanted or span.get("project_id") in wanted
        ]

    def _cost_summary(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        payload = body or {}
        start = _parse_instant(payload.get("interval_start"))
        end = _parse_instant(payload.get("interval_end"))

        def spend(since: Any, until: Any) -> float:
            total = 0.0
            for span in self._spans_for(payload.get("project_ids")):
                if not _within(span, "start_time", since, until):
                    continue
                cost = span.get("total_estimated_cost")
                if isinstance(cost, (int, float)):
                    total += float(cost)
            return round(total, 6)

        # The real engine answers with the window and the window immediately
        # before it: {"name": "cost", "current": X, "previous": Y}.
        previous = 0.0
        if start is not None and end is not None:
            previous = spend(start - (end - start), start)
        return {"name": "cost", "current": spend(start, end), "previous": previous}

    def _cost_series(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        payload = body or {}
        buckets: dict[str, float] = {}
        for span in self._spans_for(payload.get("project_ids")):
            if not _within(
                span, "start_time", payload.get("interval_start"), payload.get("interval_end")
            ):
                continue
            moment = _parse_instant(span.get("start_time"))
            if moment is None:
                continue
            day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            cost = span.get("total_estimated_cost")
            if isinstance(cost, (int, float)):
                buckets[_iso(day)] = buckets.get(_iso(day), 0.0) + float(cost)
        return {
            "results": [
                {
                    "name": "cost",
                    "data": [
                        {"time": moment, "value": round(value, 6)}
                        for moment, value in sorted(buckets.items())
                    ],
                }
            ]
        }

    def _workspace_usage(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        payload = body or {}
        metric = str(payload.get("metric_type") or "")
        # The real endpoint serves exactly one metric; anything else is a 400,
        # never an empty result.
        if metric != "SPAN_TOKEN_USAGE":
            raise EngineFailure(
                400, {"errors": [f"metric type {metric or '(none)'} is not supported"]}
            )
        buckets: dict[str, dict[str, float]] = {}
        for span in self._spans_for(payload.get("project_ids")):
            if not _within(
                span, "start_time", payload.get("interval_start"), payload.get("interval_end")
            ):
                continue
            moment = _parse_instant(span.get("start_time"))
            if moment is None:
                continue
            day = _iso(moment.replace(hour=0, minute=0, second=0, microsecond=0))
            usage = span.get("usage")
            if not isinstance(usage, Mapping):
                continue
            slot = buckets.setdefault(
                day, {"total_tokens": 0.0, "prompt_tokens": 0.0, "completion_tokens": 0.0}
            )
            for key in slot:
                slot[key] += float(usage.get(key) or 0)
        # Total, prompt and completion arrive as separate lines, exactly as the
        # real engine reports them — a reader that sums every line double-counts.
        return {
            "results": [
                {
                    "name": name,
                    "data": [
                        {"time": moment, "value": values[name]}
                        for moment, values in sorted(buckets.items())
                    ],
                }
                for name in ("total_tokens", "prompt_tokens", "completion_tokens")
            ]
        }

    def _token_usage_names(self, _match: re.Match, _query: dict, body: Any) -> JsonObject:
        wanted = set((body or {}).get("project_ids") or [])
        names: set[str] = set()
        for span in self.spans.values():
            if wanted and span.get("project_id") not in wanted:
                continue
            usage = span.get("usage")
            if isinstance(usage, Mapping):
                names.update(str(key) for key in usage)
        return {"names": sorted(names)}
