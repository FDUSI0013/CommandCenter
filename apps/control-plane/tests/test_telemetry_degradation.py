"""What every telemetry surface does when the store behind it is down.

There are two ways to answer a question you cannot answer. One is to say so.
The other is to return zero, which on this product means an empty Live Runs
table above a KPI row reading "0 runs, 0 errors, 100% success" — a screen that
looks like a healthy quiet morning and is actually a blackout. An operator would
believe it, and that is the whole problem.

So every endpoint whose answer comes from the telemetry engine has to fail
closed: 503, our error envelope, a stable code, and no number at all. The list
below is parametrised over those endpoints, and each case checks both halves —
the endpoint really does answer from telemetry when the store is up, and it
really does refuse when the store is down. Checking only the second half would
pass just as happily against a typo in the path.

The rest of the file covers the boundary: surfaces backed by our own database
carry on working, the registry reports *no* metrics rather than zeroed ones, an
outage is distinguishable from a refusal, and everything comes back when the
store does.

Agent Detail sits on that boundary and is deliberately NOT in the list. Most of
the page is our own rows -- and an outage is exactly when an operator needs its
Deactivate button -- so it is served with the telemetry half absent: ``stats``
null, no prompt history, a sentence saying why, and only the last figures that
*were* measured, stamped with when. It never refuses, and it never invents.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from conftest import error_code
from fulcrum_ops_api.models.registry import AgentStatus

# ---------------------------------------------------------------------------
# The surfaces that must fail closed
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Fixtures:
    """Ids the parametrised paths are formatted with."""

    agent_id: str
    run_id: str
    source_id: str
    configuration_id: str


@pytest.fixture
async def seeded(factory, workspace, engine) -> Fixtures:
    """A workspace with real telemetry in it, so every path below has an answer."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace = engine.add_trace(
        project_name=agent.engine_project_name,
        name="answer customer question",
        input={"question": "Where is my order?"},
        output={"answer": "It ships tomorrow."},
    )
    source = await factory.knowledge_source(workspace, name="Product documentation")
    await factory.quota(workspace, name="Tokens per month")
    await factory.memory_store(workspace, name="Conversation memory")
    # The seeded agent's model defaults to gpt-4o, so this Model configuration
    # binds it and its usage really is read from telemetry while the store is up.
    configuration = await factory.configuration(
        workspace,
        name="gpt-4o",
        payload={
            "provider": "azure-openai",
            "model": "gpt-4o",
            "temperature": 0.2,
            "max_tokens": 1024,
        },
    )
    return Fixtures(
        agent_id=agent.id,
        run_id=trace["id"],
        source_id=source.id,
        configuration_id=configuration.id,
    )


#: Every read whose answer is telemetry rather than our own rows.
TELEMETRY_READS: list[str] = [
    "/api/v1/runs",
    "/api/v1/runs/summary",
    "/api/v1/runs/export",
    "/api/v1/runs/{run_id}",
    "/api/v1/runs/{run_id}/trace",
    "/api/v1/runs/{run_id}/response",
    "/api/v1/runs/{run_id}/replay",
    "/api/v1/metrics/summary",
    "/api/v1/metrics/series",
    "/api/v1/metrics/models",
    "/api/v1/metrics/platforms",
    "/api/v1/metrics/export",
    "/api/v1/prompts",
    "/api/v1/prompts/summary",
    "/api/v1/prompts/export",
    "/api/v1/quota/summary",
    "/api/v1/quota/cost-breakdown",
    "/api/v1/quota/cost-by-service",
    "/api/v1/quota/usage-series",
    "/api/v1/quota/top-drivers",
    "/api/v1/quota/team-allocation",
    "/api/v1/quota/forecast",
    "/api/v1/quota/insights",
    "/api/v1/quota/export",
    "/api/v1/memory/summary",
    "/api/v1/memory/conversations",
    "/api/v1/memory/sessions",
    "/api/v1/memory/agent-state",
    "/api/v1/evaluations/datasets",
    "/api/v1/knowledge/{source_id}/documents",
    "/api/v1/knowledge/{source_id}/grounding",
    "/api/v1/configurations/{configuration_id}/usage",
]


def path_id(template: str) -> str:
    return template.removeprefix("/api/v1/")


def numbers_in(payload: Any) -> list[float]:
    """Every number anywhere in a response body."""
    if isinstance(payload, bool):
        return []
    if isinstance(payload, (int, float)):
        return [float(payload)]
    if isinstance(payload, dict):
        return [n for value in payload.values() for n in numbers_in(value)]
    if isinstance(payload, list):
        return [n for item in payload for n in numbers_in(item)]
    return []


@pytest.mark.parametrize("template", TELEMETRY_READS, ids=[path_id(p) for p in TELEMETRY_READS])
async def test_a_telemetry_read_fails_closed_when_the_store_is_down(
    admin_client, engine, seeded, template
):
    path = template.format(
        **dataclasses.asdict(seeded)
    )

    healthy = await admin_client.get(path)
    assert healthy.status_code == 200, (
        f"{path} must answer while the store is up, or this test proves nothing: "
        f"{healthy.text}"
    )

    engine.fail(503)
    down = await admin_client.get(path)

    assert down.status_code == 503, f"{path} answered {down.status_code}: {down.text[:200]}"
    body = down.json()
    assert set(body) == {"error"}, f"{path} returned data alongside its failure: {body}"
    assert body["error"]["code"] == "telemetry_unavailable"
    assert body["error"]["message"], "a refusal with no message is not actionable"
    assert body["error"]["request_id"] == down.headers["x-request-id"]


@pytest.mark.parametrize("template", TELEMETRY_READS, ids=[path_id(p) for p in TELEMETRY_READS])
async def test_a_failing_telemetry_read_reports_no_figure_at_all(
    admin_client, engine, seeded, template
):
    """Not one number: the envelope carries a code and a sentence, nothing else."""
    path = template.format(
        **dataclasses.asdict(seeded)
    )
    engine.fail(503)

    response = await admin_client.get(path)

    assert numbers_in(response.json()) == [], f"{path} answered with a number while blind"


# ---------------------------------------------------------------------------
# Writes that need the store
# ---------------------------------------------------------------------------


async def test_registering_an_agent_is_refused_rather_than_left_unprovisioned(
    admin_client, db, workspace, engine
):
    """An agent whose runs have nowhere to go is worse than a refused registration."""
    from fulcrum_ops_api.models.registry import Agent

    engine.fail(503)

    response = await admin_client.post(
        "/api/v1/agents",
        json={
            "name": "Refund Bot",
            "platform": "Custom Agent",
            "agent_type": "Pro-code",
            "environment": "Development",
        },
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"
    assert await db.count(Agent) == 0, "no half-registered row was left behind"


async def test_running_an_agent_from_the_console_is_refused(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail(503)

    response = await admin_client.post(f"/api/v1/agents/{agent.id}/run", json={})

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"


async def test_authoring_a_prompt_is_refused(admin_client, engine):
    engine.fail(503)
    response = await admin_client.post(
        "/api/v1/prompts", json={"name": "Support system", "template": "Hello"}
    )
    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"


async def test_ingest_tells_the_sdk_to_retry_rather_than_reporting_a_rejection(
    ingest_client, factory, workspace, engine
):
    """A rejection means "do not retry"; an outage means exactly the opposite."""
    import datetime as dt

    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail(503)

    response = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    "name": "answer customer question",
                    "start_time": dt.datetime.now(dt.UTC).isoformat(),
                }
            ],
        },
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"
    assert "retry" in response.json()["error"]["message"].lower()


async def test_auto_registration_does_not_half_create_an_agent(
    registrar_client, db, workspace, engine
):
    import datetime as dt

    from fulcrum_ops_api.models.registry import Agent

    engine.fail(503)

    response = await registrar_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Brand New Service",
            "traces": [{"name": "first run", "start_time": dt.datetime.now(dt.UTC).isoformat()}],
        },
    )

    assert response.status_code == 503, response.text
    assert await db.count(Agent) == 0


# ---------------------------------------------------------------------------
# Where the boundary is
# ---------------------------------------------------------------------------


async def test_the_registry_keeps_working_because_it_is_our_own_data(
    admin_client, factory, workspace, engine
):
    """Governance does not stop when telemetry does; it is a different database."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.secret(workspace, name="Payments key")
    await factory.approval(workspace, action="Refund over threshold")
    engine.fail(503)

    for path in (
        "/api/v1/agents",
        "/api/v1/secrets",
        "/api/v1/approvals",
        "/api/v1/policies",
        "/api/v1/audit",
        "/api/v1/alerts",
    ):
        response = await admin_client.get(path)
        assert response.status_code == 200, f"{path}: {response.text[:200]}"


async def test_the_registry_row_reports_no_metrics_rather_than_zeroed_ones(
    admin_client, factory, workspace, engine
):
    """``metrics: null`` says "not measured". ``runs_30d: 0`` says "measured, none"."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    healthy = await admin_client.get("/api/v1/agents")
    engine.fail(503)
    down = await admin_client.get("/api/v1/agents")

    assert healthy.json()["items"][0]["metrics"] is None
    assert down.status_code == 200, down.text
    assert down.json()["items"][0]["metrics"] is None
    assert down.json()["items"][0]["name"] == "Support Bot"


async def test_the_agent_detail_opens_blind_rather_than_drawing_an_empty_chart(
    admin_client, factory, workspace, engine
):
    """The detail screen is where the counters and the latency series live.

    It is also mostly our own data, so it opens during an outage -- with the
    telemetry half absent and saying so, never with a row of zeros in its place.
    """
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.add_trace(project_name=agent.engine_project_name, name="a run")

    healthy = await admin_client.get(f"/api/v1/agents/{agent.id}")
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["telemetry_error"] is None

    engine.fail(503)
    down = await admin_client.get(f"/api/v1/agents/{agent.id}")

    assert down.status_code == 200, down.text
    body = down.json()
    assert body["stats"] is None, "not measured is null, never a row of zeros"
    assert body["versions"] == []
    assert body["telemetry_error"], "the page has to say what it could not read"
    # The only figures left are the last ones that were really measured, dated.
    assert body["agent"]["metrics"]["computed_at"] is not None


async def test_health_says_the_store_is_the_thing_that_is_down(client, engine):
    healthy = await client.get("/health")
    assert healthy.json()["checks"] == {"database": True, "telemetry": True}

    engine.fail(503)
    degraded = await client.get("/health")

    body = degraded.json()
    assert body["checks"]["database"] is True
    assert body["checks"]["telemetry"] is False
    assert body["status"] in ("ok", "degraded")


# ---------------------------------------------------------------------------
# Telling one failure from another
# ---------------------------------------------------------------------------


async def test_a_refusal_by_the_store_is_reported_differently_from_an_outage(
    admin_client, factory, workspace, engine
):
    """503 either way, but the code says whether retrying could ever help."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail(400)

    response = await admin_client.get("/api/v1/runs")

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_rejected"
    assert numbers_in(response.json()) == []


async def test_a_run_that_does_not_exist_is_a_404_not_an_outage(
    admin_client, factory, workspace, engine
):
    """Fail-closed must not swallow the ordinary "no such thing" answer."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    response = await admin_client.get(
        "/api/v1/runs/1b3b5b7b-0000-4000-8000-000000000000"
    )

    assert response.status_code == 404, response.text
    assert error_code(response) == "not_found"


async def test_only_the_surfaces_that_need_the_failing_call_degrade(
    admin_client, factory, workspace, engine
):
    """A partial outage is reported as a partial outage."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.add_trace(project_name=agent.engine_project_name, name="a run")
    engine.fail_path("/traces", 503)

    runs = await admin_client.get("/api/v1/runs")
    prompts = await admin_client.get("/api/v1/prompts")

    assert runs.status_code == 503, runs.text
    assert prompts.status_code == 200, prompts.text


async def test_everything_comes_back_when_the_store_does(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.add_trace(project_name=agent.engine_project_name, name="a run")

    engine.fail(503)
    assert (await admin_client.get("/api/v1/runs")).status_code == 503

    engine.recover()
    recovered = await admin_client.get("/api/v1/runs")

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["total"] == 1


async def test_the_outage_is_never_described_in_the_stores_own_terms(
    admin_client, factory, workspace, engine, seeded
):
    """The customer is told about "the telemetry store", not about a vendor."""
    engine.fail(503)

    for path in ("/api/v1/runs", "/api/v1/metrics/summary", "/api/v1/prompts"):
        message = (await admin_client.get(path)).json()["error"]["message"]
        assert "telemetry" in message.lower()
        assert "http" not in message.lower(), "no upstream URL leaks into the message"
        assert "503" not in message, "no upstream status code leaks into the message"


async def test_an_outage_does_not_leak_a_traceback(admin_client, engine, seeded):
    engine.fail(503)
    response = await admin_client.get("/api/v1/runs")
    assert "Traceback" not in response.text
    assert "fulcrum_ops_api" not in response.text


async def test_a_viewer_sees_the_same_refusal_as_an_admin(
    viewer_client, admin_client, engine, seeded
):
    engine.fail(503)

    viewer = await viewer_client.get("/api/v1/runs")
    admin = await admin_client.get("/api/v1/runs")

    assert viewer.status_code == admin.status_code == 503
    assert error_code(viewer) == error_code(admin) == "telemetry_unavailable"


async def test_a_deactivated_agent_still_has_its_telemetry_read(
    admin_client, factory, workspace, engine
):
    """Sanity check on the seeded fixture: status is not what gates the read."""
    agent = await factory.provisioned_agent(
        workspace, engine, name="Retired Bot", status=AgentStatus.INACTIVE.value
    )
    engine.add_trace(project_name=agent.engine_project_name, name="an old run")

    response = await admin_client.get("/api/v1/runs")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
