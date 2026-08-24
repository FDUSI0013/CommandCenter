"""The boundary between tenants, checked on every collection that has one.

Workspace scoping is not a feature of a few endpoints; it is a property every
single query has to have. One `select` that forgets its `where` clause leaks a
customer's agents, credentials or approval history to another customer, and no
amount of correctness elsewhere makes up for it. So this file does the boring,
exhaustive thing: for each collection, build a row in Northwind, then look from
Contoso and prove nothing moved.

Three shapes of leak are covered, because they fail independently:

* **The list.** Contoso's total is unchanged by anything Northwind creates.
* **The lookup.** Northwind's id, addressed from Contoso, is 404 — not 403,
  which would confirm the id exists.
* **The narrowing.** A search or filter is another query, and a `where` clause
  missing from *that* one leaks just as much; the export is a third.

Two collections are not rows in our database at all — runs and prompts live in
the telemetry engine — so they are proved separately, against the double.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from conftest import APP_BASE_URL, error_code
from fulcrum_ops_api.models.identity import Role

Builder = Callable[[Any, Any, str], Awaitable[Any]]


@dataclasses.dataclass(frozen=True)
class Collection:
    """One list endpoint and how to put a row behind it."""

    name: str
    path: str
    build: Builder
    #: Detail route, when the domain has one. ``{id}`` is the row's id.
    detail: str | None = None
    #: Field the row's label lands in on the wire.
    label_field: str = "name"
    #: Whether this collection publishes a CSV export beside its list.
    export: bool = True


async def _agent(factory, workspace, label):
    return await factory.agent(workspace, name=label)


async def _connector(factory, workspace, label):
    return await factory.connector(workspace, name=label)


async def _policy(factory, workspace, label):
    return await factory.policy(workspace, name=label)


async def _approval(factory, workspace, label):
    return await factory.approval(workspace, action=label)


async def _secret(factory, workspace, label):
    return await factory.secret(workspace, name=label)


async def _configuration(factory, workspace, label):
    return await factory.configuration(workspace, name=label)


async def _knowledge(factory, workspace, label):
    return await factory.knowledge_source(workspace, name=label)


async def _memory(factory, workspace, label):
    return await factory.memory_store(workspace, name=label)


async def _quota(factory, workspace, label):
    return await factory.quota(workspace, name=label)


async def _budget(factory, workspace, label):
    return await factory.budget(workspace, name=label)


async def _environment(factory, workspace, label):
    return await factory.environment(workspace, name=label)


async def _deployment(factory, workspace, label):
    environment = await factory.environment(workspace, name=f"{label} environment")
    return await factory.deployment(workspace, environment, deployment_ref=label)


async def _guardrail(factory, workspace, label):
    return await factory.guardrail(workspace, name=label)


async def _guardrail_event(factory, workspace, label):
    guardrail = await factory.guardrail(workspace, name=label)
    return await factory.guardrail_event(workspace, guardrail)


async def _evaluation(factory, workspace, label):
    return await factory.evaluation_run(workspace, name=label)


async def _suite(factory, workspace, label):
    return await factory.test_suite(workspace, name=label)


async def _feedback(factory, workspace, label):
    return await factory.feedback_item(workspace, body=label, feedback_ref=label)


async def _alert(factory, workspace, label):
    return await factory.alert(workspace, title=label)


async def _alert_rule(factory, workspace, label):
    return await factory.alert_rule(workspace, name=label)


async def _member(factory, workspace, label):
    return await factory.user(
        workspace, email=f"{label.lower().replace(' ', '-')}@example.test", full_name=label
    )


async def _api_key(factory, workspace, label):
    _token, row = await factory.api_key(workspace, name=label)
    return row


COLLECTIONS: list[Collection] = [
    Collection("agents", "/api/v1/agents", _agent, "/api/v1/agents/{id}"),
    Collection("connectors", "/api/v1/connectors", _connector, "/api/v1/connectors/{id}"),
    Collection("policies", "/api/v1/policies", _policy, "/api/v1/policies/{id}"),
    Collection(
        "approvals", "/api/v1/approvals", _approval, "/api/v1/approvals/{id}", "action"
    ),
    Collection("secrets", "/api/v1/secrets", _secret, "/api/v1/secrets/{id}"),
    Collection(
        "configurations",
        "/api/v1/configurations",
        _configuration,
        "/api/v1/configurations/{id}",
    ),
    Collection("knowledge", "/api/v1/knowledge", _knowledge, "/api/v1/knowledge/{id}"),
    Collection("memory", "/api/v1/memory", _memory, "/api/v1/memory/{id}"),
    Collection("quota", "/api/v1/quota", _quota, "/api/v1/quota/{id}"),
    Collection(
        "budgets",
        "/api/v1/quota/budgets",
        _budget,
        "/api/v1/quota/budgets/{id}",
        export=False,
    ),
    Collection(
        "environments",
        "/api/v1/environments",
        _environment,
        "/api/v1/environments/{id}",
        export=False,
    ),
    Collection(
        "deployments",
        "/api/v1/deployments",
        _deployment,
        "/api/v1/deployments/{id}",
        "deployment_ref",
    ),
    Collection("guardrails", "/api/v1/guardrails", _guardrail, "/api/v1/guardrails/{id}"),
    Collection(
        "guardrail-events",
        "/api/v1/guardrails/events",
        _guardrail_event,
        None,
        "guardrail_name",
    ),
    Collection(
        "evaluations", "/api/v1/evaluations", _evaluation, "/api/v1/evaluations/{id}"
    ),
    Collection(
        "test-suites",
        "/api/v1/testing/suites",
        _suite,
        "/api/v1/testing/suites/{id}",
        export=False,
    ),
    Collection("feedback", "/api/v1/feedback", _feedback, "/api/v1/feedback/{id}", "body"),
    Collection("alerts", "/api/v1/alerts", _alert, "/api/v1/alerts/{id}", "title"),
    Collection(
        "alert-rules",
        "/api/v1/alerts/rules",
        _alert_rule,
        "/api/v1/alerts/rules/{id}",
        export=False,
    ),
    Collection(
        "members",
        "/api/v1/workspaces/users",
        _member,
        "/api/v1/workspaces/users/{id}",
        "full_name",
    ),
    Collection(
        "api-keys",
        "/api/v1/workspaces/api-keys",
        _api_key,
        "/api/v1/workspaces/api-keys/{id}",
    ),
]

#: Distinctive enough that a leak is unmistakable in a diff.
NORTHWIND_LABEL = "Northwind private record"
CONTOSO_LABEL = "Contoso private record"


@pytest.fixture(params=COLLECTIONS, ids=[c.name for c in COLLECTIONS])
def collection(request) -> Collection:
    return request.param


@pytest.fixture
async def contoso(app, factory, other_workspace) -> httpx.AsyncClient:
    """An admin of the second tenant, kept open for the whole test."""
    from conftest import authorise

    user = await factory.user(
        other_workspace, email="admin@contoso.test", full_name="Cass Admin", role=Role.ADMIN
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        authorise(http, user, other_workspace, Role.ADMIN)
        yield http


def labels(response: httpx.Response, collection: Collection) -> list[str]:
    return [str(item.get(collection.label_field)) for item in response.json()["items"]]


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


async def test_a_second_workspace_never_sees_the_first_workspaces_rows(
    admin_client, contoso, factory, workspace, other_workspace, collection
):
    before = await contoso.get(collection.path)
    assert before.status_code == 200, before.text
    baseline = before.json()["total"]

    row = await collection.build(factory, workspace, NORTHWIND_LABEL)

    after = await contoso.get(collection.path, params={"page_size": 200})
    ours = await admin_client.get(collection.path, params={"page_size": 200})

    assert after.status_code == 200, after.text
    assert after.json()["total"] == baseline, "the other tenant's total moved"
    assert NORTHWIND_LABEL not in labels(after, collection)
    assert row.id not in {item["id"] for item in after.json()["items"]}
    assert NORTHWIND_LABEL in labels(ours, collection), "we must still see our own"


async def test_each_tenant_sees_exactly_its_own_rows(
    admin_client, contoso, factory, workspace, other_workspace, collection
):
    await collection.build(factory, workspace, NORTHWIND_LABEL)
    await collection.build(factory, other_workspace, CONTOSO_LABEL)

    ours = await admin_client.get(collection.path, params={"page_size": 200})
    theirs = await contoso.get(collection.path, params={"page_size": 200})

    assert NORTHWIND_LABEL in labels(ours, collection)
    assert CONTOSO_LABEL not in labels(ours, collection)
    assert CONTOSO_LABEL in labels(theirs, collection)
    assert NORTHWIND_LABEL not in labels(theirs, collection)


async def test_a_search_cannot_reach_across_the_boundary(
    contoso, factory, workspace, collection
):
    """Search is a second query, and a missing tenant filter fails separately."""
    await collection.build(factory, workspace, NORTHWIND_LABEL)

    response = await contoso.get(collection.path, params={"q": "Northwind private"})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert NORTHWIND_LABEL not in response.text


async def test_an_export_cannot_reach_across_the_boundary(
    contoso, factory, workspace, collection
):
    """Export is a third query; several domains build it separately from the list."""
    if not collection.export:
        pytest.skip(f"{collection.name} publishes no CSV export")
    await collection.build(factory, workspace, NORTHWIND_LABEL)

    response = await contoso.get(f"{collection.path}/export")

    assert response.status_code == 200, response.text
    assert NORTHWIND_LABEL not in response.text


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------


async def test_another_tenants_row_is_not_found_rather_than_forbidden(
    contoso, factory, workspace, collection
):
    """403 would confirm the id exists; a missing row and a foreign one look alike."""
    if collection.detail is None:
        pytest.skip(f"{collection.name} has no detail route")
    row = await collection.build(factory, workspace, NORTHWIND_LABEL)

    response = await contoso.get(collection.detail.format(id=row.id))

    assert response.status_code == 404, response.text
    assert error_code(response) == "not_found"
    assert NORTHWIND_LABEL not in response.text


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def test_another_tenants_agent_cannot_be_deactivated_or_deleted(
    contoso, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Northwind Bot")

    deactivated = await contoso.post(f"/api/v1/agents/{agent.id}/deactivate", json={})
    deleted = await contoso.delete(f"/api/v1/agents/{agent.id}")
    patched = await contoso.patch(f"/api/v1/agents/{agent.id}", json={"team": "Theirs"})

    assert deactivated.status_code == 404
    assert deleted.status_code == 404
    assert patched.status_code == 404


async def test_another_tenants_credential_cannot_be_revealed(
    contoso, db, factory, workspace
):
    """The refusal must not even leave an access-log row on our credential."""
    from sqlalchemy import select

    from fulcrum_ops_api.models.governance import SecretAccessLog

    secret = await factory.secret(workspace, name="Northwind payments key")

    response = await contoso.post(
        f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "Curious"}
    )

    assert response.status_code == 404, response.text
    entries = await db.scalars(
        select(SecretAccessLog).where(SecretAccessLog.secret_id == secret.id)
    )
    assert entries == []


async def test_another_tenants_approval_cannot_be_decided(contoso, db, factory, workspace):
    from fulcrum_ops_api.models.governance import ApprovalRequest, ApprovalStatus

    request_row = await factory.approval(workspace, action="Northwind refund")

    response = await contoso.post(
        f"/api/v1/approvals/{request_row.id}/approve", json={"note": "Fine by me"}
    )

    assert response.status_code == 404, response.text
    stored = await db.get(ApprovalRequest, request_row.id)
    assert stored.status == ApprovalStatus.PENDING.value


async def test_another_tenants_configuration_cannot_be_rolled_back(
    contoso, factory, workspace
):
    configuration = await factory.configuration(workspace, name="Northwind router")
    response = await contoso.post(
        f"/api/v1/configurations/{configuration.id}/rollback", json={}
    )
    assert response.status_code == 404, response.text


async def test_a_row_cannot_be_created_into_another_tenant_by_naming_its_parent(
    contoso, factory, workspace
):
    """A foreign key from the body is still resolved inside the caller's tenant."""
    environment = await factory.environment(workspace, name="Northwind production")

    response = await contoso.post(
        "/api/v1/deployments",
        json={"environment_id": environment.id, "version": "v9.9.9"},
    )

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Collections that live in the telemetry engine
# ---------------------------------------------------------------------------


async def test_runs_are_scoped_by_the_projects_the_tenant_owns(
    admin_client, contoso, factory, workspace, other_workspace, engine
):
    ours = await factory.provisioned_agent(workspace, engine, name="Northwind Bot")
    theirs = await factory.provisioned_agent(other_workspace, engine, name="Contoso Bot")
    engine.add_trace(project_name=ours.engine_project_name, name="northwind run")
    engine.add_trace(project_name=theirs.engine_project_name, name="contoso run")

    mine = await admin_client.get("/api/v1/runs")
    yours = await contoso.get("/api/v1/runs")

    assert [row["agent"] for row in mine.json()["items"]] == ["Northwind Bot"]
    assert [row["agent"] for row in yours.json()["items"]] == ["Contoso Bot"]


async def test_another_tenants_run_cannot_be_addressed_by_id(
    contoso, factory, workspace, other_workspace, engine
):
    ours = await factory.provisioned_agent(workspace, engine, name="Northwind Bot")
    await factory.provisioned_agent(other_workspace, engine, name="Contoso Bot")
    trace = engine.add_trace(project_name=ours.engine_project_name, name="northwind run")

    for suffix in ("", "/trace", "/response", "/replay"):
        response = await contoso.get(f"/api/v1/runs/{trace['id']}{suffix}")
        assert response.status_code == 404, f"{suffix or 'detail'}: {response.text[:200]}"


async def test_prompts_are_scoped_by_the_namespace_the_tenant_owns(
    admin_client, contoso, engine
):
    ours = await admin_client.post(
        "/api/v1/prompts", json={"name": "Northwind system", "template": "Ours"}
    )
    theirs = await contoso.post(
        "/api/v1/prompts", json={"name": "Contoso system", "template": "Theirs"}
    )
    assert ours.status_code == 201, ours.text
    assert theirs.status_code == 201, theirs.text

    mine = await admin_client.get("/api/v1/prompts")
    yours = await contoso.get("/api/v1/prompts")

    assert [row["name"] for row in mine.json()["items"]] == ["Northwind system"]
    assert [row["name"] for row in yours.json()["items"]] == ["Contoso system"]


async def test_another_tenants_prompt_cannot_be_addressed_by_id(admin_client, contoso, engine):
    ours = await admin_client.post(
        "/api/v1/prompts", json={"name": "Northwind system", "template": "Ours"}
    )
    prompt_id = ours.json()["id"]

    fetched = await contoso.get(f"/api/v1/prompts/{prompt_id}")
    approved = await contoso.post(f"/api/v1/prompts/{prompt_id}/submit-review", json={})

    assert fetched.status_code == 404, fetched.text
    assert approved.status_code == 404, approved.text


async def test_two_tenants_may_use_the_same_prompt_name(admin_client, contoso, engine):
    """Names are unique inside a tenant, not across the estate."""
    ours = await admin_client.post(
        "/api/v1/prompts", json={"name": "System prompt", "template": "Ours"}
    )
    theirs = await contoso.post(
        "/api/v1/prompts", json={"name": "System prompt", "template": "Theirs"}
    )

    assert ours.status_code == 201, ours.text
    assert theirs.status_code == 201, theirs.text
    assert ours.json()["id"] != theirs.json()["id"]


# ---------------------------------------------------------------------------
# The other front door
# ---------------------------------------------------------------------------


async def test_an_api_key_sees_only_its_own_workspace(
    app, factory, workspace, other_workspace, engine
):
    await factory.agent(workspace, name="Northwind Bot")
    await factory.agent(other_workspace, name="Contoso Bot")
    token, _row = await factory.api_key(other_workspace, name="Contoso key")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        response = await http.get("/api/v1/agents")

    assert response.status_code == 200, response.text
    assert [row["name"] for row in response.json()["items"]] == ["Contoso Bot"]


async def test_an_api_key_cannot_be_pointed_at_another_workspace_by_header(
    app, factory, workspace, other_workspace
):
    """The header does not move a key, and the key's own workspace still wins.

    Note the divergence from the session path, which answers 403 for a workspace
    the caller does not belong to: for an API key the header is not consulted at
    all, so a client that believes it switched tenants is handed an empty page
    rather than an error. Safe, but quiet — recorded here so the behaviour is
    deliberate rather than accidental.
    """
    await factory.agent(workspace, name="Northwind Bot")
    token, _row = await factory.api_key(other_workspace, name="Contoso key")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        response = await http.get(
            "/api/v1/agents", headers={"X-Fulcrum-Workspace": workspace.slug}
        )

    assert "Northwind Bot" not in response.text
    assert response.json()["total"] == 0


async def test_an_ingest_key_cannot_report_into_another_tenants_agent(
    app, factory, workspace, other_workspace, engine
):
    """Agent names are resolved inside the key's workspace, never globally."""
    import datetime as dt

    await factory.provisioned_agent(workspace, engine, name="Northwind Bot")
    token, _row = await factory.api_key(
        other_workspace, name="Contoso key", scopes=["ingest", "read"]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        response = await http.post(
            "/api/v1/ingest/traces",
            json={
                "agent": "Northwind Bot",
                "traces": [
                    {"name": "forged run", "start_time": dt.datetime.now(dt.UTC).isoformat()}
                ],
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["code"] == "unknown_agent"
    assert engine.trace_count() == 0


async def test_the_summary_cards_are_scoped_too(
    admin_client, contoso, factory, workspace, other_workspace
):
    """A KPI count is an aggregate, and aggregates leak silently."""
    for index in range(3):
        await factory.agent(workspace, name=f"Northwind {index}")
    await factory.agent(other_workspace, name="Contoso only")

    ours = await admin_client.get("/api/v1/agents/summary")
    theirs = await contoso.get("/api/v1/agents/summary")

    assert ours.json()["total"] == 3
    assert theirs.json()["total"] == 1


# ---------------------------------------------------------------------------
# The sweep: every list endpoint, including the ones nobody remembered
# ---------------------------------------------------------------------------
#
# The table above names its collections by hand, which is what makes those
# tests readable — and also what makes them incomplete. There are more than
# fifty paginated list endpoints on this API and the table covers about twenty:
# the ones a person thought of. The rest are the sub-collections that grew
# beside a domain later — /feedback/issues, /policies/violations,
# /testing/schedules, /quota/events, /memory/backups — and they are exactly
# where a forgotten `where` clause survives review, because nobody is looking.
#
# So the sweep does not take its list of endpoints from a person. It reads the
# application's own OpenAPI document, keeps every GET that answers with the
# page envelope, and drives all of them. An endpoint added next quarter is
# covered the day it is added, without anybody remembering to come back here.
#
# The assertion is the strongest one available on a fresh tenant: a workspace
# that has created nothing must see *nothing*, on every collection, however
# crowded the workspace next door is. A missing tenant filter cannot satisfy
# that, whatever shape it takes.


def _page_list_endpoints() -> list[str]:
    """Every top-level GET on this API that answers with the page envelope.

    Read from the live OpenAPI document rather than a hand-kept list. Paths
    carrying an id are excluded: they need a row to address, which the tenant
    doing the looking does not have — the table-driven tests above cover those
    by building the row first.
    """
    from fulcrum_ops_api.main import create_app

    spec = create_app().openapi()
    schemas = spec["components"]["schemas"]
    envelope = {"items", "total", "page", "page_size", "pages"}

    def is_page(name: str) -> bool:
        return envelope <= set(schemas.get(name, {}).get("properties", {}))

    found: list[str] = []
    for path, operations in spec["paths"].items():
        operation = operations.get("get")
        if operation is None or "{" in path:
            continue
        try:
            schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        except KeyError:
            continue
        name = schema.get("$ref", "").rsplit("/", 1)[-1]
        if name and is_page(name):
            found.append(path)
    return sorted(found)


#: The plan catalogue is deliberately shared — see the test at the end of this
#: section, which pins both halves of that decision.
SHARED_BY_DESIGN = {"/api/v1/licensing/plans"}

PAGE_ENDPOINTS = [p for p in _page_list_endpoints() if p not in SHARED_BY_DESIGN]


@pytest.fixture
async def crowded(factory, workspace, engine, admin_client) -> str:
    """Fill Northwind with one of everything, all carrying the same label.

    Every row here is something the sweep might find on the other side of the
    boundary. The label is the tell: it is distinctive enough that finding it
    anywhere in Contoso's response body is unambiguous, even on an endpoint
    that reshapes its rows beyond recognition.
    """
    agent = await factory.provisioned_agent(workspace, engine, name=NORTHWIND_LABEL)
    engine.add_trace(
        project_name=agent.engine_project_name,
        name=NORTHWIND_LABEL,
        input={"question": NORTHWIND_LABEL},
    )
    environment = await factory.environment(workspace, name=NORTHWIND_LABEL)
    guardrail = await factory.guardrail(workspace, name=NORTHWIND_LABEL)
    doomed = await factory.secret(workspace, name="Swept key")

    await factory.connector(workspace, name=NORTHWIND_LABEL)
    await factory.policy(workspace, name=NORTHWIND_LABEL)
    await factory.approval(workspace, action=NORTHWIND_LABEL)
    await factory.secret(workspace, name=NORTHWIND_LABEL)
    await factory.configuration(workspace, name=NORTHWIND_LABEL)
    await factory.knowledge_source(workspace, name=NORTHWIND_LABEL)
    await factory.memory_store(workspace, name=NORTHWIND_LABEL)
    await factory.quota(workspace, name=NORTHWIND_LABEL)
    await factory.budget(workspace, name=NORTHWIND_LABEL)
    await factory.deployment(workspace, environment, deployment_ref=NORTHWIND_LABEL)
    await factory.guardrail_event(workspace, guardrail)
    await factory.evaluation_run(workspace, name=NORTHWIND_LABEL)
    await factory.test_suite(workspace, name=NORTHWIND_LABEL)
    await factory.feedback_item(workspace, body=NORTHWIND_LABEL, feedback_ref=NORTHWIND_LABEL)
    await factory.alert(workspace, title=NORTHWIND_LABEL)
    await factory.alert_rule(workspace, name=NORTHWIND_LABEL)
    await factory.license(workspace, entitlements={"ingest": True})
    await factory.user(workspace, email="swept@northwind.test", full_name=NORTHWIND_LABEL)
    await factory.api_key(workspace, name=NORTHWIND_LABEL)

    # Two more that only come into existence by going through the API.
    created = await admin_client.post(
        "/api/v1/prompts", json={"name": NORTHWIND_LABEL, "template": "Ours"}
    )
    assert created.status_code == 201, created.text
    disabled = await admin_client.post(f"/api/v1/secrets/{doomed.id}/disable", json={})
    assert disabled.status_code == 200, disabled.text  # and writes an audit row

    return NORTHWIND_LABEL


@pytest.fixture
async def contoso_owner(app, factory, other_workspace) -> httpx.AsyncClient:
    """The second tenant at full privilege.

    The sweep runs as an owner on purpose: a 403 would hide a leak behind a
    permission check, and would prove only that the endpoint is guarded — not
    that it is scoped. Every refusal below has to be about tenancy.
    """
    from conftest import authorise

    user = await factory.user(
        other_workspace,
        email="owner@contoso.test",
        full_name="Cass Owner",
        role=Role.OWNER,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        authorise(http, user, other_workspace, Role.OWNER)
        yield http


@pytest.mark.parametrize(
    "path", PAGE_ENDPOINTS, ids=[p.removeprefix("/api/v1/") for p in PAGE_ENDPOINTS]
)
async def test_a_fresh_tenant_sees_an_empty_page_on_every_collection(
    contoso_owner, crowded, path
):
    """Nothing created, nothing seen — on all of them, not just the famous ones."""
    response = await contoso_owner.get(path, params={"page_size": 200})

    assert response.status_code == 200, f"{path}: {response.text[:300]}"
    body = response.json()
    if path == "/api/v1/workspaces/users":
        # A fresh workspace is not empty of people: its own owner is a member.
        # The tenancy claim here is that the roster holds exactly that owner
        # and nobody from the crowded tenant.
        assert body["total"] == 1, f"{path} showed {body['total']} rows, expected the owner alone"
        assert [row["email"] for row in body["items"]] == ["owner@contoso.test"]
    else:
        assert body["total"] == 0, f"{path} showed {body['total']} of another tenant's rows"
        assert body["items"] == [], f"{path} listed rows it had counted as none"
    assert crowded not in response.text, f"{path} leaked the label into its body"


async def test_the_sweep_really_covers_more_than_the_table_does():
    """A guard on the guard: if discovery breaks, it must not break quietly.

    An empty or truncated endpoint list would make every case above vacuous —
    a row of passes that drove nothing. Pinning the floor means a refactor that
    breaks discovery fails here rather than silently deleting the sweep.
    """
    assert len(PAGE_ENDPOINTS) > len(COLLECTIONS), "discovery found fewer than the hand list"
    assert len(PAGE_ENDPOINTS) >= 40, f"only {len(PAGE_ENDPOINTS)} endpoints were discovered"
    for collection in COLLECTIONS:
        assert collection.path in PAGE_ENDPOINTS or collection.path in SHARED_BY_DESIGN, (
            f"{collection.path} is in the table but discovery missed it"
        )


async def test_the_plan_catalogue_is_shared_but_its_take_up_is_not(
    admin_client, contoso_owner, factory, workspace
):
    """The one deliberate exception, pinned so that it stays deliberate.

    Plans are product SKUs, not customer data: both tenants are meant to see
    the same catalogue. What must not cross is the annotation hanging off each
    row — how many licences *this* workspace holds against that plan, and how
    many seats it bought. That is commercial information about a named
    customer, and it travels in the same payload as the harmless part.
    """
    await factory.license(workspace, entitlements={"ingest": True})

    ours = await admin_client.get("/api/v1/licensing/plans")
    theirs = await contoso_owner.get("/api/v1/licensing/plans")

    assert ours.status_code == theirs.status_code == 200, theirs.text
    assert ours.json()["total"] == theirs.json()["total"] == 1, "one shared catalogue"

    mine = ours.json()["items"][0]
    yours = theirs.json()["items"][0]
    assert mine["id"] == yours["id"], "the same plan row, seen from both sides"

    assert mine["license_count"] == 1
    assert mine["seats_purchased"] == 25
    assert yours["license_count"] == 0, "Contoso holds no licence against this plan"
    assert yours["seats_purchased"] == 0, "Northwind's seat count is not Contoso's business"
