"""Memory & State: what a purge may delete, and what a backup really is.

Defects from the September audit, each pinned by driving the API the way the
console does and then looking at what is left in the telemetry store.

* Purge Data swept every provisioned agent in the workspace and deleted every
  trace older than the store's window — ordinary runs included, Production
  included — on the strength of an empty request body. It now reaches only the
  agents bound to the store, only whole conversation threads whose last activity
  is past retention, and only when the caller names the store it means.
* A purge that failed part way answered 503 and rolled back its audit row,
  leaving deletions nobody had recorded.
* Create Backup kept nothing and Restore rewrote four counters, while both
  claimed to have captured and restored the records.
* A thread-backed store's Records and Sessions could never leave zero, and a
  store could not be removed.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select, update

from conftest import error_code, utcnow
from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.registry import Agent, EnvironmentType, MemoryStoreType

OLD = dt.timedelta(days=40)


def say(engine, agent, thread_id: str | None, *, age: dt.timedelta, name: str = "turn") -> dict:
    """One trace in an agent's project, written ``age`` ago.

    The write stamp is backdated with the trace: a thread's last activity is
    read off ``last_updated_at`` first, and the double — like the engine —
    stamps that with the moment of the write unless it is told otherwise.
    """
    moment = utcnow() - age
    fields = {"thread_id": thread_id} if thread_id else {}
    return engine.add_trace(
        project_name=agent.engine_project_name,
        name=name,
        start_time=moment,
        end_time=moment,
        last_updated_at=moment.isoformat(),
        **fields,
    )


async def purge_rows(db, workspace) -> list[AuditEvent]:
    return await db.scalars(
        select(AuditEvent)
        .where(AuditEvent.workspace_id == workspace.id, AuditEvent.action == "memory.purged")
        .order_by(AuditEvent.occurred_at, AuditEvent.id)
    )


async def bound(factory, workspace, engine, *, store_name: str = "Support memory", **store):
    """A 30-day conversation store and the one agent whose memory policy names it."""
    store_row = await factory.memory_store(
        workspace,
        name=store_name,
        store_type=MemoryStoreType.CONVERSATION.value,
        retention_days=30,
        retention_policy="30 days",
        **store,
    )
    agent = await factory.provisioned_agent(
        workspace, engine, name="Support Bot", memory_policy=store_name
    )
    return store_row, agent


# ---------------------------------------------------------------------------
# Purge: whose, what, and on whose word
# ---------------------------------------------------------------------------


async def test_a_purge_reaches_only_the_agents_bound_to_the_store(
    admin_client, factory, workspace, engine
):
    """The audited scenario: a Development store's purge and a Production agent."""
    store, support = await bound(
        factory, workspace, engine, environment=EnvironmentType.DEVELOPMENT.value
    )
    billing = await factory.provisioned_agent(
        workspace, engine, name="Billing Bot", environment=EnvironmentType.PRODUCTION.value
    )
    expired = say(engine, support, "conv-old", age=OLD)
    others_thread = say(engine, billing, "conv-billing", age=OLD)
    others_run = say(engine, billing, None, age=OLD, name="nightly reconciliation")

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agents"] == ["Support Bot"]
    assert body["projects"] == 1
    assert body["purged_records"] == 1
    assert expired["id"] not in engine.traces
    assert others_thread["id"] in engine.traces, "another agent's conversation was deleted"
    assert others_run["id"] in engine.traces, "another agent's run history was deleted"
    swept = {
        (call.body or {}).get("project_id")
        for call in engine.calls
        if call.path.endswith(("/traces/search", "/traces/threads/search", "/traces/delete"))
    }
    assert swept == {support.engine_project_id}, "the purge addressed a project it does not own"


async def test_a_purge_leaves_runs_that_belong_to_no_conversation(
    admin_client, factory, workspace, engine
):
    store, support = await bound(factory, workspace, engine)
    expired = say(engine, support, "conv-old", age=OLD)
    plain_run = say(engine, support, None, age=OLD, name="batch classification")

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )

    assert response.status_code == 200, response.text
    assert response.json()["purged_traces"] == 1
    assert expired["id"] not in engine.traces
    assert plain_run["id"] in engine.traces, "run history is not memory; retention skips it"


async def test_a_conversation_still_being_written_to_keeps_its_early_messages(
    admin_client, factory, workspace, engine
):
    """Expiry is last activity plus retention — the same rule the Records view shows."""
    store, support = await bound(factory, workspace, engine)
    first = say(engine, support, "conv-live", age=OLD)
    latest = say(engine, support, "conv-live", age=dt.timedelta(hours=1))

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )

    assert response.status_code == 200, response.text
    assert response.json()["purged_records"] == 0
    assert {first["id"], latest["id"]} <= set(engine.traces)


async def test_a_purge_is_refused_until_the_caller_names_the_store(
    admin_client, factory, workspace, engine, db
):
    store, support = await bound(factory, workspace, engine)
    expired = say(engine, support, "conv-old", age=OLD)

    empty = await admin_client.post(f"/api/v1/memory/{store.id}/purge", json={})
    wrong = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": "some other store"}
    )

    for response in (empty, wrong):
        assert response.status_code == 422, response.text
        assert error_code(response) == "validation_failed"
        assert response.json()["error"]["details"] == {"field": "confirm"}
    assert expired["id"] in engine.traces
    assert engine.calls_to("/traces/delete") == []
    assert await purge_rows(db, workspace) == []


async def test_a_store_no_agent_names_is_not_purged(admin_client, factory, workspace, engine):
    """No binding means no scope — and the fallback is nothing, not the workspace."""
    store = await factory.memory_store(
        workspace, name="Orphan memory", retention_days=7, retention_policy="7 days"
    )
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    expired = say(engine, agent, "conv-old", age=OLD)

    for body in ({"dry_run": True}, {"confirm": store.name}):
        response = await admin_client.post(f"/api/v1/memory/{store.id}/purge", json=body)
        assert response.status_code == 412, response.text
        assert "memory policy" in response.json()["error"]["message"]

    assert expired["id"] in engine.traces
    assert engine.calls_to("/traces/delete") == []


async def test_a_purge_reaches_the_idle_agents_the_screens_fan_out_cap_drops(
    admin_client, factory, workspace, engine
):
    """The cap keeps the most recently used agents, which is backwards for a purge.

    The agent nobody has used for months is the one whose conversations have
    expired, and it was the first to fall off the end of a capped list — on
    every run, so repeating the purge never reached it.
    """
    from fulcrum_ops_api.services.memory import MAX_PROJECT_FANOUT

    store = await factory.memory_store(
        workspace,
        name="Support memory",
        store_type=MemoryStoreType.CONVERSATION.value,
        retention_days=30,
        retention_policy="30 days",
    )
    dormant = await factory.provisioned_agent(
        workspace,
        engine,
        name="Dormant Bot",
        memory_policy=store.name,
        last_used_at=utcnow() - dt.timedelta(days=90),
    )
    for index in range(MAX_PROJECT_FANOUT):
        await factory.provisioned_agent(
            workspace, engine, name=f"Busy Bot {index:02d}", memory_policy=store.name
        )
    expired = say(engine, dormant, "conv-dormant", age=OLD)

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["projects"] == MAX_PROJECT_FANOUT + 1
    assert "Dormant Bot" in body["agents"]
    assert body["purged_records"] == 1
    assert expired["id"] not in engine.traces


async def test_a_dry_run_says_what_would_go_and_whose_without_touching_it(
    admin_client, factory, workspace, engine, db
):
    store, support = await bound(factory, workspace, engine)
    expired = say(engine, support, "conv-old", age=OLD)
    say(engine, support, "conv-new", age=dt.timedelta(days=2))

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"dry_run": True}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["candidate_records"] == 1
    assert body["purged_records"] == 0 and body["purged_traces"] == 0
    assert body["remaining_records"] == 2
    assert body["agents"] == ["Support Bot"]
    assert "Nothing was deleted" in body["message"]
    assert expired["id"] in engine.traces
    assert engine.calls_to("/traces/delete") == []


async def test_a_conversations_expiry_is_its_own_stores_window_or_none(
    admin_client, factory, workspace, engine
):
    """The Conversation State tab promises only what a purge would do.

    It quoted the workspace's tightest conversation store against every thread:
    a 7-day Development store put a 7-day expiry on a Production conversation
    held for 30, and on one that no store — and so no purge — governs at all.
    """
    store, support = await bound(factory, workspace, engine)
    await factory.memory_store(
        workspace,
        name="Scratch memory",
        store_type=MemoryStoreType.CONVERSATION.value,
        environment=EnvironmentType.DEVELOPMENT.value,
        retention_days=7,
        retention_policy="7 days",
    )
    unbound = await factory.provisioned_agent(workspace, engine, name="Billing Bot")
    say(engine, support, "conv-support", age=dt.timedelta(days=1))
    say(engine, unbound, "conv-billing", age=dt.timedelta(days=1))

    page = (await admin_client.get("/api/v1/memory/conversations")).json()
    rows = {row["conversation_id"]: row for row in page["items"]}

    governed = rows["conv-support"]
    assert governed["retention_policy"] == "30 days"
    expires = dt.datetime.fromisoformat(governed["expires_at"].replace("Z", "+00:00"))
    last = dt.datetime.fromisoformat(governed["last_activity_at"].replace("Z", "+00:00"))
    assert expires - last == dt.timedelta(days=30)
    assert rows["conv-billing"]["retention_policy"] is None
    assert rows["conv-billing"]["expires_at"] is None, "no store governs it, so nothing expires it"


async def test_a_purge_is_audited_with_whose_records_it_removed(
    admin_client, factory, workspace, engine, db
):
    store, support = await bound(factory, workspace, engine)
    say(engine, support, "conv-old", age=OLD)
    say(engine, support, "conv-old", age=OLD + dt.timedelta(minutes=5))

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge",
        json={"confirm": store.name, "reason": "quarterly retention run"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["purged_records"], body["purged_traces"]) == (1, 2)
    assert body["remaining_records"] == 0
    assert body["partial"] is False and body["capped"] is False
    assert "1 expired record(s)" in body["message"]

    (row,) = await purge_rows(db, workspace)
    assert row.entity_id == store.id
    assert row.event_metadata["purged_records"] == 1
    assert row.event_metadata["purged_traces"] == 2
    assert row.event_metadata["agents"] == ["Support Bot"]
    assert row.event_metadata["agent_ids"] == [support.id]
    assert "Support Bot" in row.detail and "quarterly retention run" in row.detail


async def test_a_purge_does_not_move_the_stores_concurrency_token(
    admin_client, factory, workspace, engine
):
    """A purge acts on a store; it is not an edit of it."""
    store, support = await bound(factory, workspace, engine)
    say(engine, support, "conv-old", age=OLD)
    before = (await admin_client.get(f"/api/v1/memory/{store.id}")).json()["updated_at"]

    purge = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )
    assert purge.status_code == 200, purge.text

    edit = await admin_client.patch(
        f"/api/v1/memory/{store.id}",
        json={"backend": "Telemetry threads", "expected_updated_at": before},
    )
    assert edit.status_code == 200, edit.text


# ---------------------------------------------------------------------------
# Purge: a failure half way is still on the record
# ---------------------------------------------------------------------------


async def test_a_purge_that_fails_half_way_audits_what_it_deleted(
    admin_client, factory, workspace, engine, db
):
    """Deletions that happened are reported and audited, whatever became of the rest."""
    store, support = await bound(factory, workspace, engine)
    second = await factory.provisioned_agent(
        workspace,
        engine,
        name="Returns Bot",
        memory_policy=store.name,
        last_used_at=utcnow() - dt.timedelta(days=3),
    )
    first_gone = say(engine, support, "conv-a", age=OLD)
    never_reached = say(engine, second, "conv-b", age=OLD)

    async def die_after_the_first_delete(method: str, path: str) -> None:
        if path.endswith("/traces/delete") and len(engine.calls_to("/traces/delete")) > 1:
            engine.fail(503)

    engine.before_dispatch = die_after_the_first_delete

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )
    engine.before_dispatch = None
    engine.recover()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is True
    assert (body["purged_records"], body["purged_traces"]) == (1, 1)
    assert "again" in body["message"]
    assert first_gone["id"] not in engine.traces
    assert never_reached["id"] in engine.traces

    (row,) = await purge_rows(db, workspace)
    assert row.event_metadata["partial"] is True
    assert row.event_metadata["purged_traces"] == 1

    summary = (await admin_client.get("/api/v1/memory/summary")).json()
    assert summary["expired_purged_records"] == 1 and summary["purge_runs"] == 1


async def test_a_purge_that_deleted_nothing_still_fails_closed(
    admin_client, factory, workspace, engine, db
):
    store, support = await bound(factory, workspace, engine)
    expired = say(engine, support, "conv-old", age=OLD)
    engine.fail_path("/traces/delete", 503)

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "telemetry_unavailable"
    assert expired["id"] in engine.traces
    assert await purge_rows(db, workspace) == []


async def test_a_second_purge_of_the_same_store_is_turned_away_while_one_runs(
    admin_client, factory, workspace, engine
):
    """The console gives up at 30 s and the operator clicks again."""
    store, support = await bound(factory, workspace, engine)
    say(engine, support, "conv-old", age=OLD)
    deleting, release = asyncio.Event(), asyncio.Event()

    async def hold_the_delete(method: str, path: str) -> None:
        if path.endswith("/traces/delete"):
            deleting.set()
            await release.wait()

    engine.before_dispatch = hold_the_delete
    url = f"/api/v1/memory/{store.id}/purge"
    first = asyncio.ensure_future(admin_client.post(url, json={"confirm": store.name}))
    await asyncio.wait_for(deleting.wait(), timeout=30)

    second = await admin_client.post(url, json={"confirm": store.name})
    release.set()
    finished = await first
    engine.before_dispatch = None

    assert second.status_code == 409, second.text
    assert finished.status_code == 200, finished.text
    assert finished.json()["purged_records"] == 1

    again = await admin_client.post(url, json={"confirm": store.name})
    assert again.status_code == 200, "the claim is released when the purge ends"


async def test_a_thread_listing_is_paged_by_the_engines_own_cursor(
    admin_client, factory, workspace, engine
):
    """The second page is asked for by ``thread_model_id``, never by the thread's ``id``.

    The engine reads the cursor as a UUID and refuses anything else, so paging
    by the business id turned every project with more than one page of threads
    into a 503.
    """
    store, support = await bound(factory, workspace, engine)
    for index in range(501):
        say(engine, support, f"conv-{index:04d}", age=OLD)

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"dry_run": True}
    )

    assert response.status_code == 200, response.text
    assert response.json()["candidate_records"] == 501
    pages = engine.calls_to("/traces/threads/search")
    assert len(pages) == 2
    assert pages[1].body["last_retrieved_thread_model_id"] not in {
        f"{support.engine_project_id}:conv-{index:04d}" for index in range(501)
    }


# ---------------------------------------------------------------------------
# Backup and restore: say what is held, and what is not
# ---------------------------------------------------------------------------


async def test_a_backup_says_that_it_holds_no_threads(admin_client, factory, workspace, engine):
    store, support = await bound(factory, workspace, engine)
    other = await factory.provisioned_agent(workspace, engine, name="Billing Bot")
    say(engine, support, "conv-a", age=dt.timedelta(days=2))
    say(engine, support, "conv-b", age=dt.timedelta(days=1))
    say(engine, other, "conv-billing", age=dt.timedelta(days=1))

    response = await admin_client.post(f"/api/v1/memory/{store.id}/backup", json={})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["threads_captured"] is False
    assert body["captured"] == ["retention_policy", "retention_days", "status"]
    assert body["record_count"] == 2, "only the store's own agents are counted"
    assert body["agents"] == ["Support Bot"]
    assert body["payload_bytes"] is None, "nothing was exported, so there is no size to report"
    assert "not copied" in body["notice"]
    assert engine.calls_to("/traces/threads/search") == [], "no thread is paged into memory"

    ledger = (await admin_client.get(f"/api/v1/memory/{store.id}/backups")).json()["items"]
    assert [row["id"] for row in ledger] == [body["backup_id"]]
    assert ledger[0]["threads_captured"] is False
    assert ledger[0]["payload_bytes"] is None


async def test_a_restore_puts_back_the_policy_and_says_no_records_came_back(
    admin_client, factory, workspace, engine
):
    """Backup, tighten the policy, purge, restore: the records stay gone and nothing pretends."""
    store, support = await bound(factory, workspace, engine)
    purged = say(engine, support, "conv-old", age=dt.timedelta(days=20))
    backup = (await admin_client.post(f"/api/v1/memory/{store.id}/backup", json={})).json()
    assert backup["record_count"] == 1

    tightened = await admin_client.put(
        f"/api/v1/memory/{store.id}/retention", json={"retention_days": 7}
    )
    assert tightened.status_code == 200, tightened.text
    purge = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
    )
    assert purge.json()["purged_records"] == 1

    response = await admin_client.post(
        f"/api/v1/memory/{store.id}/restore", json={"backup_id": backup["backup_id"]}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fields_restored"] == ["retention_policy"]
    assert body["retention_policy"] == "30 days"
    assert body["threads_restored"] is False
    assert "not restored" in body["notice"]
    assert body["record_count"] == 0, "the count is what the store holds now, not a memory of it"
    assert purged["id"] not in engine.traces

    row = (await admin_client.get(f"/api/v1/memory/{store.id}")).json()
    assert row["retention_days"] == 30
    summary = (await admin_client.get("/api/v1/memory/summary")).json()
    assert summary["stored_memories"] == 0, "a restore must not inflate Stored Memories"


# ---------------------------------------------------------------------------
# Store rows: measured live, or not at all
# ---------------------------------------------------------------------------


async def test_a_thread_backed_stores_records_and_sessions_are_read_live(
    admin_client, factory, workspace, engine
):
    """Nothing ever wrote the counter columns, so the row read 0 / 0% / 0 for ever."""
    store, support = await bound(factory, workspace, engine)
    other = await factory.provisioned_agent(workspace, engine, name="Billing Bot")
    say(engine, support, "conv-today", age=dt.timedelta(hours=1))
    say(engine, support, "conv-last-week", age=dt.timedelta(days=6))
    say(engine, other, "conv-billing", age=dt.timedelta(hours=1))

    page = (await admin_client.get("/api/v1/memory")).json()
    (row,) = page["items"]

    assert row["record_count"] == 2, "the store's own agents' threads, and only theirs"
    assert row["active_session_count"] == 1
    assert row["bound_agents"] == 1
    assert row["usage_percent"] is None, "the telemetry store reports no capacity figure"
    assert row["avg_retrieval_latency_ms"] is None

    summary = (await admin_client.get("/api/v1/memory/summary")).json()
    assert summary["stored_memories"] == 2
    assert summary["active_sessions"] == 2, "Active Sessions stays workspace-wide"
    assert summary["avg_usage_percent"] is None, "no store with a reporter, so no average"

    records = (await admin_client.get(f"/api/v1/memory/{store.id}/records")).json()
    assert records["total"] == 2
    assert {item["agent_name"] for item in records["items"]} == {"Support Bot"}


async def test_the_store_table_answers_without_telemetry_and_shows_no_figure(
    admin_client, factory, workspace, engine
):
    """Our own rows keep working; an unmeasured figure is null, never zero."""
    store, support = await bound(factory, workspace, engine)
    say(engine, support, "conv-today", age=dt.timedelta(hours=1))
    engine.fail(503)

    response = await admin_client.get("/api/v1/memory")

    assert response.status_code == 200, response.text
    (row,) = response.json()["items"]
    assert row["name"] == store.name
    assert row["record_count"] is None and row["active_session_count"] is None
    assert row["bound_agents"] == 1


async def test_a_policys_records_count_its_conversation_stores_live(
    admin_client, factory, workspace, engine
):
    """The Retention Policies tab summed the same never-written column: Records 0."""
    store, support = await bound(factory, workspace, engine)
    await factory.memory_store(
        workspace,
        name="Product embeddings",
        store_type=MemoryStoreType.VECTOR.value,
        retention_days=30,
        retention_policy="30 days",
        record_count=1200,
    )
    say(engine, support, "conv-a", age=dt.timedelta(days=2))
    say(engine, support, "conv-b", age=dt.timedelta(days=1))

    (policy,) = (await admin_client.get("/api/v1/memory/retention-policies")).json()
    assert policy["store_count"] == 2
    assert policy["record_count"] == 1202, "two live threads plus what the vector store reported"

    engine.fail(503)
    (unread,) = (await admin_client.get("/api/v1/memory/retention-policies")).json()
    assert unread["stores"] == ["Product embeddings", "Support memory"], "our rows still answer"
    assert unread["record_count"] is None, "half a sum is not shown as the total"


async def test_a_reported_stores_counters_are_still_what_its_runtime_sent(
    admin_client, factory, workspace, engine
):
    vector = await factory.memory_store(
        workspace, name="Product embeddings", store_type=MemoryStoreType.VECTOR.value
    )

    patched = await admin_client.patch(
        f"/api/v1/memory/{vector.id}", json={"record_count": 1200, "usage_percent": 40}
    )

    assert patched.status_code == 200, patched.text
    assert (patched.json()["record_count"], patched.json()["usage_percent"]) == (1200, 40)
    assert patched.json()["bound_agents"] is None
    assert engine.calls_to("/traces/threads") == [], "a vector store is not in the telemetry store"


async def test_a_store_can_be_deleted_once_no_agent_names_it(
    admin_client, factory, workspace, engine, db
):
    store, support = await bound(factory, workspace, engine)
    kept = say(engine, support, "conv-old", age=OLD)

    refused = await admin_client.delete(f"/api/v1/memory/{store.id}")
    assert refused.status_code == 412, refused.text
    assert "Support Bot" in refused.json()["error"]["message"]

    await db.execute(update(Agent).where(Agent.id == support.id).values(memory_policy=None))
    deleted = await admin_client.delete(f"/api/v1/memory/{store.id}")

    assert deleted.status_code == 204, deleted.text
    assert (await admin_client.get(f"/api/v1/memory/{store.id}")).status_code == 404
    assert kept["id"] in engine.traces, "deleting the registry row deletes no record"
    actions = await db.scalars(
        select(AuditEvent.action).where(AuditEvent.workspace_id == workspace.id)
    )
    assert "memory.store_deleted" in actions


async def test_renaming_a_store_keeps_its_agents_bound(
    admin_client, factory, workspace, engine, db
):
    """The name is the binding, so a rename that left agents behind would unbind them all."""
    store, support = await bound(factory, workspace, engine)
    say(engine, support, "conv-old", age=OLD)

    renamed = await admin_client.patch(
        f"/api/v1/memory/{store.id}", json={"name": "Support conversations"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["bound_agents"] == 1

    assert (await db.get(Agent, support.id)).memory_policy == "Support conversations"
    dry_run = await admin_client.post(
        f"/api/v1/memory/{store.id}/purge", json={"dry_run": True}
    )
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["candidate_records"] == 1


# ---------------------------------------------------------------------------
# Thread counts are shared, addressed by id, and forgotten by a purge
# ---------------------------------------------------------------------------


async def test_thread_counts_are_shared_between_loads_and_forgotten_by_a_purge(
    admin_client, factory, workspace, engine, monkeypatch
):
    from fulcrum_ops_api.core.config import settings
    from fulcrum_ops_api.services import memory as memory_service

    monkeypatch.setattr(settings, "memory_counts_cache_seconds", 60.0)
    memory_service._thread_counts.invalidate()
    try:
        store, support = await bound(factory, workspace, engine)
        say(engine, support, "conv-old", age=OLD)

        first = (await admin_client.get("/api/v1/memory/summary")).json()
        assert first["stored_memories"] == 1
        counted = engine.calls_to("/traces/threads")
        assert counted, "the first load has to ask"
        assert all("project_id" in call.query for call in counted)
        assert not any("project_name" in call.query for call in counted), (
            "a name costs the engine a lookup on every call"
        )

        engine.reset_calls()
        again = (await admin_client.get("/api/v1/memory/summary")).json()
        rows = (await admin_client.get("/api/v1/memory")).json()["items"]
        assert again["stored_memories"] == 1 and rows[0]["record_count"] == 1
        assert engine.calls_to("/traces/threads") == [], "the KPI row and the table share one count"

        purge = await admin_client.post(
            f"/api/v1/memory/{store.id}/purge", json={"confirm": store.name}
        )
        assert purge.json()["remaining_records"] == 0, "a purge never reports a remembered count"
        after = (await admin_client.get("/api/v1/memory")).json()["items"]
        assert after[0]["record_count"] == 0
    finally:
        memory_service._thread_counts.invalidate()
