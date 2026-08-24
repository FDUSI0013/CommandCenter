"""Temporary: proves the harness works before the real suite is written."""
from __future__ import annotations

from fulcrum_ops_api.models.identity import Role


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200, response.text
    assert response.json()["checks"] == {"database": True, "telemetry": True}


async def test_anonymous_is_refused(client):
    response = await client.get("/api/v1/agents")
    assert response.status_code == 401


async def test_admin_sees_agents(admin_client, factory, workspace):
    await factory.agent(workspace, name="Support Bot")
    response = await admin_client.get("/api/v1/agents")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Support Bot"


async def test_engine_double_round_trip(engine_client, engine):
    project = await engine_client.ensure_project("northwind-support")
    assert project["name"] == "northwind-support"
    await engine_client.create_traces_batch(
        [{"id": "11111111-1111-1111-1111-111111111111",
          "project_name": "northwind-support", "name": "run"}]
    )
    rows = await engine_client.search_traces(project_id=project["id"])
    assert [row["id"] for row in rows] == ["11111111-1111-1111-1111-111111111111"]
    assert engine.trace_count("northwind-support") == 1


async def test_as_role(as_role):
    async with as_role(Role.VIEWER) as http:
        response = await http.get("/api/v1/auth/session")
        assert response.status_code == 200, response.text
        assert response.json()["role"] == "viewer"
