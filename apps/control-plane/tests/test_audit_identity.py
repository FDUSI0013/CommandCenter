"""Regressions for the identity findings of the 2026-09-18 audit.

Each test pins one defect that was live: a member table that could be used to
take over an account in another tenant, a copy-ready snippet that crashed on
import, a bookkeeping write on every authenticated request, and scopes that were
checked nowhere but ingest.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, update

from conftest import APP_BASE_URL, authorise, error_code
from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.models.identity import ApiKey, Role, User, Workspace

STRONG_PASSWORD = "horse-battery-staple-9"
ATTACKER_PASSWORD = "attacker-chosen-pass-1"


# ---------------------------------------------------------------------------
# #54  A workspace admin could take over an account that belongs to another tenant
# ---------------------------------------------------------------------------


@pytest.fixture
async def contoso_admin(app, factory, other_workspace):
    """An admin of the *second* tenant: the attacker in the takeover tests."""
    user = await factory.user(
        other_workspace, email="admin@contoso.test", full_name="Mal Admin", role=Role.ADMIN
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_BASE_URL
    ) as http:
        authorise(http, user, other_workspace, Role.ADMIN)
        yield http


@pytest.fixture
async def northwind_owner(factory, workspace) -> User:
    """The victim: an owner of the first tenant, with a password of their own."""
    return await factory.user(
        workspace,
        email="owner@northwind.test",
        full_name="Ada Owner",
        role=Role.OWNER,
        password=STRONG_PASSWORD,
    )


async def _join(http: httpx.AsyncClient, email: str) -> dict:
    joined = await http.post(
        "/api/v1/workspaces/users", json={"email": email, "full_name": "Whoever"}
    )
    assert joined.status_code == 201, joined.text
    return joined.json()


async def test_an_admin_cannot_set_the_password_of_an_account_another_tenant_holds(
    client, contoso_admin, northwind_owner
):
    """Join by email, reset the password, sign in as the other tenant's owner."""
    joined = await _join(contoso_admin, northwind_owner.email)
    assert joined["id"] == northwind_owner.id
    assert joined["shared_account"] is True

    reset = await contoso_admin.patch(
        f"/api/v1/workspaces/users/{northwind_owner.id}", json={"password": ATTACKER_PASSWORD}
    )
    assert reset.status_code == 403, reset.text
    assert error_code(reset) == "permission_denied"
    assert reset.json()["error"]["details"]["reason"] == "shared_account"

    taken = await client.post(
        "/api/v1/auth/login",
        json={"email": northwind_owner.email, "password": ATTACKER_PASSWORD},
    )
    assert taken.status_code == 401, "the attacker's password must not open the account"
    kept = await client.post(
        "/api/v1/auth/login",
        json={"email": northwind_owner.email, "password": STRONG_PASSWORD},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["workspace"]["slug"] == "northwind"


async def test_an_admin_cannot_deactivate_an_account_another_tenant_holds(
    contoso_admin, db, northwind_owner
):
    """One PATCH used to lock a foreign tenant's owner out of every workspace."""
    await _join(contoso_admin, northwind_owner.email)

    response = await contoso_admin.patch(
        f"/api/v1/workspaces/users/{northwind_owner.id}", json={"is_active": False}
    )
    assert response.status_code == 403, response.text
    assert "remove them" in response.json()["error"]["message"]
    assert (await db.get(User, northwind_owner.id)).is_active is True

    # What the message points at still works, and is the per-workspace act.
    removed = await contoso_admin.delete(f"/api/v1/workspaces/users/{northwind_owner.id}")
    assert removed.status_code == 204, removed.text
    assert (await db.get(User, northwind_owner.id)).is_active is True


async def test_an_admin_cannot_rename_an_account_another_tenant_holds(
    contoso_admin, db, northwind_owner
):
    await _join(contoso_admin, northwind_owner.email)

    response = await contoso_admin.patch(
        f"/api/v1/workspaces/users/{northwind_owner.id}", json={"full_name": "Defaced"}
    )
    assert response.status_code == 403, response.text
    assert (await db.get(User, northwind_owner.id)).full_name == "Ada Owner"


async def test_the_role_of_a_shared_account_is_still_this_workspaces_to_set(
    contoso_admin, northwind_owner
):
    """The membership is ours even when the account is not — and a form that sends
    the unchanged record back alongside the role has not asked to edit the account."""
    await _join(contoso_admin, northwind_owner.email)

    response = await contoso_admin.patch(
        f"/api/v1/workspaces/users/{northwind_owner.id}",
        json={"role": "operator", "full_name": "Ada Owner", "is_active": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "operator"


async def test_an_account_held_by_this_workspace_alone_is_still_managed_here(
    admin_client, client, db
):
    """The legitimate half of the feature: reset and deactivate your own people."""
    created = await admin_client.post(
        "/api/v1/workspaces/users",
        json={"email": "quinn@northwind.test", "full_name": "Quinn Naylor"},
    )
    assert created.status_code == 201, created.text
    member = created.json()
    assert member["shared_account"] is False

    reset = await admin_client.patch(
        f"/api/v1/workspaces/users/{member['id']}", json={"password": STRONG_PASSWORD}
    )
    assert reset.status_code == 200, reset.text
    signed_in = await client.post(
        "/api/v1/auth/login", json={"email": "quinn@northwind.test", "password": STRONG_PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text

    off = await admin_client.patch(
        f"/api/v1/workspaces/users/{member['id']}", json={"is_active": False}
    )
    assert off.status_code == 200, off.text
    assert off.json()["status"] == "Inactive"


async def test_an_admin_cannot_set_their_own_password_from_the_member_table(
    admin_client, admin, db
):
    """``/auth/change-password`` asks for the current password; this was the way round it."""
    response = await admin_client.patch(
        f"/api/v1/workspaces/users/{admin.id}", json={"password": ATTACKER_PASSWORD}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "own_account"
    assert (await db.get(User, admin.id)).password_hash is None


async def test_the_member_list_flags_the_accounts_it_does_not_solely_hold(
    contoso_admin, northwind_owner
):
    await _join(contoso_admin, northwind_owner.email)

    listed = await contoso_admin.get("/api/v1/workspaces/users")
    assert listed.status_code == 200, listed.text
    shared = {row["email"]: row["shared_account"] for row in listed.json()["items"]}
    assert shared == {"admin@contoso.test": False, northwind_owner.email: True}


async def test_a_null_for_a_required_account_field_is_no_change_not_a_500(admin_client, member):
    response = await admin_client.patch(
        f"/api/v1/workspaces/users/{member.id}", json={"is_active": None, "full_name": None}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "Active"
    assert response.json()["full_name"] == "Eli Member"


# ---------------------------------------------------------------------------
# #52  The copy-ready Python snippet crashed on import and named an agent the
#      key is refused for
# ---------------------------------------------------------------------------

SDK_SOURCE = Path(__file__).resolve().parents[3] / "sdks" / "python" / "src"


def _snippet(minted: dict, label: str) -> str:
    return next(s["code"] for s in minted["snippets"] if s["label"] == label)


async def test_the_python_snippet_runs_as_pasted_against_the_sdk(
    admin_client, factory, workspace, engine, monkeypatch
):
    """Executed, not read: the old one died with a TypeError before its first line
    of agent code, and nothing but running it would have said so."""
    agent = await factory.provisioned_agent(workspace, engine, name='UW "Bridge"')
    minted = await admin_client.post(
        "/api/v1/workspaces/api-keys",
        json={"name": "Bridge key", "agent_id": agent.id, "environment": "Staging"},
    )
    assert minted.status_code == 201, minted.text
    code = _snippet(minted.json(), "Python SDK")

    # Reporting is switched off on purpose: the snippet must import and run, not
    # reach for a network this suite does not have.
    monkeypatch.setenv("FULCRUM_OPS_DISABLED", "1")
    monkeypatch.syspath_prepend(str(SDK_SOURCE))
    import fulcrum_ops

    namespace: dict = {"__name__": "pasted_agent"}
    try:
        exec(compile(code, "<python-sdk-snippet>", "exec"), namespace)  # noqa: S102
        assert namespace["handle"]("Is telemetry arriving?") == "You asked: Is telemetry arriving?"
        client = namespace["client"]
        assert client.agent == agent.name, "the snippet must name the agent the key is bound to"
        assert client.environment == "Staging"
    finally:
        fulcrum_ops.shutdown(timeout=1.0)


async def test_the_snippets_name_an_agent_the_key_is_accepted_for(
    admin_client, client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="UW Bridge")
    minted = (
        await admin_client.post(
            "/api/v1/workspaces/api-keys", json={"name": "Bound key", "agent_id": agent.id}
        )
    ).json()

    for label in ("Python SDK", "TypeScript SDK"):
        code = _snippet(minted, label)
        assert '"UW Bridge"' in code, f"{label} does not name the bound agent"
        assert "support-copilot" not in code
    assert "FULCRUM_OPS_AGENT='UW Bridge'" in _snippet(minted, "Environment")

    # And that name is one ingest takes from this key, which is the point.
    reported = await client.post(
        "/api/v1/ingest/traces",
        headers={"Authorization": f"Bearer {minted['token']}"},
        json={
            "traces": [
                {"name": "handle", "start_time": "2026-01-01T00:00:00Z", "agent": "UW Bridge"}
            ]
        },
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["accepted"] == 1, reported.text


async def test_an_unbound_key_that_cannot_register_is_told_the_agent_must_exist(admin_client):
    plain = (
        await admin_client.post("/api/v1/workspaces/api-keys", json={"name": "Plain key"})
    ).json()
    assert "YOUR-AGENT-NAME" in _snippet(plain, "Python SDK")
    assert "Agent Registry" in _snippet(plain, "Python SDK")
    assert "Agent Registry" in _snippet(plain, "TypeScript SDK")

    registrar = (
        await admin_client.post(
            "/api/v1/workspaces/api-keys",
            json={"name": "Registrar key", "scopes": ["ingest", "admin"]},
        )
    ).json()
    assert "registers it on first report" in _snippet(registrar, "Python SDK")


# ---------------------------------------------------------------------------
# #169  Every API-key request rewrote its api_keys row; a console request paid
#       three lookups in a row to find out who was calling
# ---------------------------------------------------------------------------


async def test_a_busy_key_is_noted_once_a_minute_not_on_every_request(client, db, ingest_key):
    """The five-second config poller was some 17,000 UPDATEs a day on one hot row."""
    token, row = ingest_key
    client.headers["Authorization"] = f"Bearer {token}"

    assert (await client.get("/api/v1/auth/status")).status_code == 204
    first = await db.get(ApiKey, row.id)
    assert first.last_used_at is not None, "the first use is still recorded"
    assert first.updated_at == row.updated_at, "being used is not an edit of the key"

    for _ in range(3):
        assert (await client.get("/api/v1/auth/status")).status_code == 204
    again = await db.get(ApiKey, row.id)
    assert again.last_used_at == first.last_used_at, "rewritten inside the same minute"

    # Once the note is more than a minute old the next request refreshes it.
    stale = first.last_used_at - dt.timedelta(minutes=2)
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == row.id)
        .values(last_used_at=stale, updated_at=ApiKey.updated_at)
    )
    assert (await client.get("/api/v1/auth/status")).status_code == 204
    refreshed = await db.get(ApiKey, row.id)
    assert refreshed.last_used_at > first.last_used_at
    assert refreshed.updated_at == row.updated_at


async def test_a_recently_noted_key_makes_a_read_only_request(client, db_engine, ingest_key):
    """Once noted, authenticating with the key writes nothing at all."""
    token, _row = ingest_key
    client.headers["Authorization"] = f"Bearer {token}"
    assert (await client.get("/api/v1/auth/status")).status_code == 204

    statements: list[str] = []

    def _seen(_conn, _cursor, statement, *_rest) -> None:
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(db_engine.sync_engine, "before_cursor_execute", _seen)
    try:
        assert (await client.get("/api/v1/auth/status")).status_code == 204
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _seen)
    assert "UPDATE" not in statements, statements


async def test_a_console_request_finds_its_caller_in_one_statement(admin_client, db_engine):
    """Account, workspace and membership were three round trips before any handler ran."""
    statements: list[str] = []

    def _seen(_conn, _cursor, statement, *_rest) -> None:
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _seen)
    try:
        assert (await admin_client.get("/api/v1/auth/status")).status_code == 204
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _seen)
    assert len(statements) == 1, statements


async def test_the_single_lookup_still_tells_the_refusals_apart(
    admin_client, db, workspace, other_workspace
):
    unknown = await admin_client.get(
        "/api/v1/auth/status", headers={"X-Fulcrum-Workspace": "no-such-tenant"}
    )
    assert unknown.status_code == 403
    assert unknown.json()["error"]["message"] == "Unknown workspace."

    foreign = await admin_client.get(
        "/api/v1/auth/status", headers={"X-Fulcrum-Workspace": other_workspace.slug}
    )
    assert foreign.status_code == 403
    assert "do not have access" in foreign.json()["error"]["message"]

    await db.execute(
        update(Workspace).where(Workspace.id == workspace.id).values(status="suspended")
    )
    suspended = await admin_client.get("/api/v1/auth/status")
    assert suspended.status_code == 401
    assert "not active" in suspended.json()["error"]["message"]


async def test_sign_in_attempts_are_refused_before_the_password_is_checked(
    client, monkeypatch, northwind_owner
):
    """Nothing bounded the argon2 verifications an anonymous caller could ask for:
    the lockout counts failures against a real account, and a loop over made-up
    addresses never trips it."""
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 3)

    for _ in range(3):
        wrong = await client.post(
            "/api/v1/auth/login", json={"email": "nobody@nowhere.test", "password": "guess-1"}
        )
        assert wrong.status_code == 401, wrong.text

    refused = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@nowhere.test", "password": "guess-1"}
    )
    assert refused.status_code == 429, refused.text
    assert error_code(refused) == "rate_limited"
    assert int(refused.headers["Retry-After"]) >= 1

    # The window is the named account's: somebody else still signs in.
    other = await client.post(
        "/api/v1/auth/login", json={"email": northwind_owner.email, "password": STRONG_PASSWORD}
    )
    assert other.status_code == 200, other.text


async def test_a_spray_across_accounts_meets_the_window_of_its_address(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_per_address_per_minute", 4)

    answers = [
        (
            await client.post(
                "/api/v1/auth/login",
                json={"email": f"guess-{index}@nowhere.test", "password": "guess-1"},
            )
        ).status_code
        for index in range(6)
    ]
    assert answers == [401, 401, 401, 401, 429, 429]


async def test_a_password_is_verified_off_the_event_loop(client, northwind_owner):
    """argon2 inside the handler held the worker's loop for every attempt, and the
    ingest batches and live streams that loop was serving with it.

    Proved by occupying the hashing pool: a sign-in that really goes through it
    has to wait its turn, and the loop goes on running while it does.
    """
    import asyncio
    import threading

    from fulcrum_ops_api.core import security

    release = threading.Event()
    held = [
        security._password_pool.submit(release.wait, 30)
        for _ in range(security.PASSWORD_POOL_WORKERS)
    ]
    try:
        attempt = asyncio.create_task(
            client.post(
                "/api/v1/auth/login",
                json={"email": northwind_owner.email, "password": STRONG_PASSWORD},
            )
        )
        for _ in range(20):
            await asyncio.sleep(0.05)
        assert not attempt.done(), "the password was checked on the event loop, not in the pool"
    finally:
        release.set()
    signed_in = await attempt
    assert signed_in.status_code == 200, signed_in.text
    for job in held:
        job.result(timeout=5)
