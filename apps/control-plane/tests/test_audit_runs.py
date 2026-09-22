"""Regressions for the run screens, from the September 2026 audit.

Each test here failed against the code it was written for. They share one
subject -- the mapping layer between the telemetry store's traces and the run
the console renders -- and four ways it went wrong in production:

* a key bound to one agent could still read its neighbours' runs by id;
* Replay Studio's history browser was the one search with no time bound, and
  timed out on every agent with a real history;
* the KPI row compared two windows read under different caps, and printed the
  difference as a trend;
* a run reported without spans came back as an empty shell from the trace and
  replay reads, although the run itself carried the prompt, the answer and the
  error.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx
import pytest

from conftest import APP_BASE_URL


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@pytest.fixture
async def bound_client(app, factory, workspace, engine) -> AsyncIterator[httpx.AsyncClient]:
    """A caller holding a key bound to one agent -- what a deployed runtime has.

    ``client.mine`` is the agent the key belongs to and ``client.theirs`` is a
    neighbour in the same workspace.
    """
    mine = await factory.provisioned_agent(workspace, engine, name="Claims Bot")
    theirs = await factory.provisioned_agent(workspace, engine, name="Payroll Bot")
    token, _row = await factory.api_key(
        workspace, name="Claims runtime", scopes=["ingest", "read"], agent_id=mine.id
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.mine = mine  # type: ignore[attr-defined]
        http.theirs = theirs  # type: ignore[attr-defined]
        yield http


# ---------------------------------------------------------------------------
# A key bound to one agent reads that agent's runs and no others
# ---------------------------------------------------------------------------


async def test_a_bound_key_lists_only_its_own_agents_runs(bound_client, engine):
    engine.add_trace(project_name=bound_client.mine.engine_project_name, name="mine")
    engine.add_trace(project_name=bound_client.theirs.engine_project_name, name="theirs")
    engine.reset_calls()

    listed = await bound_client.get("/api/v1/runs", params={"status": "Completed"})
    asked_for_neighbour = await bound_client.get(
        "/api/v1/runs", params={"agent_id": bound_client.theirs.id}
    )

    assert listed.status_code == 200, listed.text
    assert [row["agent"] for row in listed.json()["items"]] == ["Claims Bot"]
    assert asked_for_neighbour.json()["total"] == 0
    # The point of the scoping for the store: the runtime's poll costs one
    # project's search, not one per project in the workspace.
    searched = {call.body["project_id"] for call in engine.calls_to("/traces/search")}
    assert searched == {bound_client.mine.engine_project_id}


async def test_a_bound_key_cannot_read_a_neighbours_run_by_id(bound_client, engine):
    """The table was scoped; the reads that take a run id were not."""
    mine = engine.add_trace(
        project_name=bound_client.mine.engine_project_name, input={"question": "mine"}
    )
    theirs = engine.add_trace(
        project_name=bound_client.theirs.engine_project_name,
        input={"question": "What is Dana's salary?"},
        output={"answer": "It is 91,000."},
    )

    for suffix in ("", "/response", "/trace", "/replay"):
        allowed = await bound_client.get(f"/api/v1/runs/{mine['id']}{suffix}")
        refused = await bound_client.get(f"/api/v1/runs/{theirs['id']}{suffix}")
        assert allowed.status_code == 200, (suffix, allowed.text)
        assert refused.status_code == 404, (suffix, refused.text)
        assert "salary" not in refused.text

    flagged = await bound_client.post(f"/api/v1/runs/{theirs['id']}/flag", json={})
    assert flagged.status_code == 404, flagged.text


async def test_a_bound_key_cannot_browse_a_neighbours_history(bound_client, engine):
    engine.add_trace(project_name=bound_client.mine.engine_project_name)
    engine.add_trace(project_name=bound_client.theirs.engine_project_name)

    allowed = await bound_client.get(
        "/api/v1/runs/history", params={"agent_id": bound_client.mine.id}
    )
    refused = await bound_client.get(
        "/api/v1/runs/history", params={"agent_id": bound_client.theirs.id}
    )
    stream = await bound_client.get(
        "/api/v1/runs/stream", params={"agent_id": bound_client.theirs.id}
    )

    assert allowed.status_code == 200, allowed.text
    assert len(allowed.json()["items"]) == 1
    assert refused.status_code == 404, refused.text
    assert stream.status_code == 404, stream.text


# ---------------------------------------------------------------------------
# Replay Studio's history browser never searches without a time bound
# ---------------------------------------------------------------------------


def _v7(at: dt.datetime, serial: int = 0) -> str:
    """A version-7 id minted at ``at``, the way the SDKs and ingest mint them."""
    stamp = f"{int(at.timestamp() * 1000):012x}"
    return f"{stamp[:8]}-{stamp[8:]}-7000-8000-{serial:012d}"


def _add_run(engine, agent, at: dt.datetime, serial: int = 0, **fields):
    return engine.add_trace(
        project_name=agent.engine_project_name,
        trace_id=_v7(at, serial),
        start_time=at,
        end_time=at + dt.timedelta(seconds=2),
        **fields,
    )


async def _walk_history(http, agent, *, limit: int) -> tuple[list[str], int]:
    seen: list[str] = []
    cursor, pages = None, 0
    while True:
        params = {"agent_id": agent.id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await http.get("/api/v1/runs/history", params=params)
        assert response.status_code == 200, response.text
        seen.extend(run["id"] for run in response.json()["items"])
        pages += 1
        cursor = response.json()["next_cursor"]
        if cursor is None or pages > 20:
            return seen, pages


async def test_history_never_asks_the_store_an_unbounded_question(
    admin_client, factory, workspace, engine
):
    """Six of six production calls died at the adapter's 30 s timeout: this was
    the one search that named no window, so the store worked through the whole
    project to return fifty rows."""
    agent = await factory.provisioned_agent(
        workspace, engine, name="Support Bot",
        created_at=_utcnow() - dt.timedelta(days=400),
    )
    now = _utcnow()
    for index in range(7):
        _add_run(engine, agent, now - dt.timedelta(minutes=index), index)
    engine.reset_calls()

    seen, pages = await _walk_history(admin_client, agent, limit=3)

    assert len(seen) == 7 and len(set(seen)) == 7
    assert pages == 3
    searches = engine.calls_to("/traces/search")
    assert searches, "the history is read from the store"
    for call in searches:
        assert call.body.get("to_time"), f"unbounded search: {call.body}"
    # A busy agent's page comes from the first, narrowest search alone.
    first = searches[0].body
    reach = dt.datetime.fromisoformat(
        first["to_time"].replace("Z", "+00:00")
    ) - dt.datetime.fromisoformat(first["from_time"].replace("Z", "+00:00"))
    assert reach == dt.timedelta(days=1)
    assert [call.body.get("last_retrieved_id") for call in searches[:2]] == [None, seen[2]]


async def test_a_deep_history_page_is_bounded_by_where_the_last_one_ended(
    admin_client, factory, workspace, engine
):
    """Paging a long way back must not get slower: each page's search is capped
    at the instant the previous page ended, not at now."""
    agent = await factory.provisioned_agent(
        workspace, engine, name="Support Bot",
        created_at=_utcnow() - dt.timedelta(days=400),
    )
    now = _utcnow()
    expected = [
        _add_run(engine, agent, now - dt.timedelta(days=20 * index, hours=1), index)["id"]
        for index in range(7)
    ]
    engine.reset_calls()

    seen, pages = await _walk_history(admin_client, agent, limit=3)

    assert seen == expected, "newest first, nothing skipped, nothing repeated"
    assert pages == 3
    resumed = [
        call.body for call in engine.calls_to("/traces/search")
        if call.body.get("last_retrieved_id") == expected[2]
    ]
    assert resumed
    for body in resumed:
        upper = dt.datetime.fromisoformat(body["to_time"].replace("Z", "+00:00"))
        assert upper < now - dt.timedelta(days=39), body


async def test_a_quiet_agents_old_runs_are_still_reachable(
    admin_client, factory, workspace, engine
):
    """Bounding every search must not put a floor under the history: a run from
    three years ago is still one page away."""
    agent = await factory.provisioned_agent(
        workspace, engine, name="Archive Bot",
        created_at=_utcnow() - dt.timedelta(days=1200),
    )
    now = _utcnow()
    recent = _add_run(engine, agent, now - dt.timedelta(days=200), 1)
    ancient = _add_run(engine, agent, now - dt.timedelta(days=1100), 2)

    response = await admin_client.get(
        "/api/v1/runs/history", params={"agent_id": agent.id}
    )

    assert response.status_code == 200, response.text
    assert [run["id"] for run in response.json()["items"]] == [recent["id"], ancient["id"]]
    assert response.json()["next_cursor"] is None


# ---------------------------------------------------------------------------
# The KPI row compares like with like, or does not compare
# ---------------------------------------------------------------------------


def _fill(engine, agent, count: int, *, newest: dt.datetime, every: dt.timedelta) -> None:
    for index in range(count):
        at = newest - every * index
        engine.add_trace(
            project_name=agent.engine_project_name,
            start_time=at,
            end_time=at + dt.timedelta(seconds=1),
        )


async def test_a_steady_agent_shows_no_trend(admin_client, factory, workspace, engine):
    """1,200 runs an hour, every hour. The earlier window was read under half
    the cap of the current one, so it came back as 1,000 and the card read
    "+20% vs previous period" for traffic that had not moved."""
    agent = await factory.provisioned_agent(workspace, engine, name="Steady Bot")
    now = _utcnow()
    hour = dt.timedelta(hours=1)
    _fill(engine, agent, 1200, newest=now - dt.timedelta(minutes=1), every=hour / 1300)
    _fill(engine, agent, 1200, newest=now - hour - dt.timedelta(minutes=1), every=hour / 1300)

    response = await admin_client.get(
        "/api/v1/runs/summary", params={"time_range": "Last hour"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_runs"] == 1200
    assert body["total_runs_previous"] == 1200
    assert body["comparable"] is True
    assert body["total_runs_delta_percent"] == 0.0
    assert body["previous_scan"]["truncated"] is False


async def test_a_capped_window_withholds_the_trend_and_says_why(
    admin_client, factory, workspace, engine
):
    """The earlier window's cap used to be thrown away: 2,100 runs read as 1,000,
    and a quiet hour after a busy one printed a precise-looking "-90%"."""
    agent = await factory.provisioned_agent(workspace, engine, name="Bursty Bot")
    now = _utcnow()
    hour = dt.timedelta(hours=1)
    _fill(engine, agent, 100, newest=now - dt.timedelta(minutes=1), every=hour / 200)
    _fill(engine, agent, 2100, newest=now - hour - dt.timedelta(minutes=1), every=hour / 2200)

    response = await admin_client.get(
        "/api/v1/runs/summary", params={"time_range": "Last hour"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_runs"] == 100
    assert body["scan"]["truncated"] is False
    assert body["previous_scan"]["truncated"] is True
    assert body["previous_scan"]["runs_scanned"] == 2000
    assert body["comparable"] is False
    # Every delta folded from the scan is withheld. Policy violations are no
    # longer among them: they are counted from the violation records, whole,
    # and test_live_runs_governance_counts proves their delta survives a cap.
    for delta in (
        "total_runs_delta_percent",
        "success_rate_delta_points",
        "avg_latency_delta_seconds",
    ):
        assert body[delta] is None, delta


async def test_the_table_says_when_its_total_is_a_floor(
    admin_client, factory, workspace, engine
):
    """The KPI row carried the scan it was computed from; the table beside it
    printed "2,000 runs" with nothing to say that 2,000 was where it stopped."""
    agent = await factory.provisioned_agent(workspace, engine, name="Busy Bot")
    now = _utcnow()
    hour = dt.timedelta(hours=1)
    _fill(engine, agent, 2050, newest=now - dt.timedelta(minutes=1), every=hour / 2200)

    capped = await admin_client.get("/api/v1/runs", params={"time_range": "Last hour"})

    assert capped.status_code == 200, capped.text
    assert capped.json()["total"] == 2000
    assert capped.json()["scan"]["truncated"] is True
    assert capped.json()["scan"]["runs_scanned"] == 2000


async def test_a_capped_scan_says_from_when_it_is_whole(
    admin_client, factory, workspace, engine
):
    """A capped scan holds each busy project's newest rows, so the early part of
    the window is only partly read: the sparklines under the KPI row drew those
    hours as zero and the rest as a spike. "A floor" did not say which part of
    the window the figures describe; ``covered_from`` does."""
    busy = await factory.provisioned_agent(workspace, engine, name="Busy Bot")
    quiet = await factory.provisioned_agent(workspace, engine, name="Quiet Bot")
    now = _utcnow()
    hour = dt.timedelta(hours=1)
    newest = now - dt.timedelta(minutes=1)
    every = hour / 2200
    # Ids minted when the run began, as the SDKs mint them: the store hands rows
    # back in id order, and "newest" has to mean the same thing in both.
    for index in range(2050):
        _add_run(engine, busy, newest - every * index, index)
    for index in range(3):
        _add_run(engine, quiet, newest - (hour / 4) * index, 9000 + index)
    # Two projects share the row budget, so the busy one is read 1,000 deep.
    oldest_read = newest - every * 999

    table = await admin_client.get("/api/v1/runs", params={"time_range": "Last hour"})
    summary = await admin_client.get(
        "/api/v1/runs/summary", params={"time_range": "Last hour"}
    )
    whole = await admin_client.get(
        "/api/v1/runs", params={"time_range": "Last hour", "agent_id": quiet.id}
    )

    for response in (table, summary, whole):
        assert response.status_code == 200, response.text
    for scan in (table.json()["scan"], summary.json()["scan"]):
        assert scan["truncated"] is True
        covered = dt.datetime.fromisoformat(scan["covered_from"].replace("Z", "+00:00"))
        assert abs((covered - oldest_read).total_seconds()) < 0.5, (covered, oldest_read)
    # Nothing was capped, so there is no boundary to report -- not the window's
    # start dressed up as one.
    assert whole.json()["scan"]["truncated"] is False
    assert whole.json()["scan"]["covered_from"] is None
    assert summary.json()["previous_scan"]["covered_from"] is None


async def test_a_view_of_one_agent_is_not_reported_as_capped(
    admin_client, factory, workspace, engine
):
    """Narrowed to one agent, the scan was still measured against every agent in
    the workspace -- "1 of 2 agents, capped" -- so the view's totals were marked
    a floor and, once a capped window stopped producing trends, it had none."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.provisioned_agent(workspace, engine, name="Payroll Bot")
    now = _utcnow()
    engine.add_trace(project_name=agent.engine_project_name, start_time=now)
    for minutes in (70, 80):
        engine.add_trace(
            project_name=agent.engine_project_name,
            start_time=now - dt.timedelta(minutes=minutes),
        )
    params = {"time_range": "Last hour", "agent_id": agent.id}

    table = await admin_client.get("/api/v1/runs", params=params)
    summary = await admin_client.get("/api/v1/runs/summary", params=params)

    assert table.json()["scan"]["truncated"] is False
    assert (table.json()["scan"]["agents_scanned"], table.json()["scan"]["agents_total"]) == (1, 1)
    assert summary.json()["scan"]["truncated"] is False
    assert summary.json()["comparable"] is True
    assert summary.json()["total_runs_delta_percent"] == -50.0


async def test_a_remembered_row_that_has_left_the_window_is_not_shown(
    admin_client, factory, workspace, engine, monkeypatch
):
    """Rows are shared between requests for a few seconds, read for the window
    of whoever asked first. Whoever asks next keeps only what is inside theirs;
    the same check is what stops a store that ignores the time bound from
    filling "Last hour" -- and "previous period" -- with whatever is newest."""
    from fulcrum_ops_api.core.config import settings

    # Long enough that both requests below share one remembered read.
    monkeypatch.setattr(settings, "runs_scan_cache_seconds", 864_000.0)

    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    now = _utcnow()
    engine.add_trace(project_name=agent.engine_project_name, start_time=now, name="inside")
    leaving = now - dt.timedelta(hours=1) + dt.timedelta(seconds=3)
    engine.add_trace(project_name=agent.engine_project_name, start_time=leaving, name="leaving")

    first = await admin_client.get("/api/v1/runs", params={"time_range": "Last hour"})
    assert first.json()["total"] == 2, "both runs are inside the first window"
    engine.reset_calls()

    # Wait for the older run to slide out of "Last hour".
    left_at = leaving + dt.timedelta(hours=1)
    await asyncio.sleep(max(0.0, (left_at - _utcnow()).total_seconds()) + 0.2)
    second = await admin_client.get("/api/v1/runs", params={"time_range": "Last hour"})

    assert engine.calls_to("/traces/search") == [], "the second read was the remembered one"
    assert second.json()["total"] == 1
    assert second.json()["items"][0]["occurred_at"].startswith(now.strftime("%Y-%m-%dT%H:%M"))


# ---------------------------------------------------------------------------
# A run with no spans is still a run
# ---------------------------------------------------------------------------


async def test_a_run_without_spans_is_readable_through_every_read(
    admin_client, factory, workspace, engine
):
    """One decorated call, nothing nested: the run arrives with its prompt and
    its answer and no spans. The trace modal showed a header over "no spans
    were recorded" and the replay showed "0 of 0 steps" -- while the store held
    everything an operator wanted to see."""
    agent = await factory.provisioned_agent(workspace, engine, name="Underwriting Bot")
    run = engine.add_trace(
        project_name=agent.engine_project_name,
        name="underwriting_insight",
        input={"question": "Is the Harbour Street warehouse insurable?"},
        output={"answer": "Yes, subject to a sprinkler survey."},
        metadata={"tenant": "acme", "model": "gpt-4o", "broker": "Marlow & Finch"},
        usage={"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52},
    )
    question = "Is the Harbour Street warehouse insurable?"
    answer = "Yes, subject to a sprinkler survey."

    detail = (await admin_client.get(f"/api/v1/runs/{run['id']}")).json()
    assert detail["span_count"] == 0
    assert (detail["input"], detail["response"]) == (question, answer)
    assert detail["metadata"]["broker"] == "Marlow & Finch"
    assert detail["replay_supported"] is True

    full = (await admin_client.get(f"/api/v1/runs/{run['id']}/response")).json()
    assert full["response"] == answer and full["error"] is None

    tree = (await admin_client.get(f"/api/v1/runs/{run['id']}/trace")).json()
    assert tree["spans"] == [] and tree["span_count"] == 0, "no span is made up"
    assert (tree["input"], tree["response"], tree["error"]) == (question, answer, None)
    assert tree["metadata"]["broker"] == "Marlow & Finch"
    assert tree["total_tokens"] == 52

    replay = (await admin_client.get(f"/api/v1/runs/{run['id']}/replay")).json()
    assert (replay["input"], replay["response"]) == (question, answer)
    assert replay["metadata"]["tenant"] == "acme"
    assert replay["steps_from_trace"] is True
    assert replay["replayable"] is True
    assert [(step["kind"], step["span_id"]) for step in replay["steps"]] == [
        ("Prompt", None),
        ("Response", None),
    ]
    assert replay["steps"][0]["prompt"] == question
    assert replay["steps"][1]["response"] == answer
    assert replay["steps"][1]["tokens"] == 52
    assert replay["step_count"] == 2 and replay["fidelity"] == 100.0
    assert [message["role"] for message in replay["transcript"]] == ["user", "agent"]


async def test_a_failed_run_without_spans_says_what_went_wrong(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Underwriting Bot")
    run = engine.add_trace(
        project_name=agent.engine_project_name,
        input={"question": "Quote the Harbour Street warehouse."},
        error_info={
            "exception_type": "TimeoutError",
            "message": "rating service timed out",
            "traceback": "...",
        },
    )

    full = (await admin_client.get(f"/api/v1/runs/{run['id']}/response")).json()
    tree = (await admin_client.get(f"/api/v1/runs/{run['id']}/trace")).json()
    replay = (await admin_client.get(f"/api/v1/runs/{run['id']}/replay")).json()

    assert full["response"] == "" and full["error"] == "rating service timed out"
    assert tree["error"] == "rating service timed out"
    assert replay["error"] == "rating service timed out"
    failed = replay["steps"][-1]
    assert (failed["kind"], failed["status"]) == ("Response", "fail")
    assert failed["error"] == "rating service timed out"
    assert failed["response"] is None, "an answer that was never given is not shown"
    assert replay["fidelity"] == 50.0


async def test_a_run_with_spans_replays_its_spans_and_still_carries_its_own_payload(
    admin_client, factory, workspace, engine, engine_client
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    run = engine.add_trace(
        project_name=agent.engine_project_name,
        input={"question": "Where is my refund?"},
        output={"answer": "On its way."},
    )
    await engine_client.create_spans_batch(
        [
            {
                "trace_id": run["id"],
                "project_name": agent.engine_project_name,
                "name": "chat-completion",
                "type": "llm",
                "start_time": run["start_time"],
                "end_time": run["start_time"],
                "input": {"prompt": "Where is my refund?"},
                "output": {"answer": "On its way."},
            }
        ]
    )

    replay = (await admin_client.get(f"/api/v1/runs/{run['id']}/replay")).json()

    assert replay["steps_from_trace"] is False
    assert [step["kind"] for step in replay["steps"]] == ["Model"]
    assert replay["steps"][0]["span_id"]
    assert (replay["input"], replay["response"]) == ("Where is my refund?", "On its way.")


async def test_a_run_that_recorded_nothing_is_not_dressed_up(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Silent Bot")
    run = engine.add_trace(project_name=agent.engine_project_name)

    detail = (await admin_client.get(f"/api/v1/runs/{run['id']}")).json()
    replay = (await admin_client.get(f"/api/v1/runs/{run['id']}/replay")).json()

    assert detail["replay_supported"] is False
    assert replay["steps"] == [] and replay["replayable"] is False
    assert replay["steps_from_trace"] is False


async def test_the_inspector_does_not_download_payloads_to_look_for_verdicts(
    admin_client, factory, workspace, engine, engine_client
):
    """Opening a row read every span of the run untruncated -- up to a thousand
    prompts and completions -- to find guardrail verdicts, which are not in the
    payloads. The trace tree, which shows payloads, still reads them whole."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    run = engine.add_trace(project_name=agent.engine_project_name, input={"question": "Hi"})
    await engine_client.create_spans_batch(
        [
            {
                "trace_id": run["id"],
                "project_name": agent.engine_project_name,
                "name": "pii-check",
                "type": "guardrail",
                "start_time": run["start_time"],
                "metadata": {"guardrails": [{"name": "PII", "result": "Passed"}]},
            }
        ]
    )
    engine.reset_calls()

    detail = await admin_client.get(f"/api/v1/runs/{run['id']}")
    inspector_reads = [call.body["truncate"] for call in engine.calls_to("/spans/search")]
    engine.reset_calls()
    await admin_client.get(f"/api/v1/runs/{run['id']}/trace")
    tree_reads = [call.body["truncate"] for call in engine.calls_to("/spans/search")]

    assert detail.json()["guardrails"]["pii_detection"] == "Passed"
    assert inspector_reads == [True]
    assert tree_reads == [False]


# ---------------------------------------------------------------------------
# What the run screens ask of the store, taken together
# ---------------------------------------------------------------------------


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def remembering(monkeypatch):
    """Switch the run screens' short memories back on, as production has them."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "runs_activity_cache_seconds", 60.0)
    monkeypatch.setattr(settings, "runs_scan_cache_seconds", 60.0)
    monkeypatch.setattr(settings, "runs_summary_cache_seconds", 60.0)


async def _one_busy_project_among_quiet_ones(factory, workspace, engine):
    """Three agents: one reporting now, two that last reported three days ago."""
    busy = await factory.provisioned_agent(workspace, engine, name="Busy Bot")
    engine.add_trace(project_name=busy.engine_project_name, name="fresh")
    for name in ("Quiet Bot", "Dormant Bot"):
        quiet = await factory.provisioned_agent(workspace, engine, name=name)
        engine.add_trace(
            project_name=quiet.engine_project_name,
            start_time=_utcnow() - dt.timedelta(days=3),
        )
        engine.projects[quiet.engine_project_id]["last_updated_trace_at"] = _iso(
            _utcnow() - dt.timedelta(days=3)
        )
    return busy


async def test_the_table_its_kpi_row_and_its_export_share_one_read_per_project(
    admin_client, factory, workspace, engine, remembering
):
    """Opening Live Runs cost a search per project for the table, two more per
    project for the KPI row, and the same again for every sort, page and export
    -- 32,543 store queries in an hour of somebody looking at a screen."""
    busy = await _one_busy_project_among_quiet_ones(factory, workspace, engine)
    engine.reset_calls()

    table = await admin_client.get("/api/v1/runs")
    summary = await admin_client.get("/api/v1/runs/summary")
    resorted = await admin_client.get("/api/v1/runs", params={"sort": "-tokens"})
    running = await admin_client.get("/api/v1/runs", params={"status": "Running"})
    exported = await admin_client.get("/api/v1/runs/export")

    for response in (table, summary, resorted, running, exported):
        assert response.status_code == 200, response.text
    assert table.json()["total"] == 1
    assert summary.json()["total_runs"] == 1
    assert summary.json()["scan"]["agents_scanned"] == 3, "skipped is not the same as unread"
    assert "Busy Bot" in exported.text

    searches = [call.body for call in engine.calls_to("/traces/search")]
    assert {body["project_id"] for body in searches} == {busy.engine_project_id}, (
        "a project last written to before the window opened cannot hold a run inside it"
    )
    # The current window once, for everybody; the KPI row's earlier window once.
    assert len(searches) == 2, searches
    assert len(engine.calls_to("/v1/private/projects")) == 1


async def test_the_previous_period_is_read_once_and_still_counted_exactly(
    admin_client, factory, workspace, engine, monkeypatch
):
    """The KPI row is asked for again on every run that arrives, and each time
    it re-read "the hour before this one" -- an hour nothing is being written
    into. It is now read on a coarse grid and kept for a few minutes; the rows
    are still counted against the exact window, not the grid."""
    from fulcrum_ops_api.core.config import settings

    # Rows are shared for a moment only and the finished KPI row not at all, so
    # the two requests below each fold their own summary.
    monkeypatch.setattr(settings, "runs_scan_cache_seconds", 0.05)
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    now = _utcnow()
    hour = dt.timedelta(hours=1)
    for minutes in (10, 59):  # this hour; the second is inside the widened read
        engine.add_trace(
            project_name=agent.engine_project_name,
            start_time=now - dt.timedelta(minutes=minutes),
        )
    for minutes in (70, 90, 110):  # the hour before
        engine.add_trace(
            project_name=agent.engine_project_name,
            start_time=now - dt.timedelta(minutes=minutes),
        )
    engine.add_trace(project_name=agent.engine_project_name, start_time=now - 2 * hour - hour / 2)
    engine.reset_calls()

    first = await admin_client.get("/api/v1/runs/summary", params={"time_range": "Last hour"})
    await asyncio.sleep(0.2)
    second = await admin_client.get("/api/v1/runs/summary", params={"time_range": "Last hour"})

    for response in (first, second):
        assert response.status_code == 200, response.text
        assert response.json()["total_runs"] == 2
        assert response.json()["total_runs_previous"] == 3
        assert response.json()["total_runs_delta_percent"] == -33.3
    ended = [
        dt.datetime.fromisoformat(call.body["to_time"].replace("Z", "+00:00"))
        for call in engine.calls_to("/traces/search")
    ]
    assert len([at for at in ended if at < now - dt.timedelta(minutes=50)]) == 1
    assert len([at for at in ended if at > now - dt.timedelta(minutes=5)]) == 2


async def test_the_scan_bound_is_the_process_not_the_request(
    admin_client, factory, workspace, engine
):
    """Each scan made its own semaphore, so "eight at a time" was per request --
    and per window: the KPI row alone had sixteen searches in flight, and a
    table, a KPI row and an export arriving together had thirty-two."""
    from fulcrum_ops_api.services import runs as service

    for index in range(12):
        agent = await factory.provisioned_agent(workspace, engine, name=f"Bot {index}")
        engine.add_trace(project_name=agent.engine_project_name)

    in_flight = 0
    peak = 0

    async def a_round_trip(_method: str, path: str) -> None:
        nonlocal in_flight, peak
        if "/traces/search" not in path:
            return
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
        finally:
            in_flight -= 1

    engine.before_dispatch = a_round_trip
    responses = await asyncio.gather(
        admin_client.get("/api/v1/runs"),
        admin_client.get("/api/v1/runs/summary"),
        admin_client.get("/api/v1/runs/export"),
    )

    for response in responses:
        assert response.status_code == 200, response.text
    assert len(engine.calls_to("/traces/search")) == 48, "nothing was skipped to get there"
    assert peak <= service.SCAN_CONCURRENCY, peak


async def test_a_failed_read_stops_the_reads_still_queued_behind_it(
    admin_client, factory, workspace, engine
):
    """One project's search fails and the table answers 503 -- and the searches
    still waiting their turn used to run anyway, each one a question put to a
    struggling store on behalf of a request that had already been answered."""
    from fulcrum_ops_api.services import runs as service

    for index in range(20):
        agent = await factory.provisioned_agent(workspace, engine, name=f"Bot {index}")
        engine.add_trace(project_name=agent.engine_project_name)

    started = 0

    async def first_one_breaks(_method: str, path: str) -> None:
        nonlocal started
        if "/traces/search" not in path:
            return
        started += 1
        if started == 1:
            await asyncio.sleep(0.05)
            engine.fail_path("/traces/search")
        else:
            await asyncio.sleep(0.3)

    engine.before_dispatch = first_one_breaks
    response = await admin_client.get("/api/v1/runs")
    await asyncio.sleep(0.6)  # long enough for anything left running to show itself

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "telemetry_unavailable"
    assert len(engine.calls_to("/traces/search")) == service.SCAN_CONCURRENCY


# ---------------------------------------------------------------------------
# The live stream
# ---------------------------------------------------------------------------


class _LiveStream:
    """GET /runs/stream, held open the way a browser tab holds it.

    The HTTP client the other tests use reads a response to its end, and this
    one has none for half an hour; so the application is driven over ASGI
    directly, with a caller that hangs up when the test is done.
    """

    def __init__(self, app, token: str, query: str = "") -> None:
        self._app = app
        host = urlsplit(APP_BASE_URL).hostname or "localhost"
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/runs/stream",
            "raw_path": b"/api/v1/runs/stream",
            "query_string": query.encode(),
            "root_path": "",
            "headers": [
                (b"host", host.encode()),
                (b"accept", b"text/event-stream"),
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": (host, 80),
        }
        self.status: int | None = None
        #: Set when the response body ends -- the server closed the stream
        #: rather than the test hanging up. A test that expects a stream to
        #: close can then assert something POSITIVE instead of waiting for a
        #: frame that never comes, which a slow machine also satisfies.
        self.ended = asyncio.Event()
        self._frames: asyncio.Queue[str] = asyncio.Queue()
        self._hang_up = asyncio.Event()
        self._asked = False
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        if not self._asked:
            self._asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._hang_up.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            if message.get("body"):
                await self._frames.put(message["body"].decode())
            if not message.get("more_body", False):
                self.ended.set()

    async def __aenter__(self) -> _LiveStream:
        self._task = asyncio.ensure_future(self._app(self._scope, self._receive, self._send))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._hang_up.set()
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout=10)

    async def frame(self, event: str, *, within: float = 5.0) -> str:
        """The next frame of this kind, skipping keep-alives and the rest."""
        async with asyncio.timeout(within):
            while True:
                text = await self._frames.get()
                if f"event: {event}" in text:
                    return text


async def test_an_open_stream_holds_no_database_connection(
    app, admin, workspace, db_engine, factory, engine
):
    """Live Runs is the landing screen. The request's session stayed checked out
    -- idle, inside the transaction authentication opened -- until the stream
    ended half an hour later, one pooled connection per open tab."""
    from sqlalchemy import event

    from conftest import session_token
    from fulcrum_ops_api.models.identity import Role

    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    held = 0

    def taken(*_args: object) -> None:
        nonlocal held
        held += 1

    def given_back(*_args: object) -> None:
        nonlocal held
        held -= 1

    pool = db_engine.sync_engine.pool
    event.listen(pool, "checkout", taken)
    event.listen(pool, "checkin", given_back)
    try:
        async with _LiveStream(app, session_token(admin, workspace, Role.ADMIN)) as stream:
            await stream.frame("open")
            await asyncio.sleep(0.3)  # the first poll's own short session comes and goes
            assert stream.status == 200
            assert held == 0, "the stream is open and a connection is still checked out"
    finally:
        event.remove(pool, "checkout", taken)
        event.remove(pool, "checkin", given_back)


async def test_a_stream_searches_only_the_projects_that_were_just_written_to(
    app, admin, workspace, factory, engine, remembering
):
    """Every open tab re-read every project every three seconds, whether or not
    anything had been reported: some two hundred store queries a minute for one
    tab left open on the landing screen."""
    from conftest import session_token
    from fulcrum_ops_api.models.identity import Role

    busy = await _one_busy_project_among_quiet_ones(factory, workspace, engine)
    engine.reset_calls()
    token = session_token(admin, workspace, Role.ADMIN)

    async with _LiveStream(app, token) as first, _LiveStream(app, token) as second:
        # Reported a moment before the tabs opened: inside the overlap the first
        # poll reads, so both tabs are told without waiting for a second poll.
        for stream in (first, second):
            assert '"agent":"Busy Bot"' in await stream.frame("run")

    searched = [call.body["project_id"] for call in engine.calls_to("/traces/search")]
    assert set(searched) == {busy.engine_project_id}
    assert len(engine.calls_to("/v1/private/projects")) == 1, "one activity read, shared"


async def test_a_stream_over_a_quiet_workspace_asks_the_store_nothing_per_project(
    app, admin, workspace, factory, engine, remembering
):
    from conftest import session_token
    from fulcrum_ops_api.models.identity import Role

    for name in ("Quiet Bot", "Dormant Bot"):
        quiet = await factory.provisioned_agent(workspace, engine, name=name)
        engine.projects[quiet.engine_project_id]["last_updated_trace_at"] = _iso(
            _utcnow() - dt.timedelta(hours=2)
        )
    engine.reset_calls()

    async with _LiveStream(app, session_token(admin, workspace, Role.ADMIN)) as stream:
        await stream.frame("open")
        await asyncio.sleep(0.5)  # the first poll has been and gone

    assert engine.calls_to("/v1/private/projects"), "the poll ran"
    assert engine.calls_to("/traces/search") == []


# ---------------------------------------------------------------------------
# A stream is not a way to outlive the session that opened it
# ---------------------------------------------------------------------------


async def test_a_stream_stops_delivering_once_its_session_has_been_ended(
    app, admin, workspace, db, factory, engine, monkeypatch
):
    """Live Runs is the landing screen, so a stolen cookie is usually already
    watching it. Changing the password 401s every ordinary request at once; the
    stream used to be the exception, and kept handing over every new run -- with
    the prompt and response text in it -- for up to half an hour.

    The re-check is on a timer rather than per frame, so it is forced here: what
    is being proved is that the stream closes on it, not how often it fires.
    """
    from sqlalchemy import update

    from conftest import session_token
    from fulcrum_ops_api.models.identity import Role, User
    from fulcrum_ops_api.services import runs as service

    monkeypatch.setattr(service, "STREAM_RECHECK_SECONDS", 0.0)
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")

    async with _LiveStream(app, session_token(admin, workspace, Role.ADMIN)) as stream:
        await stream.frame("open")
        engine.add_trace(project_name=agent.engine_project_name, name="before")
        assert '"agent":"Support Bot"' in await stream.frame("run"), "the stream is live"

        # What change_password does, a moment later and from another device.
        await db.execute(
            update(User)
            .where(User.id == admin.id)
            .values(credentials_changed_at=_utcnow() + dt.timedelta(seconds=1))
        )
        engine.add_trace(project_name=agent.engine_project_name, name="after")

        # The stream CLOSES -- asserted positively. "no further frame arrived"
        # would also be satisfied by a machine too busy to produce one.
        await asyncio.wait_for(stream.ended.wait(), timeout=10)
        assert stream._frames.empty(), "the run reported after the change was handed over"


async def test_an_api_key_stream_is_not_disturbed_by_the_session_check(
    app, workspace, db, factory, engine, admin
):
    """A key has no session to end, and must not be closed by a rule about one."""
    from fulcrum_ops_api.api.deps import Principal, session_revoked
    from fulcrum_ops_api.models.identity import Role

    key_caller = Principal(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        engine_workspace=workspace.engine_workspace,
        role=Role.OPERATOR,
        kind="api_key",
        api_key_id="key-1",
    )
    assert await session_revoked(key_caller) is False


async def test_session_revoked_answers_the_four_states_it_exists_for(
    app, workspace, db, factory, admin
):
    from sqlalchemy import delete, update

    from fulcrum_ops_api.api.deps import Principal, session_revoked
    from fulcrum_ops_api.models.identity import Role, User

    def caller(user, issued_at):
        return Principal(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            engine_workspace=workspace.engine_workspace,
            role=Role.ADMIN,
            kind="user",
            user_id=user.id,
            token_issued_at=issued_at,
        )

    now = _utcnow()
    live = caller(admin, now.timestamp())

    # 1. Nothing has happened.
    assert await session_revoked(live) is False

    # 2. The password changed after this token was minted.
    await db.execute(
        update(User)
        .where(User.id == admin.id)
        .values(credentials_changed_at=now + dt.timedelta(seconds=30))
    )
    assert await session_revoked(live) is True
    # A token minted after the change is not caught by it.
    later = caller(admin, (now + dt.timedelta(minutes=1)).timestamp())
    assert await session_revoked(later) is False

    # 3. The account was deactivated.
    await db.execute(
        update(User)
        .where(User.id == admin.id)
        .values(credentials_changed_at=None, is_active=False)
    )
    assert await session_revoked(live) is True
    await db.execute(update(User).where(User.id == admin.id).values(is_active=True))
    assert await session_revoked(live) is False

    # 4. The membership that put them in this workspace was removed. This is the
    # one `remove_member` actually does -- it deletes the membership and leaves
    # the account active, because the account may belong to other workspaces.
    from fulcrum_ops_api.models.identity import Membership

    await db.execute(delete(Membership).where(Membership.user_id == admin.id))
    assert await session_revoked(live) is True
