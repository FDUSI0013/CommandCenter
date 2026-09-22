"""The LLMs Used panel on Live Runs: the window's runs by model.

The breakdown rides on the KPI summary and is folded in the same pass as Total
Runs, so the slices of the donut always add up to the figure printed beside it
-- under a filter, under the scan cap, whatever the runs recorded. A run that
names no model, and whose agent names none either, is its own slice rather
than being dropped or spread across the others.
"""

from __future__ import annotations

import datetime as dt

import pytest


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _summary(http, **params) -> dict:
    response = await http.get("/api/v1/runs/summary", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _run(engine, agent, *, model: str | None = None, tokens: int = 0, cost: float = 0.0,
         minutes_ago: float = 1.0, **metadata):
    at = _utcnow() - dt.timedelta(minutes=minutes_ago)
    if model is not None:
        metadata["model"] = model
    return engine.add_trace(
        project_name=agent.engine_project_name,
        start_time=at,
        end_time=at + dt.timedelta(seconds=1),
        metadata=metadata,
        usage={"prompt_tokens": tokens // 2, "completion_tokens": tokens - tokens // 2,
               "total_tokens": tokens},
        total_estimated_cost=cost,
    )


def _assert_slices_add_up(summary: dict) -> None:
    slices = summary["models"]
    assert sum(item["runs"] for item in slices) == summary["total_runs"]
    assert sum(item["tokens"] for item in slices) == summary["tokens_used"]["value"]
    assert sum(item["cost"] for item in slices) == pytest.approx(
        summary["estimated_cost"]["value"], abs=1e-4
    )


async def test_runs_are_broken_down_by_model_and_add_up_to_total_runs(
    admin_client, factory, workspace, engine
):
    recorded = await factory.provisioned_agent(workspace, engine, name="Support Bot",
                                               model="gpt-4o")
    unnamed = await factory.provisioned_agent(workspace, engine, name="Legacy Bot",
                                              model=None)
    for _ in range(3):
        _run(engine, recorded, model="gpt-4o", tokens=100, cost=0.01)
    for _ in range(2):
        _run(engine, recorded, model="claude-sonnet", tokens=50, cost=0.02)
    # No model on the run: the table's Model column shows the agent's registered
    # model, and so does the breakdown.
    _run(engine, recorded, tokens=10, cost=0.001)
    # No model on the run and none registered: recorded nowhere.
    _run(engine, unnamed, tokens=7, cost=0.0)
    _run(engine, unnamed, tokens=0, cost=0.0, model="   ")
    # The hour before is not this window's.
    _run(engine, recorded, model="gpt-4o", tokens=999, cost=9.0, minutes_ago=90)

    summary = await _summary(admin_client, time_range="Last hour")

    assert summary["total_runs"] == 8
    assert summary["models"] == [
        {
            "model": "gpt-4o",
            "runs": 4,
            "tokens": 310,
            "cost": pytest.approx(0.031),
            # The one run that named no model, counted under its agent's -- and
            # said to be, so a registration never reads as what a run reported.
            "runs_model_from_agent": 1,
        },
        {
            "model": "claude-sonnet",
            "runs": 2,
            "tokens": 100,
            "cost": pytest.approx(0.04),
            "runs_model_from_agent": 0,
        },
        {"model": None, "runs": 2, "tokens": 7, "cost": 0.0, "runs_model_from_agent": 0},
    ]
    _assert_slices_add_up(summary)

    table = await admin_client.get("/api/v1/runs", params={"time_range": "Last hour"})
    by_model: dict[str | None, int] = {}
    for row in table.json()["items"]:
        by_model[row["model"]] = by_model.get(row["model"], 0) + 1
    assert by_model == {"gpt-4o": 4, "claude-sonnet": 2, None: 2}, (
        "the panel and the table's Model column describe the same runs the same way"
    )


async def test_the_breakdown_follows_the_filters_total_runs_follows(
    admin_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    other = await factory.provisioned_agent(workspace, engine, name="Payroll Bot")
    _run(engine, agent, model="gpt-4o", tenant="acme")
    _run(engine, agent, model="gpt-4o", tenant="globex")
    _run(engine, agent, model="claude-sonnet", tenant="acme")
    _run(engine, other, model="gpt-4o-mini", tenant="acme")

    by_tenant = await _summary(admin_client, time_range="Last hour", tenant="acme")
    by_agent = await _summary(admin_client, time_range="Last hour", agent_id=other.id)

    assert by_tenant["total_runs"] == 3
    assert {item["model"]: item["runs"] for item in by_tenant["models"]} == {
        "gpt-4o": 1,
        "claude-sonnet": 1,
        "gpt-4o-mini": 1,
    }
    _assert_slices_add_up(by_tenant)
    assert by_agent["models"] == [
        {"model": "gpt-4o-mini", "runs": 1, "tokens": 0, "cost": 0.0, "runs_model_from_agent": 0}
    ]
    _assert_slices_add_up(by_agent)


async def test_a_capped_window_is_broken_down_as_far_as_it_was_read(
    admin_client, factory, workspace, engine
):
    """The breakdown of a capped scan is a breakdown of the runs read, and adds
    up to the Total Runs that says it is a floor -- not to some other number."""
    agent = await factory.provisioned_agent(workspace, engine, name="Busy Bot")
    now = _utcnow()
    every = dt.timedelta(hours=1) / 2200
    for index in range(2050):
        at = now - dt.timedelta(minutes=1) - every * index
        engine.add_trace(
            project_name=agent.engine_project_name,
            start_time=at,
            end_time=at,
            metadata={"model": "gpt-4o" if index % 3 else "claude-sonnet"},
        )

    summary = await _summary(admin_client, time_range="Last hour")

    assert summary["scan"]["truncated"] is True
    assert summary["total_runs"] == 2000
    _assert_slices_add_up(summary)
    assert [item["model"] for item in summary["models"]] == ["gpt-4o", "claude-sonnet"]


async def test_a_slice_says_how_many_of_its_runs_came_from_the_agents_registration(
    admin_client, factory, workspace, engine
):
    """A run that names no model -- or only blanks -- is counted under the
    model its agent is registered with, as the table's Model column shows it.
    The slice says how many of its runs are there by that rule, so the panel
    never presents a registration as something every run reported."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot", model="gpt-4o")
    _run(engine, agent, model="gpt-4o")
    _run(engine, agent)
    _run(engine, agent, model="  ")
    _run(engine, agent, model="claude-sonnet")

    summary = await _summary(admin_client, time_range="Last hour")

    by_model = {item["model"]: item for item in summary["models"]}
    assert set(by_model) == {"gpt-4o", "claude-sonnet"}
    assert (by_model["gpt-4o"]["runs"], by_model["gpt-4o"]["runs_model_from_agent"]) == (3, 2)
    assert by_model["claude-sonnet"]["runs_model_from_agent"] == 0
    _assert_slices_add_up(summary)


async def test_an_empty_window_has_no_slices(admin_client, factory, workspace, engine):
    await factory.provisioned_agent(workspace, engine, name="Quiet Bot")

    summary = await _summary(admin_client, time_range="Last hour")

    assert summary["total_runs"] == 0
    assert summary["models"] == []
    # A rate over no runs was not measured, and is not reported as 0%.
    assert summary["fallback_rate"]["value"] is None
