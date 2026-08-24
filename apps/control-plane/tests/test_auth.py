"""Who may call, as whom, in which tenant.

The API has two front doors — a browser session and an API key — and one rule
behind them: a caller sees exactly their own workspace, at exactly their own
role. These tests exercise both doors, the lockout that protects the first, the
scope system that narrows the second, and the tenancy boundary that neither may
cross.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import delete, select, update

from conftest import APP_BASE_URL, authorise, error_code, session_token
from factories import PASSWORD
from fulcrum_ops_api.api.deps import SESSION_COOKIE
from fulcrum_ops_api.core import security
from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.identity import ApiKey, Membership, Role, User
from fulcrum_ops_api.services.identity import MAX_FAILED_LOGINS, SIGN_IN_FAILED

# ---------------------------------------------------------------------------
# Sign in
# ---------------------------------------------------------------------------


@pytest.fixture
async def signed_up(factory, workspace):
    """A user who really has a password, so the whole login path can run."""
    return await factory.user(
        workspace,
        email="pilot@northwind.test",
        full_name="Pat Pilot",
        role=Role.ADMIN,
        password=PASSWORD,
    )


async def test_login_issues_a_session_cookie_and_the_shell_envelope(
    client, signed_up, workspace
):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": signed_up.email, "password": PASSWORD},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == signed_up.email
    assert body["api_key"] is None
    assert body["workspace"]["slug"] == workspace.slug
    assert body["role"] == Role.ADMIN.value
    assert [option["slug"] for option in body["workspaces"]] == [workspace.slug]

    cookie = response.cookies.get(SESSION_COOKIE)
    assert cookie, "sign-in must install the session cookie"
    claims = security.decode_session_token(cookie)
    assert claims["sub"] == signed_up.id
    assert claims["ws"] == workspace.id


async def test_the_session_cookie_alone_authenticates_the_next_request(client, signed_up):
    await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": PASSWORD}
    )
    # The cookie jar carries it; no Authorization header is set.
    assert "authorization" not in client.headers

    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == signed_up.email


async def test_the_session_cookie_is_httponly_and_scoped_to_the_whole_site(client, signed_up):
    response = await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": PASSWORD}
    )
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("pilot@northwind.test", "the wrong password entirely"),
        ("nobody@northwind.test", PASSWORD),
    ],
    ids=["wrong-password", "unknown-address"],
)
async def test_every_sign_in_failure_answers_identically(client, signed_up, email, password):
    """A wrong password and an unknown address must be indistinguishable."""
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "unauthenticated",
        "message": SIGN_IN_FAILED,
        "request_id": response.headers["x-request-id"],
    }


async def test_a_deactivated_account_is_refused_with_the_same_answer(client, factory, workspace):
    user = await factory.user(
        workspace, email="gone@northwind.test", password=PASSWORD, is_active=False
    )
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == SIGN_IN_FAILED


async def test_an_account_with_no_workspace_is_refused_with_the_same_answer(client, factory):
    user = await factory.user(None, email="orphan@northwind.test", password=PASSWORD)
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == SIGN_IN_FAILED


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


async def test_repeated_failures_lock_the_account(client, db, signed_up):
    for attempt in range(MAX_FAILED_LOGINS):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": signed_up.email, "password": f"guess-{attempt}"},
        )
        assert response.status_code == 401

    user = await db.get(User, signed_up.id)
    assert user.locked_until is not None
    assert user.locked_until > dt.datetime.now(dt.UTC)
    # The counter resets when the lock is applied; the lock is now the state.
    assert user.failed_login_count == 0


async def test_the_correct_password_on_a_locked_account_says_so(client, signed_up):
    """The caller already holds the secret, so naming the lock reveals nothing."""
    for attempt in range(MAX_FAILED_LOGINS):
        await client.post(
            "/api/v1/auth/login",
            json={"email": signed_up.email, "password": f"guess-{attempt}"},
        )

    response = await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": PASSWORD}
    )
    assert response.status_code == 401
    assert error_code(response) == "account_locked"
    assert "locked" in response.json()["error"]["message"].lower()


async def test_a_wrong_password_on_a_locked_account_still_says_nothing(client, signed_up):
    for attempt in range(MAX_FAILED_LOGINS):
        await client.post(
            "/api/v1/auth/login",
            json={"email": signed_up.email, "password": f"guess-{attempt}"},
        )

    response = await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": "still wrong"}
    )
    assert error_code(response) == "unauthenticated"
    assert response.json()["error"]["message"] == SIGN_IN_FAILED


async def test_the_lockout_counter_survives_the_rejected_request(client, db, signed_up):
    """The failure path commits before it raises; a counter that rolls back is not one."""
    await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": "nope"}
    )
    user = await db.get(User, signed_up.id)
    assert user.failed_login_count == 1


async def test_a_successful_sign_in_clears_the_failure_count(client, db, signed_up):
    await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": "nope"}
    )
    await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": PASSWORD}
    )
    user = await db.get(User, signed_up.id)
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert user.last_login_at is not None


async def test_a_lockout_is_written_to_the_audit_trail(client, db, signed_up, workspace):
    for attempt in range(MAX_FAILED_LOGINS):
        await client.post(
            "/api/v1/auth/login",
            json={"email": signed_up.email, "password": f"guess-{attempt}"},
        )

    actions = await db.scalars(
        select(AuditEvent.action).where(AuditEvent.workspace_id == workspace.id)
    )
    assert actions.count("auth.login_failed") == MAX_FAILED_LOGINS - 1
    assert actions.count("auth.login_locked") == 1


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def test_the_cheap_probe_answers_204_then_401(client, admin_client):
    assert (await admin_client.get("/api/v1/auth/status")).status_code == 204
    assert (await client.get("/api/v1/auth/status")).status_code == 401


async def test_an_expired_session_is_refused(app, admin, workspace):
    stale = security.issue_session_token(
        user_id=admin.id, workspace_id=workspace.id, role=Role.ADMIN.value, ttl_minutes=-1
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {stale}"
        response = await http.get("/api/v1/auth/session")
    assert response.status_code == 401
    assert error_code(response) == "unauthenticated"


async def test_a_tampered_session_signature_is_refused(client, admin, workspace):
    token = session_token(admin, workspace, Role.ADMIN)
    head, payload, _signature = token.split(".")
    client.headers["Authorization"] = f"Bearer {head}.{payload}.AAAA"
    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 401


async def test_logout_clears_the_cookie_and_is_audited(client, db, signed_up, workspace):
    await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": PASSWORD}
    )
    response = await client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert not client.cookies.get(SESSION_COOKIE)

    actions = await db.scalars(
        select(AuditEvent.action).where(AuditEvent.workspace_id == workspace.id)
    )
    assert "auth.logout" in actions


async def test_logout_succeeds_for_an_anonymous_caller(client):
    """A stuck client must be able to perform the one action it needs."""
    response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 200


async def test_changing_a_password_requires_the_current_one(client, signed_up):
    await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": PASSWORD}
    )
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "not it", "new_password": "a fresh passphrase 42"},
    )
    assert response.status_code == 422
    assert error_code(response) == "validation_failed"


async def test_changing_a_password_reissues_the_session(client, signed_up):
    await client.post(
        "/api/v1/auth/login", json={"email": signed_up.email, "password": PASSWORD}
    )
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "a fresh passphrase 42"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["entity_id"] == signed_up.id
    assert (await client.get("/api/v1/auth/status")).status_code == 204


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


async def test_an_api_key_authenticates_and_identifies_itself(client, ingest_key, workspace):
    token, row = ingest_key
    client.headers["Authorization"] = f"Bearer {token}"

    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"] is None
    assert body["api_key"]["id"] == row.id
    assert body["api_key"]["scopes"] == ["ingest", "read"]
    assert body["workspace"]["slug"] == workspace.slug


async def test_an_api_key_is_accepted_on_its_own_header(client, ingest_key):
    token, _row = ingest_key
    response = await client.get("/api/v1/auth/session", headers={"X-Fulcrum-Api-Key": token})
    assert response.status_code == 200, response.text


async def test_using_a_key_records_when_and_from_where(client, db, ingest_key):
    token, row = ingest_key
    client.headers["Authorization"] = f"Bearer {token}"
    await client.get("/api/v1/auth/session")

    key = await db.get(ApiKey, row.id)
    assert key.last_used_at is not None


async def test_a_revoked_key_is_refused(client, factory, workspace):
    token, _row = await factory.api_key(
        workspace, name="Retired", revoked_at=dt.datetime.now(dt.UTC)
    )
    client.headers["Authorization"] = f"Bearer {token}"
    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 401
    assert error_code(response) == "unauthenticated"


async def test_an_expired_key_is_refused(client, factory, workspace):
    token, _row = await factory.api_key(
        workspace,
        name="Lapsed",
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
    )
    client.headers["Authorization"] = f"Bearer {token}"
    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 401


async def test_a_key_with_the_right_id_and_the_wrong_secret_is_refused(
    client, factory, workspace
):
    token, _row = await factory.api_key(workspace, name="Guessable")
    prefix, env, key_id, _secret = token.split("_")
    forged = "_".join([prefix, env, key_id, "a" * 52])
    client.headers["Authorization"] = f"Bearer {forged}"
    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 401


async def test_a_key_belonging_to_a_suspended_workspace_is_refused(client, factory):
    frozen = await factory.workspace(name="Frozen", slug="frozen", status="suspended")
    token, _row = await factory.api_key(frozen, name="Frozen key")
    client.headers["Authorization"] = f"Bearer {token}"
    response = await client.get("/api/v1/auth/session")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


async def test_a_key_without_the_ingest_scope_may_not_report_telemetry(
    client, factory, workspace
):
    token, _row = await factory.api_key(workspace, name="Read only", scopes=["read"])
    client.headers["Authorization"] = f"Bearer {token}"

    response = await client.post(
        "/api/v1/ingest/traces", json={"traces": [{"name": "run", "start_time": "2026-01-01T00:00:00Z"}]}
    )
    assert response.status_code == 403
    assert error_code(response) == "permission_denied"
    assert "ingest" in response.json()["error"]["message"]


async def test_the_admin_scope_implies_every_other_scope(client, factory, workspace, engine):
    token, _row = await factory.api_key(workspace, name="Everything", scopes=["admin"])
    client.headers["Authorization"] = f"Bearer {token}"

    response = await client.get("/api/v1/ingest/config")
    assert response.status_code == 200, response.text


async def test_a_signed_in_person_may_not_report_telemetry(admin_client):
    """A person is not an agent: letting one post would forge an agent's history."""
    response = await admin_client.post(
        "/api/v1/ingest/traces",
        json={"traces": [{"name": "run", "start_time": "2026-01-01T00:00:00Z"}]},
    )
    assert response.status_code == 403
    assert "API keys only" in response.json()["error"]["message"]


async def test_a_key_may_read_telemetry_even_though_it_may_not_forge_it(
    ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Reader")
    response = await ingest_client.get("/api/v1/runs")
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Role.VIEWER, 403),
        (Role.MEMBER, 403),
        (Role.OPERATOR, 403),
        (Role.ADMIN, 201),
        (Role.OWNER, 201),
    ],
)
async def test_registering_an_agent_requires_admin(as_role, role, expected):
    async with as_role(role) as http:
        response = await http.post(
            "/api/v1/agents",
            json={
                "name": f"Agent for {role.value}",
                "platform": "Custom Agent",
                "agent_type": "Pro-code",
                "environment": "Development",
            },
        )
    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    ("role", "expected"),
    [(Role.MEMBER, 403), (Role.APPROVER, 200), (Role.ADMIN, 200)],
)
async def test_deciding_an_approval_requires_the_approver_role(
    as_role, factory, workspace, role, expected
):
    request_row = await factory.approval(workspace)
    async with as_role(role) as http:
        response = await http.post(
            f"/api/v1/approvals/{request_row.id}/approve", json={"note": "Looks right"}
        )
    assert response.status_code == expected, response.text


async def test_an_operator_may_not_reveal_a_credential(as_role, factory, workspace):
    secret = await factory.secret(workspace, name="Payments key")
    async with as_role(Role.OPERATOR) as http:
        response = await http.post(
            f"/api/v1/secrets/{secret.id}/reveal", json={"justification": "investigating"}
        )
    assert response.status_code == 403
    assert error_code(response) == "permission_denied"


async def test_a_viewer_may_read(viewer_client, factory, workspace):
    await factory.agent(workspace, name="Readable")
    response = await viewer_client.get("/api/v1/agents")
    assert response.status_code == 200
    assert response.json()["total"] == 1


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_a_second_workspace_sees_none_of_the_first_rows(
    as_role, factory, workspace, other_workspace
):
    await factory.agent(workspace, name="Northwind Bot")
    await factory.approval(workspace, action="Northwind refund")
    await factory.secret(workspace, name="Northwind key")
    await factory.agent(other_workspace, name="Contoso Bot")

    async with as_role(Role.ADMIN, other_workspace) as http:
        agents = await http.get("/api/v1/agents")
        approvals = await http.get("/api/v1/approvals")
        secrets = await http.get("/api/v1/secrets")

    assert [row["name"] for row in agents.json()["items"]] == ["Contoso Bot"]
    assert approvals.json()["total"] == 0
    assert secrets.json()["total"] == 0


async def test_another_tenants_row_answers_404_not_403(
    as_role, factory, workspace, other_workspace
):
    """A 403 would confirm the id exists; a missing row and a forbidden one look alike."""
    agent = await factory.agent(workspace, name="Northwind Bot")
    async with as_role(Role.ADMIN, other_workspace) as http:
        response = await http.get(f"/api/v1/agents/{agent.id}")
    assert response.status_code == 404
    assert error_code(response) == "not_found"


async def test_an_api_key_cannot_read_another_tenants_telemetry(
    app, factory, workspace, other_workspace, engine
):
    ours = await factory.provisioned_agent(workspace, engine, name="Ours")
    theirs = await factory.provisioned_agent(other_workspace, engine, name="Theirs")
    engine.add_trace(project_name=ours.engine_project_name, name="our run")
    engine.add_trace(project_name=theirs.engine_project_name, name="their run")

    token, _row = await factory.api_key(other_workspace, name="Contoso key")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        response = await http.get("/api/v1/runs")

    assert response.status_code == 200, response.text
    assert [row["agent"] for row in response.json()["items"]] == ["Theirs"]


async def test_switching_to_a_workspace_you_do_not_belong_to_answers_404(
    admin_client, other_workspace
):
    """The switcher must not double as a directory of tenants."""
    response = await admin_client.post(
        "/api/v1/auth/switch-workspace", json={"workspace": other_workspace.slug}
    )
    assert response.status_code == 404


async def test_switching_carries_the_role_held_in_the_target(
    client, factory, workspace, other_workspace
):
    user = await factory.user(workspace, email="dual@northwind.test", role=Role.VIEWER)
    await factory.membership(other_workspace, user, role=Role.OWNER)
    authorise(client, user, workspace, Role.VIEWER)

    response = await client.post(
        "/api/v1/auth/switch-workspace", json={"workspace": other_workspace.slug}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace"]["slug"] == other_workspace.slug
    assert body["role"] == Role.OWNER.value


async def test_the_workspace_header_cannot_reach_a_workspace_you_are_not_in(
    admin_client, other_workspace
):
    response = await admin_client.get(
        "/api/v1/agents", headers={"X-Fulcrum-Workspace": other_workspace.slug}
    )
    assert response.status_code == 403
    assert error_code(response) == "permission_denied"


async def test_the_workspace_header_switches_within_the_session(
    client, factory, workspace, other_workspace
):
    user = await factory.user(workspace, email="both@northwind.test", role=Role.ADMIN)
    await factory.membership(other_workspace, user, role=Role.ADMIN)
    await factory.agent(other_workspace, name="Contoso Bot")
    authorise(client, user, workspace, Role.ADMIN)

    response = await client.get(
        "/api/v1/agents", headers={"X-Fulcrum-Workspace": other_workspace.slug}
    )
    assert response.status_code == 200, response.text
    assert [row["name"] for row in response.json()["items"]] == ["Contoso Bot"]


async def test_a_session_for_a_deactivated_account_is_refused(client, db, factory, workspace):
    """A live session must not outlive the account it belongs to."""
    user = await factory.user(workspace, email="ex@northwind.test", role=Role.ADMIN)
    authorise(client, user, workspace, Role.ADMIN)
    assert (await client.get("/api/v1/auth/status")).status_code == 204

    await db.execute(update(User).where(User.id == user.id).values(is_active=False))

    response = await client.get("/api/v1/auth/status")
    assert response.status_code == 401
    assert error_code(response) == "unauthenticated"


async def test_a_session_for_a_revoked_membership_is_refused(client, db, factory, workspace):
    """Removing someone from a workspace must take effect on the next request."""
    user = await factory.user(workspace, email="removed@northwind.test", role=Role.ADMIN)
    authorise(client, user, workspace, Role.ADMIN)
    assert (await client.get("/api/v1/auth/status")).status_code == 204

    await db.execute(delete(Membership).where(Membership.user_id == user.id))

    response = await client.get("/api/v1/auth/status")
    assert response.status_code == 403
    assert error_code(response) == "permission_denied"
