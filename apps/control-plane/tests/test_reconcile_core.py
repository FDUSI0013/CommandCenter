"""Regression tests for the hand-offs the 2026-09-18 fix pass addressed to core.

Each of these is something another screen's fix needed from the adapter, the
settings or the model runner and could not change itself: a scan that wants its
own deadline to reach the wire, an ingest hand-off that wants the same, a
stream page too large to parse on the event loop, a prompt run against a model
that thinks before it answers.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading

import httpx
import pytest

from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.engine import EngineBadRequest, EngineClient, EngineUnavailable
from fulcrum_ops_api.services import model_runner

ENGINE_URL = "http://telemetry-engine.internal"


def _adapter(handler, **kwargs) -> EngineClient:
    """A real adapter over httpx's own in-memory transport.

    What is under test is what the adapter puts on the wire -- which timeout,
    how many attempts -- and the engine double answers too politely to show it.
    """
    return EngineClient(
        base_url=ENGINE_URL, retries=2, transport=httpx.MockTransport(handler), **kwargs
    )


# ---------------------------------------------------------------------------
# #13 -- a scan with a deadline of its own can hand that deadline to the wire
# ---------------------------------------------------------------------------


async def test_a_span_page_and_a_stats_read_take_the_callers_deadline_and_retries() -> None:
    timeouts: dict[str, float | None] = {}
    attempts: list[str] = []

    def warming_up(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        timeouts[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(503, json={"errors": ["warming up"]})

    client = _adapter(warming_up, timeout_seconds=30.0, read_timeout_seconds=7.0)
    try:
        with pytest.raises(EngineUnavailable):
            await client.list_spans(project_id="p-1", retries=0, timeout_seconds=20.0)
        with pytest.raises(EngineUnavailable):
            await client.get_project_stats(retries=0, timeout_seconds=20.0)
    finally:
        await client.aclose()

    assert timeouts == {"/v1/private/spans": 20.0, "/v1/private/projects/stats": 20.0}
    assert attempts == ["/v1/private/spans", "/v1/private/projects/stats"], (
        "retries=0 was ignored: a scan racing its own deadline was re-dialled "
        f"with backoff ({len(attempts)} attempts for two reads)"
    )

    # A caller that says nothing keeps what it had: the trace drawer's span list
    # still runs on the client-wide timeout and is still re-dialled.
    attempts.clear()
    client = _adapter(warming_up, timeout_seconds=30.0, read_timeout_seconds=7.0)
    try:
        with pytest.raises(EngineUnavailable):
            await client.list_spans(trace_id="t-1")
    finally:
        await client.aclose()
    assert timeouts["/v1/private/spans"] == 30.0
    assert len(attempts) == 3


# ---------------------------------------------------------------------------
# #56 -- ingest's hand-off can put its own leash on every batch write
# ---------------------------------------------------------------------------


async def test_every_batch_write_takes_ingests_timeout_and_is_sent_once() -> None:
    seen: list[tuple[str, str, float | None]] = []

    def behind(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.extensions["timeout"]["read"]))
        return httpx.Response(503, json={"errors": ["merges are behind"]})

    client = _adapter(behind, timeout_seconds=30.0)
    writes = (
        client.create_traces_batch,
        client.create_spans_batch,
        client.score_traces_batch,
        client.score_spans_batch,
        client.score_threads_batch,
    )
    try:
        for write in writes:
            with pytest.raises(EngineUnavailable):
                await write([{"id": "row-1"}], timeout_seconds=10.0, retries=0)
    finally:
        await client.aclose()

    assert seen == [
        ("POST", "/v1/private/traces/batch", 10.0),
        ("POST", "/v1/private/spans/batch", 10.0),
        ("PUT", "/v1/private/traces/feedback-scores", 10.0),
        ("PUT", "/v1/private/spans/feedback-scores", 10.0),
        ("PUT", "/v1/private/traces/threads/feedback-scores", 10.0),
    ], "the SDK is ingest's retry layer: one attempt per write, on ingest's own timeout"

    # Every other caller (a reviewer's score, the feedback mirror) is not retried
    # from outside, so a score write it leaves unbounded is still re-sent here.
    seen.clear()
    client = _adapter(behind, timeout_seconds=30.0)
    try:
        with pytest.raises(EngineUnavailable):
            await client.score_traces_batch([{"id": "t-1", "name": "helpful", "value": 1}])
    finally:
        await client.aclose()
    assert [read for _, _, read in seen] == [30.0, 30.0, 30.0]


# ---------------------------------------------------------------------------
# #26 -- a multi-megabyte stream page is not parsed on the event loop
# ---------------------------------------------------------------------------


async def test_a_large_stream_page_is_parsed_off_the_event_loop(monkeypatch) -> None:
    from fulcrum_ops_api.engine import client as adapter

    loop_thread = threading.get_ident()
    parsed_on: list[int] = []
    decode = adapter._decode_lines

    def watched(response: httpx.Response) -> list[dict]:
        parsed_on.append(threading.get_ident())
        return decode(response)

    monkeypatch.setattr(adapter, "_decode_lines", watched)

    # What the knowledge scan reads: untruncated spans, a prompt and a
    # completion each, a few hundred to the page.
    big = [{"id": f"s-{n}", "input": {"prompt": "x" * 4096}} for n in range(200)]
    small = big[:3]
    torn = False

    def store(request: httpx.Request) -> httpx.Response:
        rows = big if json.loads(request.content)["limit"] == 500 else small
        body = "\n".join(json.dumps(row) for row in rows)
        if torn:
            body += "\n{not json"
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/octet-stream"}
        )

    client = _adapter(store)
    try:
        page = await client.search_spans(project_id="p-1", limit=500, truncate=False)
        assert [row["id"] for row in page] == [row["id"] for row in big]
        assert parsed_on and parsed_on[-1] != loop_thread, (
            "a page of untruncated spans was parsed on the event loop: every other "
            "request on the worker waits for the last row"
        )

        few = await client.search_spans(project_id="p-1", limit=3)
        assert [row["id"] for row in few] == ["s-0", "s-1", "s-2"]
        assert parsed_on[-1] == loop_thread, "a small page is cheaper parsed where it is"

        # A row the thread cannot parse is still the adapter's typed refusal.
        torn = True
        with pytest.raises(EngineBadRequest):
            await client.search_spans(project_id="p-1", limit=500, truncate=False)
        assert parsed_on[-1] != loop_thread
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# #114 -- a prompt run against a model that thinks before it answers
# ---------------------------------------------------------------------------


def _model_provider(monkeypatch, sent: list[dict]) -> None:
    """Point the model runner at an in-memory provider and record what it is sent."""

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(
            {"body": json.loads(request.content), "read": request.extensions["timeout"]["read"]}
        )
        return httpx.Response(
            200,
            json={
                "model": "reasoner-1",
                "choices": [{"message": {"content": "42"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 700, "total_tokens": 709},
            },
        )

    class Wired(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(provider), **kwargs)

    monkeypatch.setattr(model_runner.httpx, "AsyncClient", Wired)
    monkeypatch.setattr(settings, "prompt_studio_endpoint", "https://models.internal/v1")
    monkeypatch.setattr(settings, "prompt_studio_api_key", "test-key")


async def test_reasoning_effort_is_sent_only_when_the_operator_set_it(monkeypatch) -> None:
    sent: list[dict] = []
    _model_provider(monkeypatch, sent)

    for configured in (None, "", "   "):
        monkeypatch.setattr(settings, "prompt_studio_reasoning_effort", configured)
        await model_runner.execute("What is six times seven?")
    assert all("reasoning_effort" not in call["body"] for call in sent), (
        "a model that does not reason refuses a request that carries reasoning_effort; "
        "an unset (or unfilled compose) setting must send nothing"
    )

    monkeypatch.setattr(settings, "prompt_studio_reasoning_effort", " low ")
    run = await model_runner.execute("What is six times seven?")
    assert sent[-1]["body"]["reasoning_effort"] == "low"
    assert run.output == "42"


async def test_a_run_gets_the_time_and_the_tokens_a_reasoning_model_needs(monkeypatch) -> None:
    sent: list[dict] = []
    _model_provider(monkeypatch, sent)
    defaults = type(settings).model_fields
    monkeypatch.setattr(
        settings, "prompt_studio_timeout_seconds", defaults["prompt_studio_timeout_seconds"].default
    )
    monkeypatch.setattr(
        settings,
        "prompt_studio_max_output_tokens",
        defaults["prompt_studio_max_output_tokens"].default,
    )

    await model_runner.execute("What is six times seven?")
    assert sent[-1]["read"] == 120.0
    assert sent[-1]["body"]["max_completion_tokens"] == 4096
    # The request may still ask for less than the ceiling.
    await model_runner.execute("What is six times seven?", max_output_tokens=256)
    assert sent[-1]["body"]["max_completion_tokens"] == 256


def test_the_server_gives_up_on_a_model_before_the_console_gives_up_on_the_server() -> None:
    """The order of the three waits is the fix; this pins the two this repo's code sets."""
    console = pathlib.Path(__file__).resolve().parents[3] / "apps" / "web" / "js" / "api.js"
    if not console.exists():
        pytest.skip("the console is not part of this checkout")
    found = re.search(r"PROMPT_RUN_TIMEOUT_MS\s*=\s*(\d+)", console.read_text(encoding="utf-8"))
    assert found, "the console no longer names its prompt-run timeout"
    server = type(settings).model_fields["prompt_studio_timeout_seconds"].default
    assert server >= 120.0
    assert server + 10 <= int(found.group(1)) / 1000, (
        "the console must outwait the model call and the engine reads before it: a run is "
        "billed and audited whether or not anyone is still listening"
    )
