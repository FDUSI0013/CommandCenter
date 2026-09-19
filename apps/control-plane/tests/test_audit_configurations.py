"""Regressions from the September audit of the Configuration Center.

Each test pins one way the registry broke its own rules in production: a draft
that sat on the label the next rollback wanted and turned Roll Back into a 500,
drafts that nothing could ever make live, an Edit that put an unvalidated body
into Production, a rename that silently broke every configuration linking to the
old name, and a Usage tab that answered an engine outage with "An unexpected
error occurred".

Everything goes through HTTP as the console would send it, against the real
database schema and the engine double.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from conftest import error_code
from engine_double import EngineDouble
from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.models.registry import (
    Configuration,
    ConfigurationStatus,
    ConfigurationType,
    ConfigurationVersion,
    EnvironmentType,
)
from fulcrum_ops_api.services import configurations as configurations_service


def model_body(**overrides) -> dict:
    """A Model body that passes validation in Production."""
    body = {
        "provider": "azure-openai",
        "model": "gpt-4o",
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    body.update(overrides)
    return body


async def versions_of(http, configuration_id: str) -> list[dict]:
    response = await http.get(f"/api/v1/configurations/{configuration_id}/versions")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def audit_actions(db, workspace) -> list[str]:
    rows = await db.scalars(
        select(AuditEvent).where(AuditEvent.workspace_id == workspace.id)
    )
    return [row.action for row in rows]


async def draft(http, configuration_id: str, **body) -> dict:
    response = await http.post(
        f"/api/v1/configurations/{configuration_id}/versions",
        json={"activate": False, **body},
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


# ===========================================================================
# A draft holds a label; nothing else may assume that label is free
# ===========================================================================


async def test_rollback_still_works_after_someone_drafted_the_next_label(
    admin_client, db, factory, workspace
):
    """The draft took v1.2.0, which is exactly what Roll Back computed next."""
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    live = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"version": "v1.1.0", "payload": model_body(temperature=0.9)},
    )
    assert live.status_code == 201, live.text
    await draft(admin_client, configuration.id, version="v1.2.0", payload=model_body())

    rolled = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/rollback", json={"version": "v1.0.0"}
    )

    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"]["version"] == "v1.3.0", "the first label nobody holds"
    stored = await db.get(Configuration, configuration.id)
    assert stored.current_version == "v1.3.0"
    labels = sorted(row["version"] for row in await versions_of(admin_client, configuration.id))
    assert labels == ["v1.0.0", "v1.1.0", "v1.2.0", "v1.3.0"], "the draft is untouched"


async def test_an_unnamed_rollback_goes_to_what_was_live_not_to_a_draft(
    admin_client, factory, workspace
):
    """With no version named, Roll Back means "the body that ran before this one".
    A draft on file was the newest other row, so its body -- which had never been
    live -- is what an unnamed rollback used to publish."""
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    live = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"version": "v1.1.0", "payload": model_body(temperature=0.9)},
    )
    assert live.status_code == 201, live.text
    await draft(
        admin_client, configuration.id, version="v1.2.0", payload=model_body(temperature=1.7)
    )

    rolled = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/rollback", json={}
    )

    assert rolled.status_code == 200, rolled.text
    body = await admin_client.get(
        f"/api/v1/configurations/{configuration.id}/versions/{rolled.json()['version']['version']}"
    )
    assert body.json()["payload"]["temperature"] == 0.2, "v1.0.0's body, not the draft's"


async def test_a_version_cut_without_a_label_steps_over_a_draft(
    admin_client, factory, workspace
):
    """A caller who never chose a label cannot be asked to resolve a 409 about one."""
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    first = await draft(admin_client, configuration.id, payload=model_body(temperature=0.5))
    assert first["version"] == "v1.1.0"

    second = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": model_body(temperature=0.7)},
    )

    assert second.status_code == 201, second.text
    assert second.json()["version"]["version"] == "v1.2.0"


async def test_importing_the_same_bundle_twice_as_new_versions_is_not_a_500(
    admin_client, db, factory, workspace
):
    """Import, 'Load this workspace's bundle', Import -- and then again."""
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    bundle = (await admin_client.get("/api/v1/configurations/bundle")).json()
    request = {"items": bundle["items"], "on_conflict": "new_version", "source": "bundle.json"}

    first = await admin_client.post("/api/v1/configurations/import", json=request)
    second = await admin_client.post("/api/v1/configurations/import", json=request)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert (first.json()["versioned"], second.json()["versioned"]) == (1, 1)
    rows = await versions_of(admin_client, configuration.id)
    assert sorted(row["version"] for row in rows) == ["v1.0.0", "v1.1.0", "v1.2.0"]
    assert [row["version"] for row in rows if row["is_current"]] == ["v1.0.0"]

    # ...and the drafts the imports left behind do not break Roll Back either.
    live = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions/v1.1.0/activate"
    )
    assert live.status_code == 200, live.text
    rolled = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/rollback", json={"version": "v1.0.0"}
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"]["version"] == "v1.3.0"


async def test_a_bundle_that_names_one_configuration_twice_versions_it_twice(
    admin_client, factory, workspace
):
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    item = {"name": "Router settings", "config_type": "Model", "payload": model_body()}

    response = await admin_client.post(
        "/api/v1/configurations/import",
        json={
            "items": [item, {**item, "payload": model_body(temperature=0.4)}],
            "on_conflict": "new_version",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["versioned"] == 2
    labels = sorted(row["version"] for row in await versions_of(admin_client, configuration.id))
    assert labels == ["v1.0.0", "v1.1.0", "v1.2.0"]


# ===========================================================================
# A draft can go live
# ===========================================================================


async def test_a_drafted_version_can_be_activated(admin_client, db, factory, workspace):
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    drafted = await draft(
        admin_client, configuration.id, payload=model_body(temperature=0.9)
    )

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions/{drafted['version']}/activate"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"]["is_current"] is True
    assert body["version"]["status"] == ConfigurationStatus.ACTIVE.value
    assert body["version"]["published_at"] is not None
    assert body["configuration"]["current_version"] == drafted["version"]
    assert body["validation"]["valid"] is True

    rows = await versions_of(admin_client, configuration.id)
    assert [row["version"] for row in rows if row["is_current"]] == [drafted["version"]]
    demoted = next(row for row in rows if row["version"] == "v1.0.0")
    assert demoted["status"] == ConfigurationStatus.DEPRECATED.value
    assert "configuration.version_activated" in await audit_actions(db, workspace)


async def test_an_imported_configuration_goes_live_through_activate(admin_client, db):
    """Imports land as Draft on purpose; this is the way out of Draft."""
    imported = await admin_client.post(
        "/api/v1/configurations/import",
        json={
            "items": [
                {
                    "name": "Imported router",
                    "config_type": "Model",
                    "environment": "Production",
                    "version": "v2.0.0",
                    "payload": model_body(),
                }
            ]
        },
    )
    assert imported.status_code == 200, imported.text
    configuration_id = imported.json()["configuration_ids"][0]

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration_id}/versions/v2.0.0/activate"
    )

    assert response.status_code == 200, response.text
    stored = await db.get(Configuration, configuration_id)
    assert stored.status == ConfigurationStatus.ACTIVE.value
    assert stored.current_version == "v2.0.0"


async def test_a_draft_that_fails_validation_is_not_activated(
    admin_client, db, factory, workspace
):
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    drafted = await draft(admin_client, configuration.id, payload={"temperature": 0.7})

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions/{drafted['version']}/activate"
    )

    assert response.status_code == 422, response.text
    fields = {finding["field"] for finding in response.json()["error"]["details"]["findings"]}
    assert fields >= {"provider", "model"}
    stored = await db.get(Configuration, configuration.id)
    assert stored.current_version == "v1.0.0", "nothing was published"


async def test_only_a_draft_can_be_activated(admin_client, factory, workspace):
    """What was live before comes back through Roll Back, as a new revision."""
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    bumped = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": model_body(temperature=0.9)},
    )
    assert bumped.status_code == 201, bumped.text
    live = bumped.json()["version"]["version"]

    already = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions/{live}/activate"
    )
    earlier = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions/v1.0.0/activate"
    )
    missing = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions/v9.9.9/activate"
    )
    nonsense = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions/banana/activate"
    )

    assert already.status_code == 412, already.text
    assert "already current" in already.json()["error"]["message"]
    assert earlier.status_code == 412, earlier.text
    assert "Roll back" in earlier.json()["error"]["message"]
    assert missing.status_code == 404, missing.text
    assert nonsense.status_code == 404, nonsense.text

    # A label that cannot exist is a 404 on the reads as well; it was a 500.
    read = await admin_client.get(f"/api/v1/configurations/{configuration.id}/versions/banana")
    diffed = await admin_client.get(
        f"/api/v1/configurations/{configuration.id}/versions/diff?from=banana&to=v1.0.0"
    )
    assert (read.status_code, diffed.status_code) == (404, 404)


async def test_activation_is_refused_on_an_archived_configuration_and_below_operator(
    admin_client, as_role, factory, workspace
):
    archived = await factory.configuration(
        workspace,
        name="Retired router",
        status=ConfigurationStatus.ARCHIVED.value,
        payload=model_body(),
    )
    await factory.configuration_version(
        workspace,
        archived,
        version="v1.1.0",
        payload=model_body(),
        status=ConfigurationStatus.DRAFT.value,
    )
    live = await factory.configuration(workspace, name="Router settings", payload=model_body())
    drafted = await draft(admin_client, live.id, payload=model_body(temperature=0.9))

    refused = await admin_client.post(
        f"/api/v1/configurations/{archived.id}/versions/v1.1.0/activate"
    )
    async with as_role(Role.MEMBER) as member_http:
        forbidden = await member_http.post(
            f"/api/v1/configurations/{live.id}/versions/{drafted['version']}/activate"
        )

    assert refused.status_code == 412, refused.text
    assert "archived" in refused.json()["error"]["message"].lower()
    assert forbidden.status_code == 403, forbidden.text


# ===========================================================================
# Two revisions flagged current
# ===========================================================================


async def two_current_revisions(admin_client, db, factory, workspace) -> Configuration:
    """The state two racing activations leave behind: both rows carry the flag."""
    configuration = await factory.configuration(
        workspace, name="Router settings", current_version="v1.0.0", payload=model_body()
    )
    bumped = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"version": "v1.1.0", "payload": model_body(temperature=0.9)},
    )
    assert bumped.status_code == 201, bumped.text
    await db.execute(
        update(ConfigurationVersion)
        .where(
            ConfigurationVersion.configuration_id == configuration.id,
            ConfigurationVersion.version == "v1.0.0",
        )
        .values(is_current=True, status=ConfigurationStatus.ACTIVE.value)
    )
    return configuration


async def test_a_configuration_with_two_current_revisions_still_answers(
    admin_client, db, factory, workspace
):
    """Every verb asked for 'the' current row and raised on finding two: a 500
    from Usage, Validate, Clone, New Version, Roll Back, Deprecate and Restore,
    until somebody repaired the rows with SQL."""
    configuration = await two_current_revisions(admin_client, db, factory, workspace)

    validated = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/validate", json={}
    )
    used = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")
    cloned = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/clone", json={}
    )

    assert validated.status_code == 200, validated.text
    assert validated.json()["version"] == "v1.1.0", "the latest published one is the live one"
    assert used.status_code == 200, used.text
    assert cloned.status_code == 201, cloned.text


async def test_the_next_activation_repairs_a_configuration_with_two_current_revisions(
    admin_client, db, factory, workspace
):
    configuration = await two_current_revisions(admin_client, db, factory, workspace)

    response = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions",
        json={"payload": model_body(temperature=0.5)},
    )

    assert response.status_code == 201, response.text
    live = response.json()["version"]["version"]
    rows = await versions_of(admin_client, configuration.id)
    assert [row["version"] for row in rows if row["is_current"]] == [live]
    assert {row["status"] for row in rows if not row["is_current"]} == {
        ConfigurationStatus.DEPRECATED.value
    }


async def test_the_verbs_that_decide_what_is_current_take_a_row_lock(workspace, admin):
    """SQLite has one writer and renders no lock clause, so the race this closes
    cannot be run here; what can be checked is what Postgres will be sent."""
    from sqlalchemy.dialects import postgresql

    from conftest import principal_for

    principal = principal_for(workspace, admin, Role.ADMIN)
    locked = configurations_service._configuration_stmt(principal, "cfg", lock=True)
    plain = configurations_service._configuration_stmt(principal, "cfg")

    assert "FOR NO KEY UPDATE" in str(locked.compile(dialect=postgresql.dialect()))
    assert "FOR" not in str(plain.compile(dialect=postgresql.dialect())).split("WHERE")[1]


# ===========================================================================
# Edit is not a way around validation
# ===========================================================================

#: Passes in Development; Production also wants temperature and max_tokens.
DEVELOPMENT_ONLY_BODY = {"provider": "azure-openai", "model": "gpt-4o"}


async def test_an_active_configuration_cannot_be_edited_into_production_unvalidated(
    admin_client, db, factory, workspace
):
    configuration = await factory.configuration(
        workspace,
        name="Router settings",
        environment=EnvironmentType.DEVELOPMENT.value,
        payload=DEVELOPMENT_ONLY_BODY,
    )

    # Exactly what the Edit dialog sends: all three identity fields, every time.
    response = await admin_client.patch(
        f"/api/v1/configurations/{configuration.id}",
        json={"name": "Router settings", "config_type": "Model", "environment": "Production"},
    )

    assert response.status_code == 422, response.text
    findings = response.json()["error"]["details"]["findings"]
    assert {(f["field"], f["code"]) for f in findings} == {
        ("temperature", "required_in_production"),
        ("max_tokens", "required_in_production"),
    }
    stored = await db.get(Configuration, configuration.id)
    assert stored.environment == EnvironmentType.DEVELOPMENT.value, "nothing was written"


async def test_a_body_that_passes_the_new_rules_may_move_and_a_draft_always_may(
    admin_client, factory, workspace
):
    ready = await factory.configuration(
        workspace,
        name="Ready for production",
        environment=EnvironmentType.DEVELOPMENT.value,
        payload=model_body(),
    )
    rough = await factory.configuration(
        workspace,
        name="Rough draft",
        environment=EnvironmentType.DEVELOPMENT.value,
        status=ConfigurationStatus.DRAFT.value,
        payload=DEVELOPMENT_ONLY_BODY,
    )

    moved = await admin_client.patch(
        f"/api/v1/configurations/{ready.id}", json={"environment": "Production"}
    )
    drafted = await admin_client.patch(
        f"/api/v1/configurations/{rough.id}", json={"environment": "Production"}
    )

    assert moved.status_code == 200, moved.text
    assert moved.json()["environment"] == "Production"
    assert drafted.status_code == 200, drafted.text


async def test_a_row_that_is_already_invalid_can_still_be_edited(
    admin_client, factory, workspace
):
    """The dialog resends type and environment unchanged; that is not a move. And
    an edit that introduces no new error is not refused for the old ones."""
    configuration = await factory.configuration(
        workspace,
        name="Router settings",
        environment=EnvironmentType.PRODUCTION.value,
        payload=DEVELOPMENT_ONLY_BODY,
    )

    described = await admin_client.patch(
        f"/api/v1/configurations/{configuration.id}",
        json={
            "name": "Router settings",
            "config_type": "Model",
            "environment": "Production",
            "description": "Owned by the platform team",
        },
    )
    corrected = await admin_client.patch(
        f"/api/v1/configurations/{configuration.id}", json={"environment": "Development"}
    )

    assert described.status_code == 200, described.text
    assert described.json()["description"] == "Owned by the platform team"
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["environment"] == "Development"


async def test_a_type_change_is_held_to_the_new_types_rules(admin_client, factory, workspace):
    configuration = await factory.configuration(
        workspace, name="Router settings", payload=model_body()
    )

    response = await admin_client.patch(
        f"/api/v1/configurations/{configuration.id}", json={"config_type": "Prompt"}
    )

    assert response.status_code == 422, response.text
    findings = response.json()["error"]["details"]["findings"]
    assert ("template", "required_missing") in {(f["field"], f["code"]) for f in findings}


async def test_a_configuration_others_link_to_cannot_be_renamed_out_from_under_them(
    admin_client, db, factory, workspace
):
    guard = await factory.configuration(
        workspace,
        name="PII Guard",
        config_type=ConfigurationType.GUARDRAIL.value,
        payload={"rule_type": "PII", "action": "Redact", "threshold": 0.8},
    )
    await factory.configuration(
        workspace,
        name="Support model",
        payload=model_body(links={"guardrail": "PII Guard"}),
    )
    await factory.configuration(
        workspace,
        name="Retired model",
        status=ConfigurationStatus.ARCHIVED.value,
        payload=model_body(links={"guardrail": ["PII Guard"]}),
    )

    response = await admin_client.patch(
        f"/api/v1/configurations/{guard.id}", json={"name": "PII Shield"}
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "conflict"
    dependents = response.json()["error"]["details"]["dependents"]
    assert {entry["name"] for entry in dependents} == {"Support model", "Retired model"}
    assert {entry["slot"] for entry in dependents} == {"guardrail"}
    stored = await db.get(Configuration, guard.id)
    assert stored.name == "PII Guard"


async def test_a_configuration_nothing_links_to_is_renamed_as_before(
    admin_client, factory, workspace
):
    guard = await factory.configuration(
        workspace,
        name="PII Guard",
        config_type=ConfigurationType.GUARDRAIL.value,
        payload={"rule_type": "PII", "action": "Redact", "threshold": 0.8},
    )
    await factory.configuration(
        workspace, name="Support model", payload=model_body(links={"guardrail": "Other"})
    )

    response = await admin_client.patch(
        f"/api/v1/configurations/{guard.id}", json={"name": "PII Shield"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "PII Shield"


async def test_an_archived_configuration_restore_refuses_still_comes_back_by_rollback(
    admin_client, db, factory, workspace
):
    """Archived, with a live body whose link target has gone: restore answers 422
    and Edit and New Version answer "restore it first". Roll Back to a body that
    passes is the one way back, so it must not grow the Archived guard the other
    verbs have."""
    configuration = await factory.configuration(
        workspace,
        name="Retired router",
        status=ConfigurationStatus.ARCHIVED.value,
        current_version="v1.1.0",
        payload=model_body(links={"guardrail": "A guard that was deleted"}),
    )
    await factory.configuration_version(
        workspace,
        configuration,
        version="v1.0.0",
        payload=model_body(),
        status=ConfigurationStatus.DEPRECATED.value,
    )

    restored = await admin_client.post(f"/api/v1/configurations/{configuration.id}/restore")
    versioned = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/versions", json={"payload": model_body()}
    )
    rolled = await admin_client.post(
        f"/api/v1/configurations/{configuration.id}/rollback", json={"version": "v1.0.0"}
    )

    assert restored.status_code == 422, restored.text
    assert versioned.status_code == 412, versioned.text
    assert rolled.status_code == 200, rolled.text
    stored = await db.get(Configuration, configuration.id)
    assert stored.status == ConfigurationStatus.ACTIVE.value
    assert stored.current_version == "v1.2.0"


async def test_a_null_identity_field_is_no_change_not_a_name_conflict(
    admin_client, factory, workspace
):
    configuration = await factory.configuration(
        workspace, name="Router settings", payload=model_body()
    )

    response = await admin_client.patch(
        f"/api/v1/configurations/{configuration.id}",
        json={"environment": None, "description": "Still here"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["environment"] == "Production"
    assert response.json()["description"] == "Still here"


# ===========================================================================
# The Usage tab and the telemetry store
# ===========================================================================


class BusyEngine(EngineDouble):
    """The double, able to lose a project and to take its time over run stats.

    Nothing about its answers changes otherwise. ``stats_peak`` is how many
    trace-stats reads were ever on the store at the same moment.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gone_projects: set[str] = set()
        self.stats_delay = 0.0
        self.stats_in_flight = 0
        self.stats_peak = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].endswith("/traces/stats"):
            await super().__call__(scope, receive, send)
            return
        self.stats_in_flight += 1
        self.stats_peak = max(self.stats_peak, self.stats_in_flight)
        try:
            if self.stats_delay:
                await asyncio.sleep(self.stats_delay)
            await super().__call__(scope, receive, send)
        finally:
            self.stats_in_flight -= 1

    def _dispatch(self, method, path, query, body):
        named = (query.get("project_name") or [""])[0]
        if path.endswith("/traces/stats") and named in self.gone_projects:
            return 404, {"errors": [f"project {named} not found"]}, {}
        return super()._dispatch(method, path, query, body)


@pytest.fixture
def engine() -> BusyEngine:
    return BusyEngine()


async def bound_fleet(factory, workspace, engine, *, agents: int = 2, runs_each: int = 2):
    """A Model configuration and the provisioned agents that serve its model."""
    configuration = await factory.configuration(
        workspace, name="gpt-4o", payload=model_body()
    )
    fleet = []
    for index in range(agents):
        agent = await factory.provisioned_agent(
            workspace, engine, name=f"Support Bot {index}", model="gpt-4o"
        )
        for _ in range(runs_each):
            engine.add_trace(project_name=agent.engine_project_name, name="answer")
        fleet.append(agent)
    return configuration, fleet


async def test_usage_answers_an_engine_outage_with_the_typed_503(
    admin_client, factory, workspace, engine
):
    """It was "An unexpected error occurred": the adapter's error is not an AppError."""
    configuration, _fleet = await bound_fleet(factory, workspace, engine)

    healthy = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["runs_30d"] == 4, "the endpoint really does read telemetry"
    assert healthy.json()["telemetry_available"] is True

    engine.fail(503)
    down = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")

    assert down.status_code == 503, down.text
    assert error_code(down) == "telemetry_unavailable"
    assert down.headers.get("Retry-After")

    engine.recover()
    back = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")
    assert back.status_code == 200, back.text


async def test_usage_reports_a_refusal_by_the_store_as_a_refusal(
    admin_client, factory, workspace, engine
):
    configuration, _fleet = await bound_fleet(factory, workspace, engine)
    engine.fail(400)

    response = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_rejected"


async def test_usage_survives_a_project_that_was_deleted_upstream(
    admin_client, factory, workspace, engine
):
    """One bound agent whose project is gone used to cost the whole panel."""
    configuration, fleet = await bound_fleet(factory, workspace, engine)
    engine.gone_projects.add(fleet[0].engine_project_name)

    partial = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")

    assert partial.status_code == 200, partial.text
    assert partial.json()["used_by_agents"] == 2
    assert partial.json()["runs_30d"] == 2, "only the project that still exists is counted"

    engine.gone_projects.add(fleet[1].engine_project_name)
    nothing = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")

    assert nothing.status_code == 200, nothing.text
    assert nothing.json()["runs_30d"] is None, "no project answered: not measured, not zero"
    assert nothing.json()["telemetry_available"] is False


async def test_usage_does_not_put_one_read_per_bound_agent_on_the_store_at_once(
    admin_client, factory, workspace, engine
):
    configuration, fleet = await bound_fleet(
        factory, workspace, engine, agents=configurations_service.USAGE_ENGINE_CONCURRENCY * 3
    )
    engine.stats_delay = 0.02

    response = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")

    assert response.status_code == 200, response.text
    assert response.json()["runs_30d"] == 2 * len(fleet), "every project was still read"
    assert 1 < engine.stats_peak <= configurations_service.USAGE_ENGINE_CONCURRENCY, (
        "side by side, but never the whole fleet at once"
    )


async def test_usage_gives_up_inside_the_deadline_instead_of_outwaiting_the_console(
    admin_client, factory, workspace, engine, monkeypatch
):
    configuration, _fleet = await bound_fleet(factory, workspace, engine)
    engine.stats_delay = 0.5
    monkeypatch.setattr(settings, "engine_fanout_deadline_seconds", 0.05)

    response = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"


async def test_usage_remembers_a_projects_counters_between_clicks(
    admin_client, factory, workspace, engine, monkeypatch
):
    """Every click on the tab repeated a 30-day aggregate per bound agent."""
    monkeypatch.setattr(settings, "configuration_usage_cache_seconds", 60.0)
    configuration, fleet = await bound_fleet(factory, workspace, engine)
    try:
        first = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")
        second = await admin_client.get(f"/api/v1/configurations/{configuration.id}/usage")
    finally:
        configurations_service._usage_memory.invalidate()

    assert first.status_code == second.status_code == 200
    assert first.json()["runs_30d"] == second.json()["runs_30d"] == 4
    assert len(engine.calls_to("/traces/stats")) == len(fleet), "one read per project, once"


async def test_usage_holds_no_database_connection_while_it_waits_on_the_store(
    admin_client, db_engine, factory, workspace, engine
):
    """Held across a slow aggregate, a few open Usage tabs drain a worker's pool."""
    from sqlalchemy import event

    configuration, _fleet = await bound_fleet(factory, workspace, engine)
    held = 0
    held_during_engine_reads: list[int] = []

    def taken(*_args: object) -> None:
        nonlocal held
        held += 1

    def given_back(*_args: object) -> None:
        nonlocal held
        held -= 1

    async def note(_method: str, path: str) -> None:
        if path.endswith("/traces/stats"):
            held_during_engine_reads.append(held)

    engine.before_dispatch = note
    pool = db_engine.sync_engine.pool
    event.listen(pool, "checkout", taken)
    event.listen(pool, "checkin", given_back)
    try:
        response = await admin_client.get(
            f"/api/v1/configurations/{configuration.id}/usage"
        )
    finally:
        event.remove(pool, "checkout", taken)
        event.remove(pool, "checkin", given_back)

    assert response.status_code == 200, response.text
    assert held_during_engine_reads, "the store was asked"
    assert set(held_during_engine_reads) == {0}
