"""Regression tests for the 2026-09-18 audit of RAG & Knowledge Governance.

Each test drives the screen the way the console does -- Add Source, Sync Now,
the progress poll, the Grounding panel, the View Documents modal -- against the
engine double, and fails without the fix it is named for.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select, update

from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.registry import KnowledgeSource, KnowledgeSourceStatus


def iso(minutes_ago: float = 3.0) -> str:
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
    return moment.isoformat().replace("+00:00", "Z")


def retrieval_span(
    engine,
    project_name: str,
    index: str,
    *,
    documents: list[dict[str, Any]] | None = None,
    scores: dict[str, float] | None = None,
    minutes_ago: float = 3.0,
    **fields: Any,
) -> dict[str, Any]:
    """One retrieval span the way an SDK reports it: the index it read from in
    ``metadata``, the documents it got back on ``output``, and whatever an
    online judge scored it with."""
    return engine._store_span(
        {
            "project_name": project_name,
            "name": "retrieve",
            "type": "tool",
            "start_time": iso(minutes_ago),
            "end_time": iso(minutes_ago),
            "metadata": {"index_name": index},
            "output": {"documents": documents or []},
            "feedback_scores": [
                {"name": name, "value": value} for name, value in (scores or {}).items()
            ],
            **fields,
        }
    )


async def wait_for_sync(http, source_id: str) -> dict[str, Any]:
    """Poll the progress bar's endpoint until the job has let go of the row."""
    status: dict[str, Any] = {}
    for _ in range(200):
        polled = await http.get(f"/api/v1/knowledge/{source_id}/sync-status")
        assert polled.status_code == 200, polled.text
        status = polled.json()
        if not status["running"]:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"sync never finished: {status}")


# ---------------------------------------------------------------------------
# Finding 23 -- Add Source with the default "start the first sync now"
# ---------------------------------------------------------------------------


async def test_adding_a_source_with_the_default_first_sync_creates_it_and_syncs(
    admin_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    retrieval_span(
        engine,
        agent.engine_project_name,
        "policies-v1",
        documents=[{"document_id": "doc-1", "title": "Refund policy", "chunk_id": "c1"}],
    )

    # The console's Add Source dialog, box left ticked: start_sync is not sent.
    created = await admin_client.post(
        "/api/v1/knowledge",
        json={"name": "Policies", "source_type": "SharePoint", "index_name": "policies-v1"},
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"]["status"] == KnowledgeSourceStatus.SYNCING.value
    assert body["sync"]["state"] == "Queued"
    assert body["sync"]["started_at"] is not None

    finished = await wait_for_sync(admin_client, body["source"]["id"])
    assert finished["state"] == "Completed", finished
    assert finished["status"] == KnowledgeSourceStatus.ACTIVE.value
    assert finished["progress"] == 100
    assert finished["documents_seen"] == 1
    # The job ran from the committed row, so it kept who started it and when.
    assert finished["started_at"] is not None

    row = await db.get(KnowledgeSource, body["source"]["id"])
    assert row is not None and row.status == KnowledgeSourceStatus.ACTIVE.value


async def test_adding_a_source_without_the_first_sync_leaves_it_paused(admin_client):
    created = await admin_client.post(
        "/api/v1/knowledge",
        json={
            "name": "Handbook",
            "source_type": "SharePoint",
            "index_name": "handbook-v1",
            "start_sync": False,
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"]["status"] == KnowledgeSourceStatus.PAUSED.value
    assert body["source"]["sync_progress"] == 100
    assert body["sync"]["running"] is False
    assert body["sync"]["state"] is None


# ---------------------------------------------------------------------------
# Finding 26 -- the retrieval-span scan
# ---------------------------------------------------------------------------


async def test_one_scan_answers_every_source_and_every_page_of_the_documents_modal(
    admin_client, factory, workspace, engine, monkeypatch
):
    """Clicking down the table used to repeat the whole fan-out per click, and
    paging the documents modal repeated it per page. What the store is asked
    does not depend on the source, so it is asked once."""
    from fulcrum_ops_api.core.config import settings
    from fulcrum_ops_api.services import knowledge as service

    monkeypatch.setattr(settings, "knowledge_scan_cache_seconds", 60.0)
    service.forget_scans()

    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    policies = await factory.knowledge_source(workspace, name="Policies", index_name="policies-v1")
    handbook = await factory.knowledge_source(workspace, name="Handbook", index_name="handbook-v1")
    retrieval_span(
        engine,
        agent.engine_project_name,
        "policies-v1",
        documents=[
            {"document_id": f"doc-{n}", "title": f"Policy {n}", "chunk_id": f"c{n}"}
            for n in range(3)
        ],
        scores={"Context Relevance": 0.8},
    )
    retrieval_span(
        engine,
        agent.engine_project_name,
        "handbook-v1",
        documents=[{"document_id": "hb-1", "title": "Handbook", "chunk_id": "c1"}],
        scores={"Context Relevance": 0.4},
    )

    try:
        first = await admin_client.get(f"/api/v1/knowledge/{policies.id}/grounding")
        second = await admin_client.get(f"/api/v1/knowledge/{handbook.id}/grounding")
        page_one = await admin_client.get(
            f"/api/v1/knowledge/{policies.id}/documents", params={"page": 1, "page_size": 2}
        )
        page_two = await admin_client.get(
            f"/api/v1/knowledge/{policies.id}/documents", params={"page": 2, "page_size": 2}
        )
    finally:
        service.forget_scans()

    assert first.status_code == second.status_code == 200
    assert page_one.status_code == page_two.status_code == 200
    # Each source still sees only its own spans...
    assert first.json()["dimensions"][0]["score"] == 0.8
    assert second.json()["dimensions"][0]["score"] == 0.4
    assert page_one.json()["total"] == 3
    assert len(page_one.json()["items"]) == 2 and len(page_two.json()["items"]) == 1
    # ...and the store was asked once for all four of them.
    assert len(engine.calls_to("spans/search")) == 1


async def test_a_sync_reads_the_store_afresh_and_leaves_its_scan_for_the_screen(
    admin_client, factory, workspace, engine, monkeypatch
):
    from fulcrum_ops_api.core.config import settings
    from fulcrum_ops_api.services import knowledge as service

    monkeypatch.setattr(settings, "knowledge_scan_cache_seconds", 60.0)
    service.forget_scans()

    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    source = await factory.knowledge_source(workspace, name="Policies", index_name="policies-v1")

    try:
        before = await admin_client.get(f"/api/v1/knowledge/{source.id}/documents")
        assert before.json()["total"] == 0

        # Telemetry arrives after the screen's scan was remembered.
        retrieval_span(
            engine,
            agent.engine_project_name,
            "policies-v1",
            documents=[{"document_id": "doc-1", "title": "Refund policy", "chunk_id": "c1"}],
        )
        started = await admin_client.post(f"/api/v1/knowledge/{source.id}/sync")
        assert started.status_code == 202, started.text
        finished = await wait_for_sync(admin_client, source.id)
        assert finished["state"] == "Completed", finished
        assert finished["documents_seen"] == 1  # not the remembered, empty scan

        engine.reset_calls()
        after = await admin_client.get(f"/api/v1/knowledge/{source.id}/documents")
        assert after.json()["total"] == 1
        assert engine.calls_to("spans/search") == []  # served from the sync's scan
    finally:
        service.forget_scans()


async def test_the_scan_follows_the_cursor_past_the_newest_page(
    admin_client, factory, workspace, engine
):
    """The store streams a project's spans newest first, whatever their type.
    One page of a busy agent was all model calls, and a source whose retrieval
    spans sat behind them read "Not measured yet" with the telemetry in hand."""
    from fulcrum_ops_api.services import knowledge as service

    agent = await factory.provisioned_agent(workspace, engine, name="Busy Bot")
    source = await factory.knowledge_source(workspace, name="Policies", index_name="policies-v1")
    # Ids sort the way the store's time-ordered ids do: the retrieval span is
    # the oldest row in the project, behind more than a page of model calls.
    retrieval_span(
        engine,
        agent.engine_project_name,
        "policies-v1",
        documents=[{"document_id": "doc-1", "title": "Refund policy", "chunk_id": "c1"}],
        scores={"Faithfulness": 0.9},
        id="00000000-0000-7000-8000-000000000000",
    )
    for n in range(service.SPAN_PAGE_SIZE + 5):
        engine._store_span(
            {
                "id": f"ffffffff-0000-7000-8000-{n:012d}",
                "project_name": agent.engine_project_name,
                "name": "chat",
                "type": "llm",
                "start_time": iso(1),
                "end_time": iso(1),
            }
        )

    grounding = await admin_client.get(f"/api/v1/knowledge/{source.id}/grounding")

    assert grounding.status_code == 200, grounding.text
    assert grounding.json()["spans_sampled"] == 1
    assert grounding.json()["overall"] == 0.9
    searches = engine.calls_to("spans/search")
    assert len(searches) == 2
    assert searches[1].body["last_retrieved_id"]


async def test_a_store_failure_part_way_through_the_stream_is_an_outage_not_an_empty_source(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    source = await factory.knowledge_source(workspace, name="Policies", index_name="policies-v1")
    retrieval_span(
        engine,
        agent.engine_project_name,
        "policies-v1",
        documents=[{"document_id": "doc-1", "title": "Refund policy", "chunk_id": "c1"}],
    )
    engine.span_stream_error = {"code": 500, "message": "Memory limit exceeded"}

    grounding = await admin_client.get(f"/api/v1/knowledge/{source.id}/grounding")
    documents = await admin_client.get(f"/api/v1/knowledge/{source.id}/documents")

    assert grounding.status_code == 503, grounding.text
    assert documents.status_code == 503, documents.text

    # The sync job must not publish "Completed, 0 documents" either.
    started = await admin_client.post(f"/api/v1/knowledge/{source.id}/sync")
    assert started.status_code == 202, started.text
    finished = await wait_for_sync(admin_client, source.id)
    assert finished["state"] == "Failed", finished
    assert finished["status"] == KnowledgeSourceStatus.FAILED.value


async def test_the_scan_prefers_the_agents_that_reported_most_recently(
    admin_client, factory, workspace, engine
):
    """Only so many projects are read. Which ones used to be whatever order the
    database returned rows in; a quiet agent is the right one to leave out."""
    from fulcrum_ops_api.services import knowledge as service

    for n in range(service.MAX_SCAN_PROJECTS):
        await factory.provisioned_agent(workspace, engine, name=f"Quiet {n:02d}")
    busy = await factory.provisioned_agent(
        workspace, engine, name="Zed Busy Bot", last_used_at=dt.datetime.now(dt.UTC)
    )
    source = await factory.knowledge_source(workspace, name="Policies", index_name="policies-v1")
    retrieval_span(
        engine,
        busy.engine_project_name,
        "policies-v1",
        documents=[{"document_id": "doc-1", "title": "Refund policy", "chunk_id": "c1"}],
    )

    documents = await admin_client.get(f"/api/v1/knowledge/{source.id}/documents")

    assert documents.status_code == 200, documents.text
    assert documents.json()["total"] == 1
    scanned = {call.body["project_name"] for call in engine.calls_to("spans/search")}
    assert busy.engine_project_name in scanned
    assert len(scanned) == service.MAX_SCAN_PROJECTS


# ---------------------------------------------------------------------------
# Finding 25 -- a source stranded in Syncing
# ---------------------------------------------------------------------------


async def stranded_source(factory, db, workspace, *, minutes_ago: float, name: str = "Policies"):
    """A row the way a worker restart leaves it: Syncing at 40%, job gone, last
    progress ``minutes_ago`` old."""
    then = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
    source = await factory.knowledge_source(
        workspace,
        name=name,
        index_name="policies-v1",
        status=KnowledgeSourceStatus.SYNCING.value,
        sync_progress=40,
        retrieval_policy={
            "sync": {
                "job_id": "job-that-died",
                "stage": "Reading retrieval telemetry",
                "stage_index": 2,
                "state": "Running",
                "started_at": then.isoformat(),
            }
        },
    )
    await db.execute(
        update(KnowledgeSource).where(KnowledgeSource.id == source.id).values(updated_at=then)
    )
    return source


async def test_the_progress_poll_closes_out_a_sync_whose_job_is_gone(
    admin_client, factory, workspace, db
):
    source = await stranded_source(factory, db, workspace, minutes_ago=30)

    polled = await admin_client.get(f"/api/v1/knowledge/{source.id}/sync-status")

    assert polled.status_code == 200, polled.text
    body = polled.json()
    assert body["running"] is False
    assert body["status"] == KnowledgeSourceStatus.FAILED.value
    assert body["state"] == "Failed"
    assert "Interrupted" in body["error"]
    row = await db.get(KnowledgeSource, source.id)
    assert row.status == KnowledgeSourceStatus.FAILED.value
    trail = await db.scalars(
        select(AuditEvent).where(
            AuditEvent.entity_id == source.id,
            AuditEvent.action == "knowledge_source.sync_failed",
        )
    )
    # Signed by the sync job's own identity, not by whoever happened to poll.
    assert len(trail) == 1 and trail[0].actor == "api-key:knowledge-sync"


async def test_sync_now_and_delete_are_exits_from_a_stranded_sync(
    admin_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    retrieval_span(
        engine,
        agent.engine_project_name,
        "policies-v1",
        documents=[{"document_id": "doc-1", "title": "Refund policy", "chunk_id": "c1"}],
    )
    restarted = await stranded_source(factory, db, workspace, minutes_ago=30, name="Policies")
    removed = await stranded_source(factory, db, workspace, minutes_ago=30, name="Handbook")

    started = await admin_client.post(f"/api/v1/knowledge/{restarted.id}/sync")
    assert started.status_code == 202, started.text
    finished = await wait_for_sync(admin_client, restarted.id)
    assert finished["state"] == "Completed", finished
    assert finished["documents_seen"] == 1

    deleted = await admin_client.delete(f"/api/v1/knowledge/{removed.id}")
    assert deleted.status_code == 204, deleted.text


async def test_a_sync_that_may_still_be_running_elsewhere_is_left_alone(
    admin_client, factory, workspace, db
):
    """Another worker's job is invisible from here. Inside its own lifetime a
    Syncing row is taken at its word: 409, 412, and a poll that changes nothing."""
    source = await stranded_source(factory, db, workspace, minutes_ago=1)

    polled = await admin_client.get(f"/api/v1/knowledge/{source.id}/sync-status")
    started = await admin_client.post(f"/api/v1/knowledge/{source.id}/sync")
    deleted = await admin_client.delete(f"/api/v1/knowledge/{source.id}")

    assert polled.json()["running"] is True
    assert polled.json()["progress"] == 40
    assert started.status_code == 409, started.text
    assert deleted.status_code == 412, deleted.text


async def test_the_sweep_closes_out_stranded_syncs_nobody_is_looking_at(
    admin_client, factory, workspace, db
):
    from fulcrum_ops_api.services import knowledge as service

    gone = await stranded_source(factory, db, workspace, minutes_ago=30, name="Policies")
    live = await stranded_source(factory, db, workspace, minutes_ago=1, name="Handbook")

    assert await service.fail_stranded_syncs() == 1

    listed = await admin_client.get("/api/v1/knowledge", params={"page_size": 50})
    by_id = {row["id"]: row for row in listed.json()["items"]}
    assert by_id[gone.id]["status"] == KnowledgeSourceStatus.FAILED.value
    assert "Interrupted" in by_id[gone.id]["sync"]["error"]
    assert by_id[live.id]["status"] == KnowledgeSourceStatus.SYNCING.value


async def test_a_sync_the_store_never_answers_fails_instead_of_syncing_for_ever(
    admin_client, factory, workspace, engine, monkeypatch
):
    from fulcrum_ops_api.services import knowledge as service

    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    source = await factory.knowledge_source(workspace, name="Policies", index_name="policies-v1")
    monkeypatch.setattr(service, "EXECUTION_DEADLINE_SECONDS", 0.2)
    release = asyncio.Event()

    async def hang(_method: str, path: str) -> None:
        if "spans/search" in path:
            await release.wait()

    engine.before_dispatch = hang
    try:
        started = await admin_client.post(f"/api/v1/knowledge/{source.id}/sync")
        assert started.status_code == 202, started.text
        finished = await wait_for_sync(admin_client, source.id)
    finally:
        release.set()

    assert finished["state"] == "Failed", finished
    assert finished["status"] == KnowledgeSourceStatus.FAILED.value
    assert "did not answer" in finished["error"]


# ---------------------------------------------------------------------------
# Finding 117 -- which way round a grounding score reads
# ---------------------------------------------------------------------------


async def grounding_of(admin_client, factory, workspace, engine, scores: dict[str, float]):
    agent = await factory.provisioned_agent(workspace, engine)
    source = await factory.knowledge_source(workspace, index_name="policies-v1")
    retrieval_span(engine, agent.engine_project_name, "policies-v1", scores=scores)
    answered = await admin_client.get(f"/api/v1/knowledge/{source.id}/grounding")
    assert answered.status_code == 200, answered.text
    return answered.json()


async def test_a_source_that_never_hallucinates_is_fully_grounded(
    admin_client, factory, workspace, engine
):
    # A hallucination judge scores 0.0 for "did not hallucinate".
    body = await grounding_of(admin_client, factory, workspace, engine, {"Hallucination": 0.0})

    assert body["overall"] == 1.0
    assert body["measured"] is True


async def test_a_source_that_always_hallucinates_is_measured_as_ungrounded(
    admin_client, factory, workspace, engine
):
    body = await grounding_of(admin_client, factory, workspace, engine, {"hallucination": 1.0})

    assert body["overall"] == 0.0
    # The worst score there is, and still a score: not "Not measured yet".
    assert body["measured"] is True


async def test_the_score_name_the_user_guide_teaches_is_recognised(
    admin_client, factory, workspace, engine
):
    body = await grounding_of(admin_client, factory, workspace, engine, {"grounded": 0.75})

    assert body["overall"] == 0.75
    assert body["measured"] is True


async def test_a_source_nothing_scored_is_still_not_measured(
    admin_client, factory, workspace, engine
):
    body = await grounding_of(admin_client, factory, workspace, engine, {})

    assert body["overall"] is None
    assert body["measured"] is False
    assert body["spans_sampled"] == 1


async def test_a_sync_publishes_the_corrected_grounding_score(
    admin_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine)
    source = await factory.knowledge_source(
        workspace, index_name="policies-v1", grounding_score=0.0
    )
    retrieval_span(engine, agent.engine_project_name, "policies-v1", scores={"Hallucination": 0.1})

    started = await admin_client.post(f"/api/v1/knowledge/{source.id}/sync")
    assert started.status_code == 202, started.text
    await wait_for_sync(admin_client, source.id)

    row = await db.get(KnowledgeSource, source.id)
    assert row.grounding_score == 0.9


# ---------------------------------------------------------------------------
# Finding 210 -- where a corpus lives is not always an http URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "location"),
    [
        ("File Share", "Z:\\Policies\\Underwriting"),
        ("File Share", "\\\\fileserver\\policies"),
        ("Blob Storage", "wasbs://docs@account.blob.core.windows.net/policies"),
        ("Blob Storage", "gs://bucket/policies"),
        ("SharePoint", "sharepoint://sites/underwriting"),
        ("Database", "jdbc:postgresql://db.internal:5432/policies"),
        ("Vector Index", "localhost:9200/policies-v1"),
        ("Web", "https://docs.example.com/policies"),
    ],
)
async def test_a_location_of_any_source_type_the_dialog_offers_is_accepted(
    admin_client, source_type, location
):
    created = await admin_client.post(
        "/api/v1/knowledge",
        json={
            "name": "Policies",
            "source_type": source_type,
            "settings": {"location": location},
            "start_sync": False,
        },
    )
    assert created.status_code == 201, created.text
    source = created.json()["source"]
    assert source["settings"]["location"] == location

    # Edit re-sends the stored location on every save.
    saved = await admin_client.patch(
        f"/api/v1/knowledge/{source['id']}",
        json={"settings": {"location": location, "description": "Underwriting policies"}},
    )
    assert saved.status_code == 200, saved.text


@pytest.mark.parametrize(
    "location",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,x",
    ],
)
async def test_a_location_a_browser_would_run_is_still_refused(admin_client, location):
    created = await admin_client.post(
        "/api/v1/knowledge",
        json={
            "name": "Policies",
            "source_type": "Web",
            "settings": {"location": location},
            "start_sync": False,
        },
    )
    assert created.status_code == 422, created.text
