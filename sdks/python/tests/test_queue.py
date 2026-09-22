"""The batching flusher: what it batches, what it retries, and what it drops."""

from __future__ import annotations

import threading

from fulcrum_ops import FulcrumOps

from .conftest import build_client, sent
from .stub_server import StubServer


def batches(stub: StubServer, kind: str) -> list:
    """The bodies of every ingest request of ``kind``, one entry per request."""
    return [
        request.body
        for request in stub.requests
        if request.path == "/api/v1/ingest/{0}".format(kind)
    ]


def test_every_batch_carries_the_sdk_envelope(client: FulcrumOps, stub: StubServer) -> None:
    with client.trace("run"):
        pass
    client.flush(timeout=5)

    body = batches(stub, "traces")[0]
    assert body["sdk"] == "python"
    assert body["sdk_version"] == "1.0.2"
    assert body["agent"] == "checkout-agent"
    assert isinstance(body["traces"], list)


def test_batches_are_cut_at_the_item_limit(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client, batch_max_items=3)
    try:
        for index in range(7):
            with client.trace("run-{0}".format(index)):
                pass
        client.flush(timeout=5)

        sizes = [len(body["traces"]) for body in batches(stub, "traces")]
        assert sum(sizes) == 7
        assert max(sizes) <= 3
    finally:
        client.close(timeout=2)


def test_each_kind_goes_to_its_own_endpoint(client: FulcrumOps, stub: StubServer) -> None:
    with client.trace("run") as run:
        trace_id = run.id
    client.score(trace_id, "helpfulness", 0.9)
    client.log_feedback(trace_id=trace_id, rating=5)
    client.flush(timeout=5)

    paths = {request.path for request in stub.requests}
    assert "/api/v1/ingest/traces" in paths
    assert "/api/v1/ingest/scores" in paths
    assert "/api/v1/ingest/events" in paths


def test_the_queue_is_bounded_and_sheds_the_oldest(stub: StubServer, shared_http_client) -> None:
    """An unreachable control plane must not become the customer's OOM kill."""
    client = build_client(stub, shared_http_client, max_queue_size=5, flush_interval_seconds=30.0)
    try:
        for index in range(12):
            client._flusher.submit("traces", {"name": "run-{0}".format(index)})

        assert client._flusher.pending() == 5
        assert client.stats()["dropped_overflow"] == 7

        client.flush(timeout=5)
        # The five that survived are the five most recent.
        names = [row["name"] for row in sent(stub, "traces")]
        assert names == ["run-7", "run-8", "run-9", "run-10", "run-11"]
    finally:
        client.close(timeout=2)


def test_a_retryable_status_is_retried_then_succeeds(stub: StubServer, shared_http_client) -> None:
    stub.inject("POST", "/api/v1/ingest/traces", 503, {"error": {"code": "unavailable"}}, times=2)
    client = build_client(stub, shared_http_client, retry_max_attempts=3)
    try:
        with client.trace("needs-a-retry"):
            pass
        client.flush(timeout=5)

        stats = client.stats()
        assert stats["retries"] == 2
        assert stats["accepted"] == 1
        assert stats["dropped_failed"] == 0
        assert len(batches(stub, "traces")) == 3
    finally:
        client.close(timeout=2)


def test_a_non_retryable_status_is_not_retried(stub: StubServer, shared_http_client) -> None:
    stub.inject(
        "POST",
        "/api/v1/ingest/traces",
        401,
        {"error": {"code": "unauthenticated", "message": "That key was revoked."}},
        times=5,
    )
    seen: list = []
    client = build_client(
        stub,
        shared_http_client,
        retry_max_attempts=3,
        on_error=lambda error, operation: seen.append((error, operation)),
    )
    try:
        with client.trace("bad-key"):
            pass
        client.flush(timeout=5)

        assert len(batches(stub, "traces")) == 1, "an auth failure must not be retried"
        assert client.stats()["dropped_failed"] == 1
        error, operation = seen[-1]
        assert operation == "ingest.traces"
        assert error.status == 401
        assert "revoked" in str(error)
    finally:
        client.close(timeout=2)


def test_retry_after_is_honoured(stub: StubServer, shared_http_client) -> None:
    """The server saying when capacity returns beats the SDK's own guess."""
    slept: list = []
    stub.inject(
        "POST",
        "/api/v1/ingest/traces",
        429,
        {"error": {"code": "rate_limited"}},
        headers={"retry-after": "7"},
        times=1,
    )
    client = build_client(
        stub, shared_http_client, sleep=slept.append, retry_max_backoff_seconds=30.0
    )
    try:
        with client.trace("throttled"):
            pass
        client.flush(timeout=5)

        assert slept and slept[0] == 7.0
        assert client.stats()["accepted"] == 1
    finally:
        client.close(timeout=2)


def test_backoff_is_jittered_and_bounded() -> None:
    """Full jitter, so a fleet that failed together does not retry together."""
    import random

    from fulcrum_ops.transport import backoff_delay

    rng = random.Random(1)
    samples = [backoff_delay(3, 0.5, 30.0, None, rng) for _ in range(50)]

    assert len(set(samples)) > 1, "a fixed multiplier would synchronise a whole fleet"
    assert all(0.0 <= value <= 4.0 for value in samples), "attempt 3 caps at 0.5 * 2**3"

    # The ceiling wins over the exponential...
    assert backoff_delay(20, 0.5, 2.0, None, rng) <= 2.0
    # ...and over Retry-After too, so a server asking for an hour cannot wedge
    # the queue for an hour.
    assert backoff_delay(0, 0.5, 30.0, 3_600.0, rng) == 30.0


def test_an_oversized_batch_is_split_rather_than_dropped(
    stub: StubServer, shared_http_client
) -> None:
    stub.inject("POST", "/api/v1/ingest/traces", 413, {"error": {"code": "payload_too_large"}}, times=1)
    client = build_client(stub, shared_http_client, batch_max_items=100)
    try:
        for index in range(4):
            with client.trace("run-{0}".format(index)):
                pass
        client.flush(timeout=5)

        # One refused request, then the two halves, both accepted.
        assert client.stats()["accepted"] == 4
        assert client.stats()["dropped_failed"] == 0
        assert [len(body["traces"]) for body in batches(stub, "traces")] == [4, 2, 2]
    finally:
        client.close(timeout=2)


def test_per_item_rejections_are_counted_and_surfaced(
    stub: StubServer, shared_http_client, caplog
) -> None:
    """A 200 does not mean the rows landed; the verdict is in the per-item results."""
    stub.item_outcomes["traces"] = [
        {"outcome": "accepted"},
        {"outcome": "rejected", "code": "agent_unprovisioned", "reason": "No such agent"},
        {"outcome": "blocked", "code": "policy_blocked", "reason": "Refused by policy"},
    ]
    client = build_client(stub, shared_http_client, batch_max_items=10)
    try:
        for index in range(3):
            with client.trace("run-{0}".format(index)):
                pass
        with caplog.at_level("WARNING", logger="fulcrum_ops"):
            client.flush(timeout=5)

        stats = client.stats()
        assert stats["accepted"] == 1
        assert stats["rejected"] == 1
        assert stats["blocked"] == 1
        assert "agent_unprovisioned" in caplog.text
        assert "policy_blocked" in caplog.text
    finally:
        client.close(timeout=2)


def test_per_item_rejections_reach_the_error_handler(
    stub: StubServer, shared_http_client
) -> None:
    """A batch answered 200 with every row refused is the failure that goes unnoticed."""
    stub.item_outcomes["traces"] = [
        {"outcome": "rejected", "code": "agent_unprovisioned", "reason": "No telemetry project"},
        {"outcome": "rejected", "code": "agent_unprovisioned", "reason": "No telemetry project"},
        {"outcome": "blocked", "code": "policy_blocked", "reason": "Refused by policy"},
    ]
    seen: list = []
    client = build_client(
        stub,
        shared_http_client,
        batch_max_items=10,
        on_error=lambda error, operation: seen.append((error, operation)),
    )
    try:
        for index in range(3):
            with client.trace("run-{0}".format(index)):
                pass
        client.flush(timeout=5)

        codes = sorted(error.code for error, _ in seen)
        assert codes == ["agent_unprovisioned", "policy_blocked"], (
            "one notification per distinct reason, not one per row"
        )
        assert all(operation == "ingest.traces" for _, operation in seen)

        by_code = {error.code: error for error, _ in seen}
        assert "No telemetry project" in str(by_code["agent_unprovisioned"])
        assert by_code["agent_unprovisioned"].details["kind"] == "traces"
        assert by_code["agent_unprovisioned"].details["submitted"] == 3

        # The request itself succeeded, so the transport-failure counter stays
        # at zero while the per-item counters carry the news.
        stats = client.stats()
        assert stats["errors"] == 0
        assert stats["rejected"] == 2 and stats["blocked"] == 1
        assert stats["dropped_failed"] == 0
    finally:
        client.close(timeout=2)


def test_flush_without_a_worker_drains_inline(stub: StubServer, shared_http_client) -> None:
    """A script that submits once and flushes once still reports."""
    client = build_client(stub, shared_http_client, enabled=True)
    try:
        client._flusher.close(timeout=1)  # stop the worker
        client._flusher._stopping = False
        client._flusher._started = False

        client._flusher.submit("traces", {"name": "inline", "start_time": "2026-01-01T00:00:00Z"})
        assert client._flusher.flush(timeout=5) is True
        assert [row["name"] for row in sent(stub, "traces")] == ["inline"]
    finally:
        client.close(timeout=2)


def test_submitting_from_many_threads_loses_nothing(
    stub: StubServer, shared_http_client
) -> None:
    client = build_client(stub, shared_http_client, batch_max_items=10, max_queue_size=1_000)
    try:

        def worker(index: int) -> None:
            for step in range(20):
                with client.trace("t-{0}-{1}".format(index, step)):
                    pass

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        client.flush(timeout=10)
        assert len(sent(stub, "traces")) == 100
        assert client.stats()["dropped_overflow"] == 0
    finally:
        client.close(timeout=2)


def test_close_is_idempotent_and_flushes(stub: StubServer, shared_http_client) -> None:
    client = build_client(stub, shared_http_client)
    with client.trace("last-words"):
        pass
    client.close(timeout=5)
    client.close(timeout=5)

    assert [row["name"] for row in sent(stub, "traces")] == ["last-words"]


def test_a_closed_client_accepts_calls_and_reports_nothing(
    stub: StubServer, shared_http_client
) -> None:
    """A shutdown path that traces one last thing must not become a crash."""
    client = build_client(stub, shared_http_client)
    client.close(timeout=2)

    before = len(stub.requests)
    with client.trace("after-close"):
        pass
    assert client.score("t-1", "late", 1.0) is False
    assert client.flush(timeout=1) in (True, False)
    assert len(stub.requests) == before
