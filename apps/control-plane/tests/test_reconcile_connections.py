"""Regressions for the Connection Center changes other owners handed over.

Each of these was found from outside ``services/connections.py`` -- by the owner
of the connector probe, of alerts, of the console -- and each pins a behaviour
that was wrong and that no test looked at. Probes go to a loopback socket, as in
``test_audit_connections``: a probe is a network call, and a closed port on this
machine is the one endpoint that is certain not to answer.
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import select, update

from fulcrum_ops_api.models.registry import Connection, ConnectionActivity, Platform


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _connection(admin_client, *, name: str, kind: str = "Custom REST API", **fields) -> dict:
    created = await admin_client.post(
        "/api/v1/connections", json={"name": name, "kind": kind, **fields}
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _stored_endpoint(db, tile: dict, url: str) -> None:
    """Put an endpoint on a row the way rows written before the schema check hold one."""
    await db.execute(
        update(Connection).where(Connection.id == tile["id"]).values(config={"endpoint_url": url})
    )


# ---------------------------------------------------------------------------
# An endpoint the HTTP library cannot parse
# ---------------------------------------------------------------------------


async def test_a_stored_endpoint_that_is_not_a_url_is_an_answer_not_a_500(admin_client, db):
    tile = await _connection(admin_client, name="Typo")
    await _stored_endpoint(db, tile, "https://erp.internal:port/")

    tested = await admin_client.post(f"/api/v1/connections/{tile['id']}/test")

    assert tested.status_code == 200, tested.text
    result = tested.json()
    assert result["ok"] is False
    assert result["data"]["detail"] == "Endpoint is not a valid URL"
    shown = (await admin_client.get(f"/api/v1/connections/{tile['id']}")).json()
    assert shown["status"] == "Disconnected"
    assert shown["status_detail"] == "Endpoint is not a valid URL", (
        "the tile says why, so the operator knows it is the address and not the network"
    )


async def test_one_unparseable_endpoint_does_not_fail_test_all_for_every_other_tile(
    admin_client, db
):
    broken = await _connection(admin_client, name="Placeholder pasted back")
    await _stored_endpoint(db, broken, "https://…")
    await _connection(
        admin_client, name="Nobody home",
        config={"endpoint_url": f"http://127.0.0.1:{_free_port()}/"},
    )

    tested = await admin_client.post("/api/v1/connections/test-all")

    assert tested.status_code == 200, "one bad row raised out of the gather: " + tested.text
    body = tested.json()["data"]
    assert body["summary"]["failed"] == 2
    assert {row["name"]: row["detail"] for row in body["results"]}[
        "Placeholder pasted back"
    ] == "Endpoint is not a valid URL"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://erp.internal:port/",  # a port that is not a number
        "https://\u2026",  # the Add form's placeholder, pasted back in
        "https://host:99999/",  # parses, and can never be connected to
        "https://host:0/",
        "https:///no-host",
    ],
)
async def test_an_endpoint_the_probe_could_never_open_is_refused_when_it_is_saved(
    admin_client, endpoint
):
    refused = await admin_client.post(
        "/api/v1/connections",
        json={"name": "Typo", "kind": "Custom REST API", "config": {"endpoint_url": endpoint}},
    )
    assert refused.status_code == 422, (
        "saved cleanly, and then every Test on it failed for a reason nobody was shown: "
        + refused.text
    )

    tile = await _connection(admin_client, name=f"Fine until edited {abs(hash(endpoint))}")
    edited = await admin_client.patch(
        f"/api/v1/connections/{tile['id']}", json={"config": {"endpoint_url": endpoint}}
    )
    assert edited.status_code == 422, edited.text


async def test_an_ordinary_endpoint_is_still_accepted(admin_client):
    for index, endpoint in enumerate(
        (
            "https://mcp.example.com/sse",
            "http://10.0.4.12:8080/health",  # a private address is an ordinary thing to connect
            "http://mcp-server:3000/",
            "https://[2001:db8::1]:8443/",
        )
    ):
        tile = await _connection(
            admin_client, name=f"Accepted {index}", config={"endpoint_url": endpoint}
        )
        assert tile["config"]["endpoint_url"] == endpoint


async def test_the_metadata_address_is_refused_in_its_ipv6_spelling_too(admin_client):
    refused = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Metadata, mapped", "kind": "Custom REST API",
            "config": {"endpoint_url": "http://[::ffff:169.254.169.254]/latest/meta-data/"},
        },
    )
    assert refused.status_code == 422, refused.text


# ---------------------------------------------------------------------------
# An agent platform with no endpoint to probe
# ---------------------------------------------------------------------------

CUSTOM_AGENT = Platform.CUSTOM_AGENT.value


async def test_a_platform_tile_with_no_endpoint_is_tested_against_the_registry(
    admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Orders Bot")
    await factory.agent(workspace, name="Registered, never wired up")
    tile = await _connection(
        admin_client, name="SDK agents", kind=CUSTOM_AGENT, config={"endpoint_url": None}
    )
    assert tile["status"] == "Disconnected", "only a test may say otherwise"

    tested = await admin_client.post(f"/api/v1/connections/{tile['id']}/test")

    assert tested.status_code == 200, (
        "412 'no endpoint configured': SDK agents push, so there is no URL, and the tile "
        "stayed red while its agents' runs were listed under it: " + tested.text
    )
    result = tested.json()
    assert result["ok"] is True
    assert result["data"]["status"] == "Connected"
    assert result["data"]["detail"].startswith("1 agent(s) reporting via SDK ingest")
    assert result["data"]["latency_ms"] is None, "nothing was timed: unmeasured, not 0"
    assert "no response" not in result["message"]
    assert "reporting via SDK ingest" in result["message"]

    # Which is what lets it be synced, and takes it out of the red KPI card.
    synced = await admin_client.post(f"/api/v1/connections/{tile['id']}/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["ok"] is True
    summary = (await admin_client.get("/api/v1/connections/summary")).json()
    assert (summary["connected"], summary["disconnected"]) == (1, 0)


async def test_the_registry_verdict_tells_registered_from_reporting_from_nobody(
    admin_client, db, factory, workspace, other_workspace, engine
):
    await factory.agent(workspace, name="Not wired up", platform=Platform.COPILOT_STUDIO.value)
    # Somebody else's agents are nobody's evidence here.
    await factory.provisioned_agent(
        other_workspace, engine, name="Theirs", platform=Platform.M365_COPILOT.value
    )
    waiting = await _connection(admin_client, name="Copilot", kind=Platform.COPILOT_STUDIO.value)
    empty = await _connection(admin_client, name="M365", kind=Platform.M365_COPILOT.value)

    warned = (await admin_client.post(f"/api/v1/connections/{waiting['id']}/test")).json()
    assert warned["data"]["status"] == "Warning"
    assert warned["data"]["health"] == "Warning"
    assert "0 agent(s) reporting via SDK ingest" in warned["data"]["detail"]

    nobody = (await admin_client.post(f"/api/v1/connections/{empty['id']}/test")).json()
    assert nobody["ok"] is False
    assert nobody["data"]["status"] == "Disconnected"
    shown = (await admin_client.get(f"/api/v1/connections/{empty['id']}")).json()
    assert "no agent is registered on M365 Copilot" in shown["status_detail"]
    feed = await db.scalars(
        select(ConnectionActivity).where(ConnectionActivity.connection_id == empty["id"])
    )
    assert ("Connection Failed", "Error") not in {(row.event, row.status) for row in feed}, (
        "nothing was called and nothing failed to answer"
    )


async def test_any_other_kind_with_no_endpoint_is_still_refused(admin_client):
    tile = await _connection(admin_client, name="Vectors", kind="Vector Database")

    refused = await admin_client.post(f"/api/v1/connections/{tile['id']}/test")

    assert refused.status_code == 412, refused.text
    assert "no endpoint configured" in refused.json()["error"]["message"]


async def test_test_all_reads_every_such_platform_tile_with_one_count_and_skips_none(
    admin_client, db, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Orders Bot")
    await factory.agent(workspace, name="Foundry Bot", platform=Platform.AZURE_AI_FOUNDRY.value)
    await _connection(admin_client, name="A - SDK agents", kind=CUSTOM_AGENT)
    await _connection(admin_client, name="B - SDK agents (EU)", kind=CUSTOM_AGENT)
    await _connection(admin_client, name="C - Foundry", kind=Platform.AZURE_AI_FOUNDRY.value)
    await _connection(admin_client, name="D - Power", kind=Platform.POWER_PLATFORM.value)
    await _connection(admin_client, name="E - Vectors", kind="Vector Database")
    off = await _connection(admin_client, name="F - Switched off", kind=CUSTOM_AGENT)
    await db.execute(update(Connection).where(Connection.id == off["id"]).values(enabled=False))

    tested = await admin_client.post("/api/v1/connections/test-all")

    assert tested.status_code == 200, tested.text
    body = tested.json()["data"]
    assert body["summary"] == {
        "requested": 6, "succeeded": 2, "warned": 1, "failed": 1, "skipped": 2,
    }, "the platform tiles were all counted as skipped, and stayed red"
    assert [(row["name"][0], row["status"]) for row in body["results"]] == [
        ("A", "Connected"), ("B", "Connected"), ("C", "Warning"), ("D", "Disconnected"),
    ]
