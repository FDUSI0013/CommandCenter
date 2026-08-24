"""A stand-in for the control plane, on a real socket.

The tests drive the SDK over HTTP rather than against a mocked transport,
because most of what is worth testing here *is* the HTTP behaviour: status
handling, the retry loop, ``Retry-After``, ETag revalidation, the per-item
result rows. A stubbed transport would let every one of those pass while broken.

The stub implements the ingest and prompt contracts faithfully enough to catch a
shape error, and adds failure injection so a test can ask for "503 twice, then
200" without waiting for a real outage.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_CONFIG: Dict[str, Any] = {
    "workspace": "acme",
    "environment": "Production",
    "agent_id": "agent-1",
    "agent_name": "support-copilot",
    "agent_bound": False,
    "sampling_rate": 1.0,
    "batch_max_spans": 500,
    "batch_max_bytes": 2_000_000,
    "flush_interval_seconds": 2.0,
    "max_queue_size": 5_000,
    "retry_max_attempts": 3,
    "retry_backoff_seconds": 0.5,
    "capture_input": True,
    "capture_output": True,
    "endpoints": {
        "traces": "/api/v1/ingest/traces",
        "spans": "/api/v1/ingest/spans",
        "scores": "/api/v1/ingest/scores",
        "events": "/api/v1/ingest/events",
        "config": "/api/v1/ingest/config",
        "otlp_traces": "/v1/traces",
    },
    "guardrails": [],
    "redaction": [],
    "revision": "rev-1",
    "refresh_after_seconds": 300,
}

DEFAULT_PROMPT: Dict[str, Any] = {
    "id": "prompt-1",
    "name": "support-system",
    "status": "Approved",
    "version": "v3",
    "commit": "c0ffee",
    "template": "You are helping {{customer_name}} with {{topic}}.",
    "variables": ["customer_name", "topic"],
}


class RecordedRequest:
    """One request the stub saw, kept for assertions."""

    __slots__ = ("method", "path", "query", "headers", "body")

    def __init__(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        headers: Dict[str, str],
        body: Any,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "RecordedRequest({0} {1})".format(self.method, self.path)


class Injected:
    """A canned response to serve instead of the normal handler."""

    __slots__ = ("status", "body", "headers", "times")

    def __init__(
        self,
        status: int,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        times: int = 1,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.times = times


class StubServer:
    """The control plane, as much of it as the SDK actually talks to."""

    def __init__(self) -> None:
        self.requests: List[RecordedRequest] = []
        self.injected: Dict[str, List[Injected]] = {}
        self.config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.config_etag = 'W/"rev-1"'
        self.prompts: Dict[str, Dict[str, Any]] = {"prompt-1": dict(DEFAULT_PROMPT)}
        self.prompt_versions: Dict[str, Dict[str, Any]] = {
            "prompt-1/deadbee": {
                "commit": "deadbee",
                "version": "v2",
                "status": "Approved",
                "template": "Older text for {{customer_name}}.",
                "variables": ["customer_name"],
            }
        }
        self.datasets: Dict[str, List[Dict[str, Any]]] = {}
        #: Per-item outcomes to report for the next ingest batch, by kind.
        self.item_outcomes: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        #: Live client sockets, so ``stop()`` can genuinely break them. Closing
        #: only the listening socket is not an outage: an SDK holding a
        #: keep-alive connection would keep being served by the handler thread
        #: that already owns it, and a test meaning to simulate a dead control
        #: plane would quietly pass against a live one.
        self._connections: set = set()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "StubServer":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # noqa: A003 - silence the stub
                pass

            def setup(self) -> None:
                super().setup()
                stub.register_connection(self.connection)

            def finish(self) -> None:
                stub.forget_connection(self.connection)
                super().finish()

            def _read_body(self) -> Any:
                length = int(self.headers.get("content-length") or 0)
                if not length:
                    return None
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return raw.decode("utf-8", "replace")

            def _respond(
                self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None
            ) -> None:
                body = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._read_body()
                record = RecordedRequest(
                    method,
                    path,
                    parse_qs(parsed.query),
                    {k.lower(): v for k, v in self.headers.items()},
                    body,
                )
                status, payload, headers = stub.handle(record)
                self._respond(status, payload, headers)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._handle("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        # ``ThreadingMixIn.server_close`` joins every handler thread unless this
        # is off, and a keep-alive handler sits blocked on a socket read until
        # its client hangs up. With the SDK holding connections open across
        # tests that join costs half a second per stub — the threads are daemons
        # and the process is going to outlive them either way.
        self._server.block_on_close = False
        # ``serve_forever`` polls its shutdown flag every ``poll_interval``, so
        # the default of 0.5s is what every ``stop()`` would wait for.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def register_connection(self, connection: Any) -> None:
        with self._lock:
            self._connections.add(connection)

    def forget_connection(self, connection: Any) -> None:
        with self._lock:
            self._connections.discard(connection)

    def stop(self) -> None:
        """Stop listening *and* break every connection already established."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    @property
    def port(self) -> int:
        assert self._server is not None, "the stub server is not running"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:{0}/api/v1".format(self.port)

    def __enter__(self) -> "StubServer":
        return self.start()

    def __exit__(self, *args: Any) -> bool:
        self.stop()
        return False

    # ------------------------------------------------------------- injection

    def inject(
        self,
        method: str,
        path: str,
        status: int,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        times: int = 1,
    ) -> None:
        """Serve ``status`` for the next ``times`` requests to ``METHOD /path``."""
        key = "{0} {1}".format(method.upper(), path)
        with self._lock:
            self.injected.setdefault(key, []).append(Injected(status, body, headers, times))

    # --------------------------------------------------------------- routing

    def handle(self, request: RecordedRequest) -> Any:
        with self._lock:
            self.requests.append(request)
            key = "{0} {1}".format(request.method, request.path)
            queue = self.injected.get(key)
            if queue:
                canned = queue[0]
                canned.times -= 1
                if canned.times <= 0:
                    queue.pop(0)
                return canned.status, canned.body, canned.headers

        path = request.path
        if path == "/api/v1/ingest/config":
            return self._config(request)
        if path.startswith("/api/v1/ingest/"):
            return self._ingest(request, path.rsplit("/", 1)[-1])
        if path.startswith("/api/v1/prompts"):
            return self._prompts(request)
        if path.startswith("/api/v1/evaluations"):
            return self._evaluations(request)
        return 404, {"error": {"code": "not_found", "message": "No route for {0}".format(path)}}, {}

    def _config(self, request: RecordedRequest) -> Any:
        if request.headers.get("if-none-match") == self.config_etag:
            return 304, None, {"etag": self.config_etag}
        return 200, self.config, {"etag": self.config_etag, "cache-control": "private, max-age=300"}

    def _ingest(self, request: RecordedRequest, kind: str) -> Any:
        body = request.body if isinstance(request.body, dict) else {}
        items = body.get(kind) or []
        outcomes = self.item_outcomes.get(kind)

        results = []
        accepted = rejected = blocked = 0
        spans_accepted = 0
        for index, item in enumerate(items):
            planned = outcomes[index] if outcomes and index < len(outcomes) else None
            outcome = (planned or {}).get("outcome", "accepted")
            row: Dict[str, Any] = {"index": index, "outcome": outcome}
            if outcome == "accepted":
                accepted += 1
                row["id"] = (item or {}).get("id") or "srv-{0}".format(index)
                nested = len((item or {}).get("spans") or [])
                row["spans"] = nested
                spans_accepted += nested
            elif outcome == "blocked":
                blocked += 1
                row["code"] = (planned or {}).get("code", "policy_blocked")
                row["reason"] = (planned or {}).get("reason", "Refused by policy")
            else:
                rejected += 1
                row["code"] = (planned or {}).get("code", "malformed")
                row["reason"] = (planned or {}).get("reason", "Not a valid row")
            results.append(row)

        return (
            200,
            {
                "received": len(items),
                "accepted": accepted,
                "rejected": rejected,
                "blocked": blocked,
                "spans_accepted": spans_accepted,
                "scores_accepted": accepted if kind == "scores" else 0,
                "events_recorded": accepted if kind == "events" else 0,
                "guardrails_evaluated": False,
                "agents": ["agent-1"],
                "auto_registered": [],
                "quotas": [],
                "results": results,
                "duration_ms": 1,
            },
            {},
        )

    def _prompts(self, request: RecordedRequest) -> Any:
        path = request.path[len("/api/v1/prompts") :].strip("/")
        if not path:
            query = (request.query.get("q") or [""])[0].strip().lower()
            items = [
                prompt
                for prompt in self.prompts.values()
                if not query or query in str(prompt.get("name", "")).lower()
            ]
            return 200, {"items": items, "total": len(items), "page": 1, "pages": 1}, {}

        parts = path.split("/")
        prompt = self.prompts.get(parts[0])
        if prompt is None:
            return 404, {"error": {"code": "not_found", "message": "No such prompt"}}, {}
        if len(parts) >= 3 and parts[1] == "versions":
            version = self.prompt_versions.get("{0}/{1}".format(parts[0], parts[2]))
            if version is None:
                return 404, {"error": {"code": "not_found", "message": "No such commit"}}, {}
            return 200, version, {}
        return 200, prompt, {}

    def _evaluations(self, request: RecordedRequest) -> Any:
        path = request.path[len("/api/v1/evaluations") :].strip("/")

        if path.startswith("datasets"):
            parts = path.split("/")
            if len(parts) == 1:
                if request.method == "POST":
                    name = (request.body or {}).get("name", "unnamed")
                    self.datasets.setdefault(name, [])
                    return 200, {"name": name, "case_count": 0}, {}
                items = [
                    {"name": name, "case_count": len(cases)}
                    for name, cases in self.datasets.items()
                ]
                return 200, {"items": items, "total": len(items), "page": 1, "pages": 1}, {}

            dataset = parts[1]
            if len(parts) >= 3 and parts[2] == "items":
                if request.method == "POST":
                    incoming = (request.body or {}).get("items") or []
                    store = self.datasets.setdefault(dataset, [])
                    for item in incoming:
                        store.append(dict(item, id="case-{0}".format(len(store) + 1)))
                    return 200, {"added": len(incoming)}, {}
                cases = self.datasets.get(dataset, [])
                return 200, {"items": cases, "total": len(cases), "page": 1, "pages": 1}, {}
            return 404, {"error": {"code": "not_found", "message": "No such dataset route"}}, {}

        if request.method == "POST" and not path:
            return 200, {"id": "eval-1", "status": "Running", **(request.body or {})}, {}
        if path:
            return 200, {"id": path, "status": "Completed", "score": 0.91}, {}
        return 200, {"items": [], "total": 0, "page": 1, "pages": 1}, {}
