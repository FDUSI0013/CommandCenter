"""What the other owners of the audit pass asked of this SDK.

Two things, each written down by somebody looking at this package from outside:

* the TypeScript SDK reads the timeout under both SDKs' environment names, and a
  fleet that runs agents in both languages sets one of them for everybody;
* ``POST /agents/{id}/run`` hands the caller a ``run_id`` and expects the runtime
  to report the run *under that id* — which needs a trace that can be given one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

import fulcrum_ops
from fulcrum_ops import FulcrumOps, new_id, trace
from fulcrum_ops.options import resolve_options

from .conftest import build_client, sent
from .stub_server import StubServer

# ------------------------------------------------------------------ the timeout


def test_the_timeout_is_read_under_the_typescript_sdks_name_too() -> None:
    os.environ["FULCRUM_OPS_TIMEOUT_MS"] = "2500"
    assert resolve_options().timeout_seconds == 2.5


def test_this_sdks_own_timeout_name_wins_when_both_are_set() -> None:
    os.environ["FULCRUM_OPS_TIMEOUT_SECONDS"] = "5"
    os.environ["FULCRUM_OPS_TIMEOUT_MS"] = "1500"
    assert resolve_options().timeout_seconds == 5.0


def test_an_unreadable_seconds_value_falls_through_to_the_milliseconds_one() -> None:
    os.environ["FULCRUM_OPS_TIMEOUT_SECONDS"] = "soon"
    os.environ["FULCRUM_OPS_TIMEOUT_MS"] = "1500"
    assert resolve_options().timeout_seconds == 1.5

    os.environ["FULCRUM_OPS_TIMEOUT_MS"] = "later"
    assert resolve_options().timeout_seconds == 30.0


def test_the_argument_still_beats_either_environment_name() -> None:
    os.environ["FULCRUM_OPS_TIMEOUT_MS"] = "1500"
    assert resolve_options(timeout_seconds=7).timeout_seconds == 7.0
    # The floor applies whichever name the number came in under.
    os.environ["FULCRUM_OPS_TIMEOUT_MS"] = "1"
    assert resolve_options().timeout_seconds == 0.1


# ------------------------------------------------------- a run somebody else named


def test_a_trace_can_be_reported_under_an_id_the_console_issued(
    client: FulcrumOps, stub: StubServer
) -> None:
    run_id = new_id()  # what ``POST /agents/{id}/run`` answers with

    with client.trace("handle", id=run_id, thread_id="ses-1") as run:
        assert run.id == run_id
        with client.span("step", type="tool") as step:
            assert step.trace_id == run_id
        late = run.span("reply", type="llm")
    late.end()  # after the run closed: goes out alone, and has to say whose it is
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert row["id"] == run_id
    assert row["thread_id"] == "ses-1"
    assert [span["name"] for span in row["spans"]] == ["step"]
    (streamed,) = sent(stub, "spans")
    assert streamed["trace_id"] == run_id


def test_an_id_the_store_would_refuse_is_replaced_and_said_out_loud(
    client: FulcrumOps, stub: StubServer, caplog: pytest.LogCaptureFixture
) -> None:
    """One version 4 UUID on a trace costs every trace in the same request."""
    refused = str(uuid.uuid4())

    with caplog.at_level("WARNING", logger="fulcrum_ops"):
        for bad in (refused, "run-17", ""):
            with client.trace("handle", id=bad) as run:
                assert uuid.UUID(run.id).version == 7
    client.flush(timeout=5)

    ids = [row["id"] for row in sent(stub, "traces")]
    assert len(set(ids)) == 3 and refused not in ids
    assert all(uuid.UUID(value).version == 7 for value in ids)
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(messages) == 3 and refused in messages[0]


def test_an_adopted_id_is_filed_under_its_canonical_spelling(
    client: FulcrumOps, stub: StubServer
) -> None:
    run_id = new_id()

    with client.trace("handle", id=f"  {run_id.upper()}  ") as run:
        assert run.id == run_id
    with client.trace("handle", id=uuid.UUID(run_id)) as again:  # type: ignore[arg-type]
        assert again.id == run_id
    client.flush(timeout=5)

    assert [row["id"] for row in sent(stub, "traces")] == [run_id, run_id]


def test_no_id_still_mints_one(client: FulcrumOps, stub: StubServer) -> None:
    with client.trace("handle") as first, client.trace("handle", id=None) as second:
        assert first.id != second.id
    client.flush(timeout=5)
    assert all(uuid.UUID(row["id"]).version == 7 for row in sent(stub, "traces"))


def test_the_decorator_works_the_run_id_out_from_each_calls_own_arguments(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(name="step", type="tool", trace_id=lambda job: "not-read-here", client=client)
    def step(job: dict) -> str:
        return "done"

    @trace(
        name="handle",
        trace_id=lambda job: job["run_id"],
        thread_id=lambda job: job["session_id"],
        client=client,
    )
    def handle(job: dict) -> str:
        return step(job)

    first, second = new_id(), new_id()
    assert handle({"run_id": first, "session_id": "ses-1"}) == "done"
    assert handle({"run_id": second, "session_id": "ses-2"}) == "done"
    client.flush(timeout=5)

    rows = sent(stub, "traces")
    assert [(row["id"], row["thread_id"]) for row in rows] == [(first, "ses-1"), (second, "ses-2")]
    # The nested call is a step of the run that was open, not a run of its own.
    assert [[span["name"] for span in row["spans"]] for row in rows] == [["step"], ["step"]]


def test_a_typed_root_adopts_the_id_for_the_run_and_its_one_step(
    client: FulcrumOps, stub: StubServer
) -> None:
    run_id = new_id()

    @trace(name="answer", type="llm", trace_id=run_id, client=client)
    def answer() -> str:
        assert fulcrum_ops.current_trace().id == run_id
        assert fulcrum_ops.current_span().trace_id == run_id
        return "refer"

    answer()
    client.flush(timeout=5)
    (row,) = sent(stub, "traces")
    assert row["id"] == run_id and row["spans"][0]["type"] == "llm"


def test_a_trace_id_callable_that_raises_costs_the_id_not_the_call(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(trace_id=lambda job: job["run_id"], client=client)
    def handle(job: dict) -> str:
        return "handled"

    assert handle({}) == "handled"  # KeyError inside the lambda; the agent never sees it
    client.flush(timeout=5)
    (row,) = sent(stub, "traces")
    assert uuid.UUID(row["id"]).version == 7


def test_coroutines_and_generators_adopt_the_id_too(client: FulcrumOps, stub: StubServer) -> None:
    @trace(trace_id=lambda run_id: run_id, client=client)
    async def answer(run_id: str) -> str:
        return "ok"

    @trace(trace_id=lambda run_id: run_id, client=client)
    def stream(run_id: str) -> Iterator[str]:
        yield "a"

    first, second = new_id(), new_id()
    asyncio.run(answer(first))
    list(stream(second))
    client.flush(timeout=5)
    assert sorted(row["id"] for row in sent(stub, "traces")) == sorted([first, second])


def test_a_run_the_console_is_waiting_on_can_be_kept_out_of_the_sampling_draw(
    stub: StubServer, shared_http_client: Any
) -> None:
    instance = build_client(stub, http_client=shared_http_client, sampling_rate=0.0)
    try:
        dropped, kept = new_id(), new_id()
        with instance.trace("handle", id=dropped):
            pass
        with instance.trace("handle", id=kept, sampled=True):
            pass
        instance.flush(timeout=5)
        assert [row["id"] for row in sent(stub, "traces")] == [kept]
    finally:
        instance.close(timeout=2.0)

    always = build_client(stub, http_client=shared_http_client)
    try:
        with always.trace("handle", sampled=False):
            pass
        always.flush(timeout=5)
        assert [row["id"] for row in sent(stub, "traces")] == [kept]
    finally:
        always.close(timeout=2.0)
