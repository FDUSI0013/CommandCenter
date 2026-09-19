"""Regressions for the Connection Center defects found by the 2026-09-18 audit.

Each test pins one behaviour that was wrong in production and invisible in the
suite, because nothing exercised it: the traffic view had no test at all, the
probe had never met an endpoint that streams, and no test looked at a tile the
day after it was synced.

Telemetry goes through the engine double over the real adapter. Probes go to a
small server on a loopback socket -- a probe is a network call, and the defects
were in how it used the network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import uuid
from collections.abc import AsyncIterator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select, update

from fulcrum_ops_api.models.registry import Connection, ConnectionActivity, Platform
from fulcrum_ops_api.services import connections as service

CUSTOM_AGENT = Platform.CUSTOM_AGENT.value


@pytest.fixture(autouse=True)
def _no_traffic_memory(monkeypatch):
    """A test seeds spans and reads them straight back; it must see them.

    The test that is about the memory switches it on for itself.
    """
    monkeypatch.setattr(service, "TRAFFIC_CACHE_SECONDS", 0.0)
    service._traffic_scans.invalidate()
    yield
    service._traffic_scans.invalidate()


async def _connection(admin_client, *, name: str, kind: str = CUSTOM_AGENT, **fields) -> dict:
    created = await admin_client.post(
        "/api/v1/connections", json={"name": name, "kind": kind, **fields}
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _spans(engine_client, project_name: str, *, name: str, span_type: str, count: int):
    """Report ``count`` finished spans of one name and type into one project."""
    trace_id = str(uuid.uuid4())
    started = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    await engine_client.create_spans_batch(
        [
            {
                "trace_id": trace_id,
                "project_name": project_name,
                "name": name,
                "type": span_type,
                "start_time": (started + dt.timedelta(seconds=index)).isoformat(),
                "end_time": (started + dt.timedelta(seconds=index, milliseconds=40)).isoformat(),
                # What a real span carries and this view never reads.
                "input": {"prompt": "x" * 64},
                "output": {"completion": "y" * 64},
            }
            for index in range(count)
        ]
    )


# ---------------------------------------------------------------------------
# Tool Calls & Data
# ---------------------------------------------------------------------------


async def test_the_traffic_scan_asks_the_store_for_tool_spans_only_and_no_payloads(
    admin_client, factory, workspace, engine, engine_client
):
    agent = await factory.provisioned_agent(workspace, engine, name="Orders Bot")
    engine.add_trace(project_name=agent.engine_project_name)
    engine.add_trace(project_name=agent.engine_project_name)
    await _spans(
        engine_client, agent.engine_project_name,
        name="lookup_order", span_type="tool", count=3
    )
    await _spans(
        engine_client, agent.engine_project_name,
        name="vector_search", span_type="general", count=2
    )
    await _spans(engine_client, agent.engine_project_name, name="chat", span_type="llm", count=5)
    tile = await _connection(admin_client, name="SDK agents")

    engine.reset_calls()
    answered = await admin_client.get(f"/api/v1/connections/{tile['id']}/traffic")
    assert answered.status_code == 200, answered.text
    traffic = answered.json()

    assert traffic["agents"] == 1
    assert traffic["runs"] == 2
    assert [(row["name"], row["calls"]) for row in traffic["tool_calls"]] == [("lookup_order", 3)]
    assert [(row["name"], row["operations"]) for row in traffic["data_flows"]] == [
        ("vector_search", 2)
    ]
    assert traffic["truncated"] is False

    span_reads = [call for call in engine.calls_to("/spans") if call.method == "GET"]
    assert span_reads, "the view is built from the span list"
    assert {call.param("type") for call in span_reads} == {"tool", "general"}, (
        "llm spans were fetched with their full prompts and dropped in Python"
    )
    assert {call.param("truncate") for call in span_reads} == {"true"}
    assert {call.param("strip_attachments") for call in span_reads} == {"true"}

    assert engine.calls_to("/traces/stats") == [], (
        "the run count is one statistics read; the token fan-out is one call per agent "
        "for a number this view never shows"
    )


async def test_a_busy_agent_early_in_the_alphabet_does_not_spend_the_whole_scan(
    admin_client, factory, workspace, engine, engine_client, monkeypatch
):
    monkeypatch.setattr(service, "MAX_TRAFFIC_SPANS", 40)
    monkeypatch.setattr(service, "TRAFFIC_PAGE_SIZE", 10)
    busy = await factory.provisioned_agent(workspace, engine, name="Aardvark Bot")
    quiet = await factory.provisioned_agent(workspace, engine, name="Zebra Bot")
    await _spans(
        engine_client, busy.engine_project_name,
        name="spam_tool", span_type="tool", count=60
    )
    await _spans(
        engine_client, quiet.engine_project_name,
        name="zebra_tool", span_type="tool", count=4
    )
    tile = await _connection(admin_client, name="SDK agents")

    traffic = (await admin_client.get(f"/api/v1/connections/{tile['id']}/traffic")).json()

    calls = {row["name"]: row["calls"] for row in traffic["tool_calls"]}
    assert calls.get("zebra_tool") == 4, (
        "one shared counter, read in name order, never reached the second agent"
    )
    assert calls["spam_tool"] == 20, "each agent reads its own share of the cap, and no more"
    assert traffic["truncated"] is True
    assert traffic["agents"] == 2


async def test_a_scan_that_ends_on_a_full_page_with_nothing_beyond_it_is_not_a_floor(
    admin_client, factory, workspace, engine, engine_client, monkeypatch
):
    monkeypatch.setattr(service, "MAX_TRAFFIC_SPANS", 10)
    monkeypatch.setattr(service, "TRAFFIC_PAGE_SIZE", 10)
    agent = await factory.provisioned_agent(workspace, engine, name="Exact Bot")
    await _spans(
        engine_client, agent.engine_project_name,
        name="only_tool", span_type="tool", count=10
    )
    tile = await _connection(admin_client, name="SDK agents")

    traffic = (await admin_client.get(f"/api/v1/connections/{tile['id']}/traffic")).json()

    assert traffic["tool_calls"][0]["calls"] == 10
    assert traffic["truncated"] is False, "every span was read; the count is a count"


async def test_a_slow_store_gets_a_partial_answer_inside_the_deadline_not_a_hang(
    admin_client, factory, workspace, engine, engine_client, monkeypatch
):
    agent = await factory.provisioned_agent(workspace, engine, name="Slow Bot")
    await _spans(
        engine_client, agent.engine_project_name,
        name="lookup_order", span_type="tool", count=2
    )
    tile = await _connection(admin_client, name="SDK agents")

    # The store answers, eventually: far later than anyone is still listening.
    real = engine_client.list_spans

    async def crawl(**kwargs):
        await asyncio.sleep(8.0)
        return await real(**kwargs)

    monkeypatch.setattr(engine_client, "list_spans", crawl)
    monkeypatch.setattr(service, "TRAFFIC_DEADLINE_SECONDS", 1.5)

    started = asyncio.get_running_loop().time()
    answered = await admin_client.get(f"/api/v1/connections/{tile['id']}/traffic")
    waited = asyncio.get_running_loop().time() - started

    assert answered.status_code == 200, answered.text
    assert waited < 6.0, f"the scan must stop at its deadline, not run for {waited:.1f}s"
    traffic = answered.json()
    assert traffic["truncated"] is True
    assert traffic["tool_calls"] == []
    assert traffic["note"], "an answer cut short by the clock says so"
    assert traffic["runs"] == 0, "the run count is its own read and did arrive"


async def test_a_run_count_that_never_arrived_is_unknown_rather_than_zero(
    admin_client, factory, workspace, engine, engine_client, monkeypatch
):
    agent = await factory.provisioned_agent(workspace, engine, name="Counted Bot")
    engine.add_trace(project_name=agent.engine_project_name)
    tile = await _connection(admin_client, name="SDK agents")

    real = engine_client.get_project_stats

    async def crawl(**kwargs):
        await asyncio.sleep(8.0)
        return await real(**kwargs)

    monkeypatch.setattr(engine_client, "get_project_stats", crawl)
    monkeypatch.setattr(service, "TRAFFIC_DEADLINE_SECONDS", 1.5)

    traffic = (await admin_client.get(f"/api/v1/connections/{tile['id']}/traffic")).json()

    assert traffic["runs"] is None, "a number that was not measured is not reported as 0"
    assert traffic["truncated"] is True


async def test_reopening_the_traffic_view_does_not_scan_the_store_again(
    admin_client, factory, workspace, engine, engine_client, monkeypatch
):
    monkeypatch.setattr(service, "TRAFFIC_CACHE_SECONDS", 60.0)
    agent = await factory.provisioned_agent(workspace, engine, name="Popular Bot")
    await _spans(
        engine_client, agent.engine_project_name,
        name="lookup_order", span_type="tool", count=2
    )
    first = await _connection(admin_client, name="SDK agents")
    second = await _connection(admin_client, name="SDK agents (EU)")

    engine.reset_calls()
    one = (await admin_client.get(f"/api/v1/connections/{first['id']}/traffic")).json()
    scans = len(engine.calls)
    assert scans > 0
    two = (await admin_client.get(f"/api/v1/connections/{second['id']}/traffic")).json()

    assert len(engine.calls) == scans, "two tiles of one kind share one platform's scan"
    assert two["tool_calls"] == one["tool_calls"]
    assert (two["connection_id"], two["name"]) == (second["id"], "SDK agents (EU)"), (
        "the aggregates are remembered, not the response"
    )

    # A different window is a different question.
    await admin_client.get(f"/api/v1/connections/{first['id']}/traffic?window_days=7")
    assert len(engine.calls) > scans


async def test_the_traffic_view_still_fails_closed_when_the_store_is_down(
    admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Dark Bot")
    tile = await _connection(admin_client, name="SDK agents")

    engine.fail(503)
    answered = await admin_client.get(f"/api/v1/connections/{tile['id']}/traffic")

    assert answered.status_code == 503, answered.text


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def endpoints() -> AsyncIterator[str]:
    """Endpoints a probe meets in the field, on a real loopback socket."""
    app = FastAPI()

    def event_stream(interval: float, seconds: float) -> StreamingResponse:
        async def pings():
            for _ in range(int(seconds / interval)):
                yield b": keep-alive\n\n"
                await asyncio.sleep(interval)

        return StreamingResponse(pings(), media_type="text/event-stream")

    @app.get("/sse/quiet")
    async def quiet():
        # Healthy: 200 at once, then a keep-alive less often than the read timeout.
        return event_stream(interval=3.0, seconds=6.0)

    @app.get("/sse/chatty")
    async def chatty():
        # Healthy: 200 at once, then a chunk far more often than the read timeout,
        # so no per-read timeout ever fires while the body is being downloaded.
        return event_stream(interval=0.1, seconds=10.0)

    @app.get("/hop/{number}")
    async def hop(number: int):
        return RedirectResponse(f"/hop/{number + 1}")

    @app.get("/to-metadata")
    async def to_metadata():
        return RedirectResponse("http://169.254.169.254/latest/meta-data/")

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sick")
    async def sick():
        return JSONResponse({"ok": False}, status_code=503)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", timeout_graceful_shutdown=1
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - the server exposes a flag, not an event
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving


async def _probe_tile(admin_client, url: str) -> tuple[dict, dict, float]:
    tile = await _connection(
        admin_client, name=f"Probe {uuid.uuid4().hex[:6]}", kind="MCP Server",
        config={"endpoint_url": url},
    )
    started = asyncio.get_running_loop().time()
    tested = await admin_client.post(f"/api/v1/connections/{tile['id']}/test")
    waited = asyncio.get_running_loop().time() - started
    assert tested.status_code == 200, tested.text
    shown = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()
    return tested.json(), shown, waited


async def test_a_healthy_event_stream_is_connected_not_timed_out(
    admin_client, endpoints, monkeypatch
):
    monkeypatch.setattr(service, "PROBE_TIMEOUT_SECONDS", 2.0)

    result, tile, _ = await _probe_tile(admin_client, f"{endpoints}/sse/quiet")

    assert result["ok"] is True, result
    assert result["data"]["http_status"] == 200
    assert tile["status"] == "Connected", (
        "the server answered 200 at once; waiting for the body read as 'no response'"
    )
    assert tile["health"] == "Healthy"


async def test_a_stream_that_never_pauses_cannot_hold_the_probe_open(
    admin_client, endpoints, monkeypatch
):
    monkeypatch.setattr(service, "PROBE_TIMEOUT_SECONDS", 2.0)

    result, tile, waited = await _probe_tile(admin_client, f"{endpoints}/sse/chatty")

    assert tile["status"] == "Connected"
    assert waited < 5.0, f"the probe ends at the status line, not after {waited:.1f}s of body"
    assert result["data"]["latency_ms"] < 5000, (
        "latency is time to the answer, not to the last byte"
    )


async def test_test_all_is_bounded_by_the_probe_deadline_not_by_the_slowest_body(
    admin_client, endpoints, monkeypatch
):
    monkeypatch.setattr(service, "PROBE_TIMEOUT_SECONDS", 2.0)
    for index in range(3):
        await _connection(
            admin_client, name=f"Stream {index}", kind="MCP Server",
            config={"endpoint_url": f"{endpoints}/sse/chatty"},
        )

    started = asyncio.get_running_loop().time()
    tested = await admin_client.post("/api/v1/connections/test-all")
    waited = asyncio.get_running_loop().time() - started

    assert tested.status_code == 200, tested.text
    assert tested.json()["data"]["summary"]["succeeded"] == 3
    assert waited < 6.0, f"Test All waited {waited:.1f}s on bodies nobody reads"


async def test_a_redirect_loop_is_reported_not_followed_twenty_times(admin_client, endpoints):
    result, tile, _ = await _probe_tile(admin_client, f"{endpoints}/hop/1")

    assert result["ok"] is False
    assert "redirect" in result["data"]["detail"].lower()
    assert tile["status"] == "Disconnected"


async def test_the_probe_will_not_call_the_instance_metadata_address(admin_client, endpoints):
    refused = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Metadata", "kind": "Custom REST API",
            "config": {"endpoint_url": "http://169.254.169.254/latest/meta-data/"},
        },
    )
    assert refused.status_code == 422, refused.text

    # Nor by way of somebody else's redirect.
    result, _, _ = await _probe_tile(admin_client, f"{endpoints}/to-metadata")
    assert result["ok"] is False
    assert "link-local" in result["data"]["detail"], (
        "refused at the hop, rather than timing out against an address nobody should call"
    )


# ---------------------------------------------------------------------------
# Agents Using, the delete guard, and what a sync does
# ---------------------------------------------------------------------------


async def test_agents_using_is_counted_from_the_platform_link_not_read_from_a_dead_column(
    admin_client, factory, workspace, other_workspace
):
    for index in range(3):
        await factory.agent(workspace, name=f"SDK Bot {index}")
    await factory.agent(workspace, name="Foundry Bot", platform=Platform.AZURE_AI_FOUNDRY.value)
    await factory.agent(other_workspace, name="Somebody else's bot")
    sdk = await _connection(admin_client, name="SDK agents")
    await _connection(admin_client, name="SDK agents (EU)")
    await _connection(admin_client, name="Foundry", kind=Platform.AZURE_AI_FOUNDRY.value)
    await _connection(admin_client, name="Vault", kind="Key Vault")

    assert sdk["linked_agent_count"] == 3, "the create response is a read like any other"

    shown = (await admin_client.get(f"/api/v1/connections/{sdk['id']}")).json()
    assert shown["linked_agent_count"] == 3, "nothing ever wrote the column, so this said 0"

    listed = (await admin_client.get("/api/v1/connections")).json()["items"]
    assert {row["name"]: row["linked_agent_count"] for row in listed} == {
        "SDK agents": 3, "SDK agents (EU)": 3, "Foundry": 1, "Vault": 0,
    }

    ranked = (await admin_client.get("/api/v1/connections?sort=-linked_agent_count")).json()
    assert [row["linked_agent_count"] for row in ranked["items"]] == [3, 3, 1, 0], (
        "the sort key has to order by the number the column shows"
    )

    patched = await admin_client.patch(
        f"/api/v1/connections/{sdk['id']}", json={"kind": Platform.AZURE_AI_FOUNDRY.value}
    )
    assert patched.json()["linked_agent_count"] == 1, "a tile that changes kind changes agents"


async def test_the_summary_counts_each_linked_agent_once(admin_client, factory, workspace):
    for index in range(3):
        await factory.agent(workspace, name=f"SDK Bot {index}")
    await factory.agent(workspace, name="Unlinked", platform=Platform.COPILOT_STUDIO.value)
    await _connection(admin_client, name="SDK agents")
    await _connection(admin_client, name="SDK agents (EU)")

    summary = (await admin_client.get("/api/v1/connections/summary")).json()

    assert summary["linked_agents"] == 3, (
        "two tiles of one kind share their agents; nothing is linked to Copilot Studio"
    )


async def test_reading_a_tile_does_not_write_the_derived_count_back(
    admin_client, db, factory, workspace
):
    await factory.agent(workspace, name="SDK Bot")
    tile = await _connection(admin_client, name="SDK agents")
    before = await db.get(Connection, tile["id"])

    await admin_client.get(f"/api/v1/connections/{tile['id']}")
    await admin_client.get("/api/v1/connections")

    after = await db.get(Connection, tile["id"])
    assert after.updated_at == before.updated_at, "a GET moved the concurrency token"
    assert after.linked_agent_count == 0, "the unused column is left alone"


async def test_the_last_connection_of_a_kind_cannot_go_while_agents_run_on_it(
    admin_client, factory, workspace
):
    await factory.agent(workspace, name="SDK Bot")
    first = await _connection(admin_client, name="SDK agents")
    duplicate = await _connection(admin_client, name="SDK agents (copy)")

    removed = await admin_client.delete(f"/api/v1/connections/{duplicate['id']}")
    assert removed.status_code == 204, "a duplicate tile can always be removed"

    refused = await admin_client.delete(f"/api/v1/connections/{first['id']}")
    assert refused.status_code == 412, "the guard read a column that was always 0"
    message = refused.json()["error"]["message"]
    assert "1 agent(s) still run on Custom Agent" in message
    assert "etach" not in message, "there is no detach; the message must not ask for one"


async def test_a_sync_reports_what_it_found_instead_of_a_bare_counter(
    admin_client, db, factory, workspace, endpoints
):
    for index in range(2):
        await factory.agent(workspace, name=f"SDK Bot {index}")
    tile = await _connection(
        admin_client, name="SDK agents", config={"endpoint_url": f"{endpoints}/ok"}
    )
    assert (await admin_client.post(f"/api/v1/connections/{tile['id']}/test")).json()["ok"]

    synced = (await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")).json()

    assert synced["ok"] is True
    assert synced["data"]["linked_agent_count"] == 2
    assert synced["data"]["http_status"] == 200
    assert synced["data"]["syncs_today"] == 1
    assert "2 agent(s) on this platform, HTTP 200" in synced["data"]["detail"]
    feed = await db.scalars(
        select(ConnectionActivity).where(ConnectionActivity.event == "Sync Completed")
    )
    assert [row.details for row in feed] == [synced["data"]["detail"]]


async def test_a_sync_with_no_endpoint_says_that_it_only_counted(
    admin_client, db, factory, workspace
):
    await factory.agent(workspace, name="SDK Bot")
    tile = await _connection(admin_client, name="SDK agents")
    # No endpoint, so no test can connect it; a seed or an operator marked it so.
    await db.execute(
        update(Connection).where(Connection.id == tile["id"]).values(status="Connected")
    )

    synced = (await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")).json()

    assert synced["ok"] is True
    assert synced["data"]["reachable"] is None
    assert "No endpoint configured" in synced["data"]["detail"]


async def test_a_sync_that_reaches_nobody_is_not_reported_as_a_sync(
    admin_client, db, factory, workspace, endpoints
):
    tile = await _connection(
        admin_client, name="Gone", kind="Custom REST API",
        config={"endpoint_url": f"http://127.0.0.1:{_free_port()}/nobody-home"},
    )
    await db.execute(
        update(Connection).where(Connection.id == tile["id"]).values(status="Connected")
    )

    synced = (await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")).json()

    assert synced["ok"] is False, "the toast said 'synchronised' having contacted nothing"
    assert synced["data"]["synced"] is False
    shown = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()
    assert shown["status"] == "Disconnected"
    assert shown["last_sync_at"] is None, "nothing was synchronised, so Last Sync does not move"
    assert shown["syncs_today"] == 0


async def test_sync_all_probes_together_and_separates_warnings_from_successes(
    admin_client, db, endpoints, monkeypatch
):
    monkeypatch.setattr(service, "PROBE_TIMEOUT_SECONDS", 2.0)
    for name, path in (("Fine", "/ok"), ("Sick", "/sick"), ("Slow A", "/sse/chatty")):
        await _connection(
            admin_client, name=name, kind="Custom REST API",
            config={"endpoint_url": f"{endpoints}{path}"},
        )
    await _connection(admin_client, name="Never tested", kind="Custom REST API")
    await db.execute(
        update(Connection).where(Connection.name != "Never tested").values(status="Connected")
    )

    synced = (await admin_client.post("/api/v1/connections/sync-all")).json()

    assert synced["data"]["summary"] == {
        "requested": 4, "succeeded": 2, "warned": 1, "failed": 0, "skipped": 1,
    }
    assert synced["ok"] is False, "a tile answering 503 is not 'all synchronised'"


# ---------------------------------------------------------------------------
# The operator's note
# ---------------------------------------------------------------------------

NOTE = "Owned by Platform team - rotate key before 2026-10-01"


async def test_a_probe_leaves_the_operators_note_alone(admin_client, db, endpoints):
    tile = await _connection(
        admin_client, name="Noted", kind="Custom REST API", note=NOTE,
        config={"endpoint_url": f"{endpoints}/ok"},
    )
    url = f"/api/v1/connections/{tile['id']}"

    assert (await admin_client.post(f"{url}/test")).json()["ok"] is True
    assert (await admin_client.get(url)).json()["note"] == NOTE, (
        "a healthy probe set the operator's note to NULL"
    )

    repointed = await admin_client.patch(
        url, json={"config": {"endpoint_url": f"{endpoints}/sick"}}
    )
    assert repointed.status_code == 200, repointed.text
    sick = (await admin_client.post(f"{url}/test")).json()
    assert sick["data"]["http_status"] == 503
    assert (await admin_client.get(url)).json()["note"] == NOTE, (
        "a failing probe replaced the operator's note with its own diagnostic"
    )

    assert (await admin_client.post("/api/v1/connections/test-all")).status_code == 200
    assert (await admin_client.get(url)).json()["note"] == NOTE, "Test All is the same probe"

    found = (await admin_client.get("/api/v1/connections?q=rotate+key")).json()["items"]
    assert [row["id"] for row in found] == [tile["id"]], "the note is a search column"

    # What the probe saw is not lost: it is on the feed, and the tile reads it
    # back from there under a name of its own.
    feed = await db.scalars(
        select(ConnectionActivity)
        .where(ConnectionActivity.connection_id == tile["id"])
        .where(ConnectionActivity.status == "Warning")
    )
    assert feed and all(row.details.startswith("HTTP 503 in ") for row in feed)
    shown = (await admin_client.get(url)).json()
    assert shown["status_detail"].startswith("HTTP 503 in "), shown
    listed = (await admin_client.get("/api/v1/connections")).json()["items"]
    assert listed[0]["status_detail"] == shown["status_detail"]

    # It is the *last* probe's, and nobody can write it.
    healed = await admin_client.patch(
        url,
        json={"config": {"endpoint_url": f"{endpoints}/ok"}, "status_detail": "made up"},
    )
    assert healed.json()["status_detail"] == shown["status_detail"], "PATCH is a read too"
    assert (await admin_client.post(f"{url}/test")).json()["ok"] is True
    healthy = (await admin_client.get(url)).json()
    assert healthy["status_detail"] is None, "a clean answer leaves nothing to explain"
    assert healthy["note"] == NOTE


async def test_a_note_the_old_probe_wrote_is_cleared_but_a_real_one_never_is(
    admin_client, db, endpoints
):
    left_behind = await _connection(
        admin_client, name="Clobbered", kind="Custom REST API",
        config={"endpoint_url": f"{endpoints}/ok"},
    )
    quoting = await _connection(
        admin_client, name="Quoting", kind="Custom REST API",
        note="Saw HTTP 503 in 120ms twice last week; ask networking",
        config={"endpoint_url": f"{endpoints}/ok"},
    )
    # What the defect left in production rows: the probe's text, in the note.
    await db.execute(
        update(Connection)
        .where(Connection.id == left_behind["id"])
        .values(note="HTTP 503 in 120ms")
    )

    assert (await admin_client.post("/api/v1/connections/test-all")).status_code == 200

    assert (await db.get(Connection, left_behind["id"])).note is None
    assert (await db.get(Connection, quoting["id"])).note == (
        "Saw HTTP 503 in 120ms twice last week; ask networking"
    ), "a note that merely mentions a status is the operator's"


# ---------------------------------------------------------------------------
# Syncs today
# ---------------------------------------------------------------------------


async def _synced_yesterday(db, tile: dict, *, syncs: int) -> dt.datetime:
    yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    await db.execute(
        update(Connection)
        .where(Connection.id == tile["id"])
        .values(status="Connected", syncs_today=syncs, last_sync_at=yesterday)
    )
    return yesterday


async def test_yesterdays_syncs_are_not_shown_as_todays(admin_client, db):
    tile = await _connection(admin_client, name="SDK agents")
    quiet = await _connection(admin_client, name="SDK agents (EU)")
    await _synced_yesterday(db, tile, syncs=5)
    await _synced_yesterday(db, quiet, syncs=35)

    shown = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()
    assert shown["syncs_today"] == 0, "nobody has synced today; 5 was yesterday's count"
    assert shown["last_sync_at"] is not None, "Last Sync still says when it was"

    listed = (await admin_client.get("/api/v1/connections")).json()["items"]
    assert {row["name"]: row["syncs_today"] for row in listed} == {
        "SDK agents": 0, "SDK agents (EU)": 0,
    }

    summary = (await admin_client.get("/api/v1/connections/summary")).json()
    assert summary["syncs_today"] == 0, "the KPI card read '1d ago - 40 syncs today'"
    assert summary["last_sync_at"] is not None

    # The next sync starts today's count; the tile nobody synced stays at none.
    first = (await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")).json()
    assert first["data"]["syncs_today"] == 1
    second = (await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")).json()
    assert second["data"]["syncs_today"] == 2

    shown = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()
    assert shown["syncs_today"] == 2
    summary = (await admin_client.get("/api/v1/connections/summary")).json()
    assert summary["syncs_today"] == 2, "only tiles last synced today count towards today"


async def test_two_syncs_at_once_are_both_counted(admin_client, db):
    tile = await _connection(admin_client, name="SDK agents")
    await db.execute(
        update(Connection).where(Connection.id == tile["id"]).values(status="Connected")
    )
    url = f"/api/v1/connections/{tile['id']}/sync"

    answers = await asyncio.gather(*(admin_client.post(url) for _ in range(3)))

    assert [answer.status_code for answer in answers] == [200, 200, 200]
    assert sorted(answer.json()["data"]["syncs_today"] for answer in answers) == [1, 2, 3], (
        "each sync is told its own place in the day's count"
    )
    stored = await db.get(Connection, tile["id"])
    assert stored.syncs_today == 3, "a read-modify-write in Python loses one of these"


async def test_a_sync_is_bookkeeping_and_does_not_move_the_concurrency_token(admin_client, db):
    tile = await _connection(admin_client, name="SDK agents")
    # Last edited an hour ago: the guard allows a second of slack for the JSON
    # round trip, so a token that moves has to be seen to move by more than that.
    await db.execute(
        update(Connection)
        .where(Connection.id == tile["id"])
        .values(status="Connected", updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
    )
    opened = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()

    assert (await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")).json()["ok"]

    saved = await admin_client.patch(
        f"/api/v1/connections/{tile['id']}",
        json={"note": "edited after a sync", "expected_updated_at": opened["updated_at"]},
    )
    assert saved.status_code == 200, (
        "Configure was open while somebody pressed Sync Now; that is not a conflicting edit"
    )


async def test_what_a_syncs_probe_measured_does_not_move_the_token_either(
    admin_client, db, endpoints
):
    # The usual tile: it has an endpoint, so its sync re-probes it, and the
    # latency it measures differs from the last one nearly every time.
    tile = await _connection(
        admin_client, name="Probed", kind="Custom REST API",
        config={"endpoint_url": f"{endpoints}/ok"},
    )
    await db.execute(
        update(Connection)
        .where(Connection.id == tile["id"])
        .values(
            status="Connected", health="Healthy", latency_ms=987_654,
            updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
        )
    )
    opened = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()

    synced = (await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")).json()
    assert synced["ok"] is True, synced

    shown = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()
    assert shown["latency_ms"] == synced["data"]["latency_ms"] != 987_654, (
        "the measurement is still recorded on the tile"
    )
    assert shown["updated_at"] == opened["updated_at"], (
        "a measurement is an observation about the tile, not an edit of it"
    )
