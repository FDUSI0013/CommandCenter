"""An outage longer than a few seconds must not cost the runs made during it.

The in-request retries add up to about three and a half seconds. A control-plane
deploy takes ten to twenty, and a proxy answering 502 takes as long as it takes.
A batch that ran out of retries was dropped on the spot, ``flush()`` answered
``True`` because the queue was then empty, and the ten-thousand-row queue the
README calls the outage buffer only ever buffered while the worker was asleep
inside that retry.
"""

from __future__ import annotations

import time

import pytest

from fulcrum_ops import FulcrumOps
from fulcrum_ops import queue as queue_module

from .conftest import build_client, sent
from .stub_server import StubServer

TRACES = "/api/v1/ingest/traces"


def test_a_batch_that_outlives_its_retries_is_sent_when_the_control_plane_returns(
    stub: StubServer, shared_http_client
) -> None:
    stub.inject("POST", TRACES, 503, {"error": {"code": "unavailable"}}, times=2)
    client = build_client(stub, shared_http_client, retry_max_attempts=1)
    try:
        with client.trace("during-the-deploy"):
            pass

        assert client.flush(timeout=5) is False, "the run was not delivered; flush must not say it was"
        stats = client.stats()
        assert stats["dropped_failed"] == 0
        assert stats["requeued"] == 1 and stats["pending"] == 1

        # The deploy finishes. Nothing new is traced; the run from before arrives.
        assert client.flush(timeout=5) is True
        assert [row["name"] for row in sent(stub, "traces")][-1] == "during-the-deploy"
        stats = client.stats()
        assert stats["accepted"] == 1 and stats["pending"] == 0 and stats["dropped_failed"] == 0
    finally:
        client.close(timeout=2)


def test_the_worker_backs_off_instead_of_hammering_a_control_plane_that_is_down(
    stub: StubServer, shared_http_client
) -> None:
    """Even with a full batch waiting, which is otherwise a reason to send at once."""
    stub.inject("POST", TRACES, 503, {"error": {"code": "unavailable"}}, times=1_000)
    client = build_client(
        stub, shared_http_client, retry_max_attempts=0, batch_max_items=1, flush_interval_seconds=30.0
    )
    try:
        with client.trace("first"):
            pass
        assert client.flush(timeout=5) is False
        attempts = len(stub.requests)
        with client.trace("second"):  # a full batch: wakes the worker
            pass
        time.sleep(0.3)
        assert len(stub.requests) == attempts, "the back-off must outlast a wake-up"
        assert client.stats()["pending"] == 2
    finally:
        client.close(timeout=2)


def test_a_refusal_is_still_a_drop_and_flush_says_so(stub: StubServer, shared_http_client) -> None:
    """Retrying cannot change a revoked key, so that batch is not kept."""
    stub.inject("POST", TRACES, 401, {"error": {"code": "unauthenticated"}}, times=1)
    client = build_client(stub, shared_http_client)
    try:
        with client.trace("bad-key"):
            pass
        assert client.flush(timeout=5) is False
        stats = client.stats()
        assert stats["dropped_failed"] == 1 and stats["pending"] == 0 and stats["requeued"] == 0
    finally:
        client.close(timeout=2)


def test_an_outage_does_not_stop_the_other_kinds_being_kept(
    stub: StubServer, shared_http_client
) -> None:
    stub.inject("POST", TRACES, 503, {"error": {"code": "unavailable"}}, times=1)
    client = build_client(stub, shared_http_client, retry_max_attempts=0)
    try:
        with client.trace("run") as run:
            pass
        client.score(run.id, "helpfulness", 1.0)
        assert client.flush(timeout=5) is False
        # One failed request told the worker what it needed to know.
        assert [r.path for r in stub.requests] == [TRACES]
        assert client.flush(timeout=5) is True
        assert client.stats()["accepted"] == 2
    finally:
        client.close(timeout=2)


def test_a_row_is_given_up_on_once_it_has_been_failing_too_long(
    stub: StubServer, shared_http_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.inject("POST", TRACES, 503, {"error": {"code": "unavailable"}}, times=2)
    client = build_client(stub, shared_http_client, retry_max_attempts=0)
    try:
        with client.trace("stale"):
            pass
        assert client.flush(timeout=5) is False
        assert client.stats()["pending"] == 1

        monkeypatch.setattr(queue_module, "REQUEUE_MAX_AGE_SECONDS", -1.0)
        assert client.flush(timeout=5) is False
        stats = client.stats()
        assert stats["pending"] == 0 and stats["dropped_failed"] == 1
    finally:
        client.close(timeout=2)


def test_the_queue_ceiling_still_holds_through_an_outage(
    stub: StubServer, shared_http_client
) -> None:
    stub.inject("POST", TRACES, 503, {"error": {"code": "unavailable"}}, times=1_000)
    client = build_client(stub, shared_http_client, retry_max_attempts=0, max_queue_size=3)
    try:
        for index in range(6):
            with client.trace("run-{0}".format(index)):
                pass
            client.flush(timeout=5)
        stats = client.stats()
        assert stats["pending"] <= 3
        assert stats["dropped_overflow"] >= 3
    finally:
        client.close(timeout=2)


def test_closing_during_an_outage_drops_what_is_left_and_does_not_hang(
    stub: StubServer, shared_http_client
) -> None:
    stub.inject("POST", TRACES, 503, {"error": {"code": "unavailable"}}, times=1_000)
    client = build_client(stub, shared_http_client, retry_max_attempts=0, batch_max_items=1)
    for index in range(5):
        with client.trace("run-{0}".format(index)):
            pass
    began = time.monotonic()
    client.close(timeout=5)
    assert time.monotonic() - began < 4
    stats = client.stats()
    assert stats["pending"] == 0 and stats["dropped_failed"] == 5
    # Not one request per remaining batch: a process on its way out learns the
    # control plane is down once.
    assert len(stub.requests) <= 2


def test_a_forked_child_gets_a_working_flusher_and_none_of_the_parents_rows(
    stub: StubServer,
) -> None:
    """What ``os.register_at_fork`` runs in the child, called by hand: Windows cannot fork."""
    client = FulcrumOps(
        api_key="fo_test_key",
        base_url=stub.base_url,
        bootstrap=False,
        set_as_default=False,
        flush_on_exit=False,
        flush_interval_seconds=30.0,
    )
    try:
        with client.trace("queued-in-the-parent"):
            pass
        parent_worker = client._flusher._thread
        assert parent_worker is not None and parent_worker.is_alive()
        client._transport._ensure_client()

        # In the child the worker thread does not exist. Stand in for that by
        # leaving the parent's one parked and resetting as the fork hook would.
        held = client._flusher._cond
        client._reset_after_fork()

        assert client._flusher._cond is not held, "a lock held across fork() is held for ever"
        assert client._transport._client is None, "the parent's sockets are not the child's to use"
        assert client.stats()["pending"] == 0, "the parent still has those rows and will send them"

        with client.trace("traced-in-the-child"):
            pass
        assert client.flush(timeout=5) is True, "flush used to wait out its timeout for a dead worker"
        assert [row["name"] for row in sent(stub, "traces")] == ["traced-in-the-child"]
        assert client._flusher._thread is not parent_worker
    finally:
        client.close(timeout=2)
        with held:  # let the stand-in for "the thread fork() left behind" go home
            held.notify_all()


def test_flush_restarts_a_worker_that_did_not_survive(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client)
    try:
        # The state a fork leaves behind when nothing reset it: marked as
        # started, with no thread behind the mark.
        flusher = client._flusher
        flusher.close(timeout=2)
        flusher._stopping = False
        assert flusher._started is True and flusher._thread is None
        flusher._queues["traces"].append(
            queue_module.QueueItem({"name": "orphan", "start_time": "2026-01-01T00:00:00Z"}, 64)
        )
        assert client.flush(timeout=5) is True
        assert [row["name"] for row in sent(stub, "traces")] == ["orphan"]
    finally:
        client.close(timeout=2)
