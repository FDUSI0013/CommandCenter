"""Work fanned out to a thread pool has to stay inside the run that started it.

The parent link lives in two context variables. A task inherits a copy of them
and so does ``asyncio.to_thread``; a ``ThreadPoolExecutor`` worker and
``loop.run_in_executor`` do not. An agent that extracted its attachments in
parallel therefore reported one empty run for itself and one more run per
attachment, each named after the helper: run counts, request quotas and the
Live Runs list were all inflated, and the real run showed no steps.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List

import fulcrum_ops
from fulcrum_ops import FulcrumOps, trace

from .conftest import sent
from .stub_server import StubServer


def test_a_fan_out_through_the_traced_executor_is_one_run(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(type="tool", client=client)
    def extract(attachment: str) -> int:
        return len(attachment)

    @trace(name="underwriting_insight", client=client)
    def insight(attachments: List[str]) -> int:
        with fulcrum_ops.TracedThreadPoolExecutor(max_workers=2) as pool:
            return sum(pool.map(extract, attachments))

    assert insight(["a.docx", "b.pdf", "c.xlsx"]) == 17
    assert client.flush(timeout=5) is True

    (row,) = sent(stub, "traces")
    assert row["name"] == "underwriting_insight"
    assert sorted(step["input"]["attachment"] for step in row["spans"]) == [
        "a.docx",
        "b.pdf",
        "c.xlsx",
    ]
    assert {step["type"] for step in row["spans"]} == {"tool"}


def test_propagate_carries_the_run_into_a_plain_pool_and_nests_under_the_open_step(
    client: FulcrumOps, stub: StubServer
) -> None:
    @trace(type="tool", client=client)
    def extract(attachment: str) -> str:
        return attachment.upper()

    with client.trace("underwriting_insight"):
        with client.span("attachments") as parent:
            with ThreadPoolExecutor(max_workers=2) as pool:
                assert list(pool.map(fulcrum_ops.propagate(extract), ["a", "b"])) == ["A", "B"]
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    children = [step for step in row["spans"] if step["name"].endswith("extract")]
    assert len(children) == 2
    assert {step["parent_span_id"] for step in children} == {parent.id}


def test_a_pool_thread_does_not_keep_the_last_jobs_run(client: FulcrumOps) -> None:
    """Pool threads are reused: what one job was inside must not greet the next one."""
    seen = []

    def look() -> None:
        seen.append((threading.current_thread().name, client.current_trace()))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with client.trace("first") as run:
            pool.submit(fulcrum_ops.propagate(look)).result()
        pool.submit(look).result()

    (first_thread, first), (second_thread, second) = seen
    assert first_thread == second_thread
    assert first is run
    assert second is None


def test_run_in_executor_keeps_the_run(client: FulcrumOps, stub: StubServer) -> None:
    @trace(type="tool", client=client)
    def poll_once(mailbox: str) -> int:
        return 3

    @trace(name="poller", client=client)
    async def poller() -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fulcrum_ops.propagate(poll_once), "inbox")

    assert asyncio.run(poller()) == 3
    client.flush(timeout=5)

    (row,) = sent(stub, "traces")
    assert [step["name"].rsplit(".", 1)[-1] for step in row["spans"]] == ["poll_once"]


def test_the_wrapped_function_is_still_the_callers_function() -> None:
    def extract(attachment: str, *, pages: int = 1) -> str:
        """Pull the text out."""
        return "{0}:{1}".format(attachment, pages)

    bound = fulcrum_ops.propagate(extract)
    assert bound.__name__ == "extract" and bound.__doc__ == "Pull the text out."
    assert bound("a.pdf", pages=2) == "a.pdf:2"
