"""Regressions for the reconcile pass over the engine double.

The double is the second implementation of the engine's contract, so a place
where it is *kinder* than the real engine is a place where a test passes and
production does not. Each test here pins one such place through the same HTTP
the console uses, so it says what an operator would have seen.
"""

from __future__ import annotations

import datetime as dt

from fulcrum_ops_api.db.base import new_id


def tokens_spent(engine, agent, *, at, tokens):
    """A run whose tokens are carried by its LLM span, as the engine accounts for them."""
    trace = engine.add_trace(
        project_name=agent.engine_project_name,
        start_time=at,
        end_time=at + dt.timedelta(seconds=1),
    )
    engine._store_span(
        {
            "trace_id": trace["id"],
            "project_name": agent.engine_project_name,
            "type": "llm",
            "start_time": trace["start_time"],
            "end_time": trace["end_time"],
            "usage": {
                "prompt_tokens": tokens - 10,
                "completion_tokens": 10,
                "total_tokens": tokens,
            },
        }
    )
    engine._refresh_trace(trace["id"])


def line(payload, metric) -> dict:
    return next(row for row in payload["series"] if row["metric"] == metric)


def hour_of(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Workspace token usage: the interval that was asked for
# ---------------------------------------------------------------------------


async def test_the_last_24_hours_tokens_chart_puts_each_hour_in_its_own_bucket(
    admin_client, factory, workspace, engine
):
    """The double answered in days whatever it was asked: every token sat on one bucket."""
    agent = await factory.provisioned_agent(workspace, engine, name="Tokens")
    now = dt.datetime.now(dt.UTC)
    # Twenty hours ago and one hour ago: always two different hours, and both
    # inside the window whatever the time of day.
    early, late = now - dt.timedelta(hours=20), now - dt.timedelta(hours=1)
    tokens_spent(engine, agent, at=early, tokens=300)
    tokens_spent(engine, agent, at=late, tokens=500)

    response = await admin_client.get(
        "/api/v1/metrics/series", params={"window": "24h", "metric": "tokens"}
    )

    assert response.status_code == 200, response.text
    chart = response.json()
    tokens = line(chart, "tokens")
    used = [(p["timestamp"], p["value"]) for p in tokens["points"] if p["value"]]
    assert chart["interval"] == "hourly"
    (asked,) = engine.calls_to("/workspaces/metrics/spans")
    assert asked.body["interval"] == "HOURLY"
    # The total line only: prompt and completion are separate lines of the same
    # tokens, and a reader that adds every line reports them twice.
    assert tokens["total"] == 800
    assert [value for _stamp, value in used] == [300, 500], "each hour keeps its own tokens"
    assert [dt.datetime.fromisoformat(stamp) for stamp, _value in used] == [
        hour_of(early),
        hour_of(late),
    ]


async def test_workspace_usage_buckets_on_the_calendar_unit_it_is_asked_for(engine_client, engine):
    """Hour, day, and the week from Monday 00:00 UTC -- the per-project series' rule."""
    project = engine.add_project("usage-intervals")
    # A Thursday afternoon and the Saturday evening after it.
    thursday = dt.datetime(2026, 9, 17, 14, 37, tzinfo=dt.UTC)
    saturday = dt.datetime(2026, 9, 19, 21, 5, tzinfo=dt.UTC)
    for at, tokens in ((thursday, 100), (saturday, 40)):
        engine._store_span(
            {
                "trace_id": "0198f000-0000-7000-8000-000000000001",
                "project_id": project["id"],
                "type": "llm",
                "start_time": at.isoformat(),
                "usage": {"total_tokens": tokens},
            }
        )

    async def stamps(interval: str) -> dict[str, float]:
        payload = await engine_client.get_workspace_usage(
            metric_type="SPAN_TOKEN_USAGE",
            interval=interval,
            interval_start=thursday - dt.timedelta(days=1),
            interval_end=saturday + dt.timedelta(days=1),
            project_ids=[project["id"]],
        )
        total = next(row for row in payload["results"] if row["name"] == "total_tokens")
        return {point["time"]: point["value"] for point in total["data"]}

    assert await stamps("HOURLY") == {
        "2026-09-17T14:00:00Z": 100,
        "2026-09-19T21:00:00Z": 40,
    }
    assert await stamps("DAILY") == {"2026-09-17T00:00:00Z": 100, "2026-09-19T00:00:00Z": 40}
    # Both days belong to the week that opened on Monday the 14th -- stamped
    # before the window asked about, exactly as the real engine stamps it.
    assert await stamps("WEEKLY") == {"2026-09-14T00:00:00Z": 140}


# ---------------------------------------------------------------------------
# A batch write of an id already stored
# ---------------------------------------------------------------------------


async def test_a_second_batch_write_of_a_run_replaces_the_row_it_finds(
    ingest_client, factory, workspace, engine
):
    """``batch_insert_replaces`` was declared and documented but never honoured.

    A flag a test can set and that then does nothing is worse than no flag:
    the test reads as if it pinned the strict assumption and passes under the
    kind one. Here the reporter that finishes a run resends everything but the
    input, and on the store's own last-write-wins insert the input is gone.
    """
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.batch_insert_replaces = True
    run_id = new_id()
    opened_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    started = {
        "id": run_id,
        "name": "answer customer question",
        "start_time": opened_at.isoformat().replace("+00:00", "Z"),
        "input": {"question": "Where is my order?"},
    }
    finished = {
        **started,
        "end_time": (opened_at + dt.timedelta(seconds=1.5)).isoformat().replace("+00:00", "Z"),
        "output": {"answer": "It ships tomorrow."},
    }
    finished.pop("input")

    opened = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [started]}
    )
    closed = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [finished]}
    )

    assert opened.status_code == 200, opened.text
    assert closed.status_code == 200, closed.text
    assert [row["outcome"] for row in closed.json()["results"]] == ["accepted"]
    (stored,) = engine.traces.values()
    assert stored["output"] == {"answer": "It ships tomorrow."}
    assert stored.get("input") is None, "the second batch write is the whole row"
    # Left off, the double keeps merging -- which is what every test written
    # before the flag existed relies on.
    engine.batch_insert_replaces = False
    merged_back = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [started]}
    )
    assert merged_back.status_code == 200, merged_back.text
    (stored,) = engine.traces.values()
    assert stored["input"] == {"question": "Where is my order?"}
    assert stored["output"] == {"answer": "It ships tomorrow."}
