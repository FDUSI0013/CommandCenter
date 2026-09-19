"""Regression tests for the core findings of the 2026-09-18 audit.

The adapter, the health probe, the clock, the limiter and the list plumbing:
the parts every screen stands on, and therefore the parts whose faults showed
up everywhere at once -- 90 s requests, erratic schedules, pages that repeat a
row.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import BackgroundTasks

from fulcrum_ops_api.api.deps import Db
from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.engine import EngineClient, EngineTimeout, EngineUnavailable, deadline

ENGINE_URL = "http://telemetry-engine.internal"


def _adapter(handler, **kwargs) -> EngineClient:
    """A real adapter over httpx's own in-memory transport.

    The engine double answers instantly and never drops a connection, so the
    transport-level behaviour under test here -- what is replayed and what is
    not -- needs a wire that can misbehave on demand.
    """
    return EngineClient(
        base_url=ENGINE_URL, retries=2, transport=httpx.MockTransport(handler), **kwargs
    )


# ---------------------------------------------------------------------------
# #8 / #60 / #179 -- a read timeout is not replayed; a refused connection is
# ---------------------------------------------------------------------------


async def test_a_read_timeout_on_a_get_is_reported_once_and_never_replayed() -> None:
    attempts: list[str] = []

    def slow_store(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ReadTimeout("the query is still running", request=request)

    client = _adapter(slow_store)
    try:
        with pytest.raises(EngineTimeout):
            await client.get_trace_stats(project_id="p-1")
    finally:
        await client.aclose()

    assert len(attempts) == 1, (
        "a timed-out read was re-sent: each replay starts another copy of the "
        f"same heavy query beside the first ({len(attempts)} attempts)"
    )


async def test_a_refused_connection_is_still_retried_on_a_get() -> None:
    attempts: list[str] = []

    def restarting(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        if len(attempts) == 2:
            raise httpx.ConnectTimeout("no route yet", request=request)
        return httpx.Response(200, json={"stats": []})

    client = _adapter(restarting)
    try:
        assert await client.get_trace_stats(project_id="p-1") == {"stats": []}
    finally:
        await client.aclose()
    assert len(attempts) == 3


async def test_a_5xx_is_still_retried_on_a_get_and_never_on_a_post() -> None:
    seen: list[str] = []

    def failing(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(503, json={"errors": ["warming up"]})

    client = _adapter(failing)
    try:
        with pytest.raises(EngineUnavailable):
            await client.get_trace("t-1")
        assert seen == ["GET", "GET", "GET"]
        seen.clear()
        with pytest.raises(EngineUnavailable):
            await client.create_traces_batch([{"id": "t-1"}])
        assert seen == ["POST"], "a replayed POST duplicates customer telemetry"
    finally:
        await client.aclose()


async def test_screen_aggregates_run_on_the_read_timeout_and_writes_do_not() -> None:
    timeouts: dict[str, float | None] = {}

    def recording(request: httpx.Request) -> httpx.Response:
        timeouts[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={})

    client = _adapter(recording, timeout_seconds=30.0, read_timeout_seconds=7.0)
    try:
        await client.get_project_stats()
        await client.get_trace_stats(project_id="p-1")
        await client.get_project_metrics(
            "p-1", metric_type="TRACE_COUNT", interval="DAILY", interval_start="2026-09-01"
        )
        await client.get_cost_summary(interval_start="2026-09-01", interval_end="2026-09-02")
        await client.create_traces_batch([{"id": "t-1"}])
        # An export pages the same aggregate on its own clock by saying so.
        await client.get_span_stats(project_id="p-1", timeout_seconds=30.0)
    finally:
        await client.aclose()

    assert timeouts["/v1/private/projects/stats"] == 7.0
    assert timeouts["/v1/private/traces/stats"] == 7.0
    assert timeouts["/v1/private/projects/p-1/metrics"] == 7.0
    assert timeouts["/v1/private/workspaces/costs/summaries"] == 7.0
    assert timeouts["/v1/private/traces/batch"] == 30.0
    assert timeouts["/v1/private/spans/stats"] == 30.0


async def test_a_fan_out_deadline_answers_as_an_engine_timeout() -> None:
    started = asyncio.get_running_loop().time()
    with pytest.raises(EngineTimeout):
        async with deadline(0.05, what="metrics rollup"):
            await asyncio.gather(*(asyncio.sleep(5) for _ in range(4)))
    assert asyncio.get_running_loop().time() - started < 1.0

    # A timeout that is not the deadline's own passes through untouched.
    with pytest.raises(TimeoutError):
        async with deadline(5):
            raise TimeoutError("somebody else's")

    assert settings.engine_fanout_deadline_seconds < 30, "must beat the console's abort"


# ---------------------------------------------------------------------------
# #160 / #177 / #213 / #219 -- the clock's lock is released when the tick ends
# ---------------------------------------------------------------------------


def test_the_tick_lock_is_transaction_scoped() -> None:
    """There is no Postgres under this suite, so the statement itself is pinned.

    The session-scoped form survives the ROLLBACK that returns a pooled
    connection, so the first winner kept the lock for half an hour and every
    other tick, in every worker, was skipped in silence.
    """
    from sqlalchemy.dialects import postgresql

    from fulcrum_ops_api.services import scheduler

    sql = str(scheduler._lock_statement().compile(dialect=postgresql.dialect()))
    assert "pg_try_advisory_xact_lock" in sql, sql


async def test_a_sweep_restatuses_secrets_without_moving_their_concurrency_token(
    db, factory, workspace
):
    import datetime as dt

    from fulcrum_ops_api.models.governance import Secret, SecretStatus
    from fulcrum_ops_api.services import scheduler

    expired = await factory.secret(
        workspace, name="Old cert", expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    )
    before = (await db.get(Secret, expired.id)).updated_at

    counts = await scheduler.run_once()
    assert counts["secrets_restatused"] == 1

    after = await db.get(Secret, expired.id)
    assert after.status == SecretStatus.EXPIRED.value
    assert after.updated_at == before, (
        "the clock catching up with a date is not an edit: moving updated_at "
        "409s whoever has the secret open in the console"
    )
    # Every worker now wins the lock in turn, so a second pass must be a no-op.
    assert (await scheduler.run_once())["secrets_restatused"] == 0


# ---------------------------------------------------------------------------
# #144 -- a cadence counts from when it was set, not from a stale last run
# ---------------------------------------------------------------------------


async def test_scheduling_an_old_suite_does_not_fire_it_on_the_next_tick(
    as_role, db, factory, workspace, monkeypatch
):
    import datetime as dt

    from sqlalchemy import select, update

    from fulcrum_ops_api.models.identity import Role
    from fulcrum_ops_api.models.quality import TestRun, TestSuite
    from fulcrum_ops_api.services import scheduler, testing

    async def swallowed(_run_id: str, _workspace_id: str) -> None:
        return None

    monkeypatch.setattr(testing, "execute_run", swallowed)

    suite = await factory.test_suite(workspace, name="Payments regression")
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    await db.execute(
        update(TestSuite)
        .where(TestSuite.id == suite.id)
        .values(created_at=long_ago, last_run_at=long_ago)
    )

    # Hourly, half an hour from now: counted from three days ago a fire time is
    # long past; counted from now the first one is still ahead. (Relative to the
    # clock so the test cannot straddle its own cron boundary.)
    minute = (dt.datetime.now(dt.UTC).minute + 30) % 60

    async with as_role(Role.OPERATOR) as http:
        created = await http.post(
            "/api/v1/testing/schedules",
            json={"suite_id": suite.id, "cron": f"{minute} * * * *"},
        )
        assert created.status_code == 201, created.text

        counts = await scheduler.run_once()
        assert counts["test_suites_started"] == 0, (
            "binding a schedule ran the suite on the very next tick"
        )
        assert await db.scalars(select(TestRun)) == []

        changed = await http.patch(
            f"/api/v1/testing/schedules/{suite.id}",
            json={"cron": f"{(minute + 1) % 60} * * * *"},
        )
        assert changed.status_code == 200, changed.text
        assert (await scheduler.run_once())["test_suites_started"] == 0


# ---------------------------------------------------------------------------
# #60 / #174 -- the two 503s that mean "slow right now" say when to come back
# ---------------------------------------------------------------------------


async def test_a_telemetry_outage_tells_pollers_how_long_to_stay_away(
    as_role, engine, factory, workspace
):
    from conftest import error_code
    from fulcrum_ops_api.models.identity import Role

    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.fail(503)
    async with as_role(Role.VIEWER) as http:
        refused = await http.get("/api/v1/runs")
    assert refused.status_code == 503, refused.text
    assert error_code(refused) == "telemetry_unavailable"
    assert int(refused.headers["retry-after"]) >= 1, (
        "a slow store is made slower by everything that asks again at once"
    )


async def test_a_full_database_pool_answers_busy_not_an_opaque_500(
    as_role, db_engine, workspace
):
    """SQLite runs unpooled under test, so this test gives the app a pool of one.

    The request path is the production one: the pool's checkout timeout is
    raised from inside the first dependency that touches the session.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    from conftest import error_code
    from fulcrum_ops_api.db import session as db_session
    from fulcrum_ops_api.models.identity import Role

    async with as_role(Role.VIEWER) as http:
        pooled = create_async_engine(
            db_engine.url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.2,
        )
        db_session._engine, db_session._sessionmaker = pooled, None
        try:
            async with pooled.connect():  # the one connection, held elsewhere
                refused = await http.get("/api/v1/agents")
            served = await http.get("/api/v1/agents")
        finally:
            db_session._engine, db_session._sessionmaker = db_engine, None
            await pooled.dispose()

    assert refused.status_code == 503, refused.text
    assert error_code(refused) == "busy"
    assert int(refused.headers["retry-after"]) >= 1
    assert served.status_code == 200, "and it passes as soon as a connection frees up"


# ---------------------------------------------------------------------------
# #163 -- the renewal sweep the licensing model describes now exists
# ---------------------------------------------------------------------------


async def test_the_clock_ends_renews_and_flags_licence_terms(
    db, factory, workspace, other_workspace
):
    import datetime as dt

    from sqlalchemy import select, update

    from fulcrum_ops_api.models.governance import AuditEvent
    from fulcrum_ops_api.models.licensing import LicenseStatus, TenantLicense
    from fulcrum_ops_api.services import scheduler

    now = dt.datetime.now(dt.UTC)

    async def licence(target, status, *, expires_in_days, auto_renew):
        row = await factory.license(target, status=status)
        await db.execute(
            update(TenantLicense)
            .where(TenantLicense.id == row.id)
            .values(
                expires_at=now + dt.timedelta(days=expires_in_days), auto_renew=auto_renew
            )
        )
        return row.id

    third = await factory.workspace(name="Fabrikam", slug="fabrikam")
    fourth = await factory.workspace(name="Tailspin", slug="tailspin")
    trial = await licence(
        workspace, LicenseStatus.TRIAL.value, expires_in_days=-12, auto_renew=False
    )
    renewing = await licence(
        other_workspace, LicenseStatus.ACTIVE.value, expires_in_days=-3, auto_renew=True
    )
    closing = await licence(
        third, LicenseStatus.ACTIVE.value, expires_in_days=10, auto_renew=False
    )
    suspended = await licence(
        fourth, LicenseStatus.SUSPENDED.value, expires_in_days=-40, auto_renew=False
    )
    token_before = (await db.get(TenantLicense, closing)).updated_at

    counts = await scheduler.run_once()
    assert (
        counts["licenses_expired"],
        counts["licenses_renewed"],
        counts["licenses_restatused"],
    ) == (1, 1, 1), counts

    ended = await db.get(TenantLicense, trial)
    assert ended.status == LicenseStatus.EXPIRED.value, "a trial stayed entitled for ever"

    rolled = await db.get(TenantLicense, renewing)
    assert rolled.status == LicenseStatus.ACTIVE.value
    # The factory's plan bills annually: one term on from the date that passed.
    assert 360 <= (rolled.expires_at - now).days <= 364, rolled.expires_at
    assert (now - rolled.starts_at).days == 3

    flagged = await db.get(TenantLicense, closing)
    assert flagged.status == LicenseStatus.EXPIRING_SOON.value
    assert flagged.updated_at == token_before, "the calendar reaching a date is not an edit"

    held = await db.get(TenantLicense, suspended)
    assert held.status == LicenseStatus.SUSPENDED.value, "a suspension is a decision"

    actions = {
        (event.entity_id, event.action)
        for event in await db.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "tenant_license")
        )
    }
    assert actions == {
        (trial, "licensing.license.expired"),
        (renewing, "licensing.license.renewed"),
    }

    # Every worker wins the lock in turn, so a second pass must change nothing.
    again = await scheduler.run_once()
    assert (
        again["licenses_expired"],
        again["licenses_renewed"],
        again["licenses_restatused"],
    ) == (0, 0, 0), again

    # A term extended past the horizon stops being "Expiring Soon".
    await db.execute(
        update(TenantLicense)
        .where(TenantLicense.id == closing)
        .values(expires_at=now + dt.timedelta(days=200))
    )
    assert (await scheduler.run_once())["licenses_restatused"] == 1
    assert (await db.get(TenantLicense, closing)).status == LicenseStatus.ACTIVE.value


# ---------------------------------------------------------------------------
# systemic -- a background job can see the row its route just wrote
# ---------------------------------------------------------------------------


async def test_commit_then_lets_a_background_job_find_the_row_it_was_given(app, client):
    """Two routes that differ only in how they schedule the job.

    The dependency's commit runs after the response is sent, which is after the
    background tasks: the plain ``add_task`` route is the trap every "runs stuck
    Queued" report came from, kept here so the test says why the helper exists.
    (``Db`` and ``BackgroundTasks`` are imported at module level because the
    routes' annotations are resolved against it.)
    """
    from fulcrum_ops_api.db.session import commit_then, get_sessionmaker
    from fulcrum_ops_api.models.identity import Workspace

    found: dict[str, bool] = {}

    async def job(workspace_id: str) -> None:
        async with get_sessionmaker()() as own:  # its own session, as the real jobs do
            found[workspace_id] = await own.get(Workspace, workspace_id) is not None

    def queued(slug: str) -> Workspace:
        return Workspace(
            name=slug, slug=slug, engine_workspace=slug, status="active", settings={}
        )

    @app.post("/_audit/plain")
    async def plain(session: Db, background: BackgroundTasks) -> dict[str, str]:
        row = queued("plain")
        session.add(row)
        await session.flush()
        background.add_task(job, row.id)
        return {"id": row.id}

    @app.post("/_audit/durable")
    async def durable(session: Db, background: BackgroundTasks) -> dict[str, str]:
        row = queued("durable")
        session.add(row)
        await session.flush()
        await commit_then(session, background, job, row.id)
        return {"id": row.id, "name": row.name}  # still readable after the commit

    first = await client.post("/_audit/plain")
    assert first.status_code == 200, first.text
    trapped = first.json()["id"]
    assert found[trapped] is False, "the premise changed: re-read db.session.commit_then"

    kept = await client.post("/_audit/durable")
    assert kept.status_code == 200, kept.text
    assert found[kept.json()["id"]] is True, "the job ran before its row was committed"


# ---------------------------------------------------------------------------
# list plumbing -- a sort is total, and the same on every database
# ---------------------------------------------------------------------------


async def test_a_sorted_list_puts_unrecorded_values_last_and_breaks_ties_by_id(
    as_role, db, factory, workspace
):
    import datetime as dt

    from sqlalchemy import update

    from fulcrum_ops_api.models.identity import Role
    from fulcrum_ops_api.models.quality import TestSuite

    now = dt.datetime.now(dt.UTC)
    suites = [await factory.test_suite(workspace, name=f"Suite {n}") for n in range(5)]
    ran_long_ago, ran_today = suites[1].id, suites[3].id
    never_ran = {suites[0].id, suites[2].id, suites[4].id}
    for suite_id, when in ((ran_long_ago, now - dt.timedelta(days=9)), (ran_today, now)):
        await db.execute(
            update(TestSuite).where(TestSuite.id == suite_id).values(last_run_at=when)
        )

    async with as_role(Role.VIEWER) as http:

        async def ids(**params) -> list[str]:
            page = await http.get("/api/v1/testing/suites", params=params)
            assert page.status_code == 200, page.text
            return [row["id"] for row in page.json()["items"]]

        # Postgres calls NULL the largest value and SQLite the smallest, so one
        # of these two opened with every never-run suite, whichever you ran on.
        newest_first = await ids(sort="-last_run_at")
        oldest_first = await ids(sort="last_run_at")
        assert newest_first[:2] == [ran_today, ran_long_ago]
        assert oldest_first[:2] == [ran_long_ago, ran_today]
        assert set(newest_first[2:]) == set(oldest_first[2:]) == never_ran

        # Five rows, one status: the order inside the tie is the key's, in the
        # direction of the sort, so OFFSET pages cannot repeat or drop a row.
        by_id = sorted(suite.id for suite in suites)
        assert await ids(sort="status") == by_id
        assert await ids(sort="-status") == by_id[::-1]
        paged = [
            row
            for page in (1, 2, 3)
            for row in await ids(sort="-status", page=page, page_size=2)
        ]
        assert paged == by_id[::-1]


# ---------------------------------------------------------------------------
# #61 -- /health answers inside its ceiling while the engine hangs
# ---------------------------------------------------------------------------


async def test_health_reports_a_hanging_engine_instead_of_hanging_with_it(
    client, monkeypatch
):
    from fulcrum_ops_api import main
    from fulcrum_ops_api.engine import set_engine_client

    async def hanging(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2.5)
        return httpx.Response(200, json={"healthy": True})

    monkeypatch.setattr(main, "HEALTH_CHECK_SECONDS", 0.2)
    monkeypatch.setattr(settings, "engine_required", True)
    # Once against the healthy double first: the first request through a fresh
    # app pays for building its middleware stack, which is not what is timed.
    assert (await client.get("/health")).status_code == 200

    stuck = _adapter(hanging)
    set_engine_client(stuck)  # the engine_client fixture clears it again on teardown
    try:
        started = asyncio.get_running_loop().time()
        probe = await client.get("/health")
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        set_engine_client(None)
        await stuck.aclose()

    assert elapsed < 1.5, f"the probe waited {elapsed:.1f}s on a dependency that was stuck"
    assert probe.status_code == 503
    assert probe.json()["checks"] == {"database": True, "telemetry": False}
