"""Regressions for the Evaluations screen, from the September 2026 audit.

Each test here failed against the code it was written for. They share one
subject -- the judged loop between an SDK experiment's traces and the run the
Evaluations table shows -- and the ways it went wrong:

* a run was handed to its supervisor before the row was committed, and a
  supervisor that lost that race left it Queued with nobody driving it;
* a dataset no SDK experiment had covered was "judged" anyway: every case was
  registered blank and the run waited out a fifteen-minute deadline for verdicts
  nothing was ever going to write;
* a scorer whose name was not on a short alias list was thrown away, so the
  SDK's own documented example ended Failed with every case Unscored;
* the experiment's trace count was read as "cases judged", so three judged cases
  out of fifty were reported as fifty of fifty.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import sqlite3

from fulcrum_ops_api.db.base import new_id
from fulcrum_ops_api.services import evaluations as service


def iso(offset: float = 0.0) -> str:
    base = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=3)
    return (base + dt.timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


async def make_dataset(http, name: str, inputs: list[str]) -> list[str]:
    """Create a dataset through the API and return its case ids, in order."""
    created = await http.post("/api/v1/evaluations/datasets", json={"name": name})
    assert created.status_code == 201, created.text
    added = await http.post(
        f"/api/v1/evaluations/datasets/{name}/items",
        json={"items": [{"input": text, "expected_output": "ok"} for text in inputs]},
    )
    assert added.status_code == 201, added.text
    listed = await http.get(f"/api/v1/evaluations/datasets/{name}/items", params={"page_size": 100})
    by_input = {row["input"]: row["id"] for row in listed.json()["items"]}
    return [by_input[text] for text in inputs]


async def sdk_case(ingest, *, agent: str, dataset: str, item_id: str, scores, offset: float = 0.0):
    """What ``client.evaluate()`` leaves behind for one case: a tagged, scored trace."""
    trace = {
        "name": f"experiment:{dataset}",
        "start_time": iso(offset),
        "end_time": iso(offset + 1.0),
        "tags": ["experiment", f"experiment:{dataset}"],
        "metadata": {"dataset": dataset, "dataset_item_id": item_id},
    }
    if scores:
        trace["feedback_scores"] = [
            {"name": name, "value": value, "source": "sdk"} for name, value in scores.items()
        ]
    posted = await ingest.post("/api/v1/ingest/traces", json={"agent": agent, "traces": [trace]})
    assert posted.json()["accepted"] == 1, posted.text


async def until_terminal(http, evaluation_id: str, *, polls: int = 60) -> dict:
    progress: dict = {}
    for _ in range(polls):
        progress = (await http.get(f"/api/v1/evaluations/{evaluation_id}/progress")).json()
        if progress["is_terminal"]:
            break
        await asyncio.sleep(0.25)
    return progress


# ---------------------------------------------------------------------------
# A queued run is durable before anything is asked to drive it
# ---------------------------------------------------------------------------


async def test_a_run_is_committed_before_its_supervisor_is_started(
    admin_client, engine, tmp_path
):
    """The supervisor reads the row on its own connection, so it has to be there.

    The probe is a second, synchronous connection to the same database, asked at
    the instant the supervisor's task is created: it sees only what has been
    committed, which is exactly what the supervisor's own session will see.
    """
    await make_dataset(admin_client, "durable-set", ["only case"])
    database = tmp_path / "control-plane.db"
    visible: list[int] = []

    def watching(loop, coro, **kwargs):
        if getattr(coro, "__qualname__", "") == "EvaluationSupervisor._run":
            with contextlib.closing(sqlite3.connect(database, timeout=2)) as probe:
                visible.append(
                    probe.execute("SELECT count(*) FROM evaluation_runs").fetchone()[0]
                )
        return asyncio.Task(coro, loop=loop, **kwargs)

    loop = asyncio.get_running_loop()
    loop.set_task_factory(watching)
    try:
        started = await admin_client.post(
            "/api/v1/evaluations", json={"dataset": "durable-set", "judge_model": "gpt-5"}
        )
    finally:
        loop.set_task_factory(None)
    assert started.status_code == 202, started.text
    assert visible == [1], "the supervisor was started on a row no other connection can see"

    rerun_visible_before = len(visible)
    await until_terminal(admin_client, started.json()["id"])
    loop.set_task_factory(watching)
    try:
        again = await admin_client.post(f"/api/v1/evaluations/{started.json()['id']}/rerun")
    finally:
        loop.set_task_factory(None)
    assert again.status_code == 202, again.text
    assert visible[rerun_visible_before:] == [2], "a re-run is queued the same way"
    await until_terminal(admin_client, again.json()["id"])


async def test_the_supervisor_looks_again_for_a_row_it_cannot_see_yet(
    admin_client, factory, workspace, db
):
    """A supervisor that loses the race to a COMMIT must not walk away in silence."""
    from fulcrum_ops_api.models.quality import EvaluationRun

    evaluation_id = new_id()
    assert service.supervisor.start(evaluation_id, workspace.id)
    await asyncio.sleep(0.05)  # its first look has happened, and found nothing
    await factory.evaluation_run(
        workspace, id=evaluation_id, dataset_ref="never-created", status="Queued"
    )

    progress = await until_terminal(admin_client, evaluation_id)
    assert progress["is_terminal"], "the run was abandoned in Queued"
    run = await db.get(EvaluationRun, evaluation_id)
    assert run.status == "Failed"
    assert "never-created" in (run.notes or "")


# ---------------------------------------------------------------------------
# A dataset nothing has judged fails at once, and says what to do about it
# ---------------------------------------------------------------------------


async def test_a_dataset_no_experiment_has_covered_fails_at_once_and_says_why(
    admin_client, factory, workspace, engine
):
    """Nothing here runs the agent or calls the judge, so there is nothing to wait for."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await make_dataset(admin_client, "unjudged-set", ["case one", "case two"])

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "unjudged-set", "judge_model": "gpt-4o"}
    )
    assert started.status_code == 202, started.text
    progress = await until_terminal(admin_client, started.json()["id"], polls=24)
    assert progress["is_terminal"], "the run is still waiting for verdicts nothing will write"

    detail = (await admin_client.get(f"/api/v1/evaluations/{started.json()['id']}")).json()
    assert detail["status"] == "Failed"
    assert "client.evaluate('unjudged-set'" in detail["notes"]
    assert detail["cases"] == 0, "a run that judged nothing ran no cases"

    assert engine.experiments == {}, "no experiment is created for a run that cannot be judged"
    assert not engine.calls_to("/experiments/items"), "no blank case was written to the store"


async def test_a_store_that_cannot_be_searched_is_not_reported_as_nothing_to_judge(
    admin_client, ingest_client, factory, workspace, engine
):
    """'Found nothing' and 'could not look' are different answers."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    (item_id,) = await make_dataset(admin_client, "blind-set", ["only case"])
    await sdk_case(
        ingest_client, agent="Support Bot", dataset="blind-set", item_id=item_id,
        scores={"answer_correctness": 0.9},
    )
    engine.fail_path("/traces/search", 503)

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "blind-set", "judge_model": "gpt-4o"}
    )
    progress = await until_terminal(admin_client, started.json()["id"], polls=24)
    assert progress["is_terminal"]

    detail = (await admin_client.get(f"/api/v1/evaluations/{started.json()['id']}")).json()
    assert detail["status"] == "Failed"
    assert "client.evaluate" not in detail["notes"], detail["notes"]
    assert "unavailable" in detail["notes"]


# ---------------------------------------------------------------------------
# Cases judged are counted, not cases registered
# ---------------------------------------------------------------------------


async def test_a_partly_judged_run_reports_the_cases_it_judged(
    admin_client, ingest_client, factory, workspace, engine, monkeypatch
):
    """Three cases: one scored, one whose task raised (a trace, no score), one never run."""
    monkeypatch.setattr(service, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(service, "SCORE_SETTLE_SECONDS", 0.3)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    scored, raised, _never = await make_dataset(
        admin_client, "partial-set", ["scored", "raised", "never run"]
    )
    await sdk_case(
        ingest_client, agent="Support Bot", dataset="partial-set", item_id=scored,
        scores={"answer_correctness": 0.8},
    )
    await sdk_case(
        ingest_client, agent="Support Bot", dataset="partial-set", item_id=raised,
        scores=None, offset=2.0,
    )

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "partial-set", "judge_model": "gpt-4o"}
    )
    evaluation_id = started.json()["id"]
    progress = await until_terminal(admin_client, evaluation_id)
    assert progress["is_terminal"], "a case with no verdict coming held the run open"

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["status"] == "Completed", detail.get("notes")
    assert detail["correctness"] == 0.8
    assert detail["cases"] == 1, "one case was judged, not the three that were registered"
    assert detail["scored_items"] == 1
    assert "Judged 1 of 3" in detail["notes"]

    summary = (await admin_client.get("/api/v1/evaluations/summary")).json()
    assert summary["cases_run"] == 1


async def test_a_run_the_sweep_failed_does_not_show_its_datasets_size_as_cases_run(
    admin_client, factory, workspace, engine
):
    """The stale-run sweep fails a run without touching the count the bar was using."""
    run = await factory.evaluation_run(
        workspace, status="Failed", case_count=25, notes="Interrupted before it finished."
    )

    listed = (await admin_client.get("/api/v1/evaluations")).json()["items"]
    assert listed[0]["cases"] == 0, "a failed run judged nothing"
    detail = (await admin_client.get(f"/api/v1/evaluations/{run.id}")).json()
    assert detail["cases"] == 0 and detail["scored_items"] == 0


# ---------------------------------------------------------------------------
# A scorer's verdict counts whatever the scorer is called
# ---------------------------------------------------------------------------


async def test_a_scorer_under_its_own_name_still_judges_the_run(
    admin_client, ingest_client, factory, workspace, engine
):
    """The SDK names a score after the scorer function; ``tone_check`` is on no alias list."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    first, second = await make_dataset(admin_client, "own-name-set", ["one", "two"])
    for index, item_id in enumerate((first, second)):
        await sdk_case(
            ingest_client, agent="Support Bot", dataset="own-name-set", item_id=item_id,
            scores={"tone_check": 1.0 if index == 0 else 0.5, "user_feedback": 0.0},
            offset=index * 2.0,
        )

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "own-name-set", "judge_model": "gpt-4o"}
    )
    evaluation_id = started.json()["id"]
    progress = await until_terminal(admin_client, evaluation_id, polls=24)
    assert progress["is_terminal"], "verdicts under an unlisted name were not seen as verdicts"

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["status"] == "Completed", detail.get("notes")
    assert detail["extra_scores"] == {"tone_check": 0.75}
    assert detail["avg_score"] == 0.75, "the only judged score is the average"
    assert detail["correctness"] is None, "an unlisted name is never guessed into a column"
    assert detail["cases"] == 2

    by_input = {item["input"]: item for item in detail["items"]}
    assert by_input["one"]["scores"] == {"tone_check": 1.0}
    assert by_input["one"]["passed"] is True
    assert "user_feedback" not in by_input["two"]["scores"], "a thumbs-down is not a verdict"

    listed = (await admin_client.get("/api/v1/evaluations")).json()["items"]
    assert listed[0]["avg_score"] == 0.75


def test_the_readmes_scorer_and_the_engines_own_judges_land_in_their_columns():
    from fulcrum_ops_api.schemas.evaluations import average_score, metric_values

    values = metric_values(
        [
            {"name": "contains_expected", "value": 0.5},
            {"name": "hallucination_metric", "value": 0.1},
            {"name": "Answer Relevance Metric", "value": 0.9},
            {"name": "latency_ms", "value": 840},
        ]
    )
    assert values == {"correctness": 0.7, "faithfulness": 0.9, "latency_ms": 840.0}
    assert average_score(values) == 0.8, "a latency is reported, never averaged with a ratio"
    assert metric_values(values) == values, "the row's cached scores read back unchanged"
    assert average_score({"latency_ms": 840.0}) is None


# ---------------------------------------------------------------------------
# The experiment's traces are found on an agent with real traffic
# ---------------------------------------------------------------------------


async def test_an_experiment_is_found_under_a_busy_agents_newer_traffic(
    admin_client, ingest_client, factory, workspace, engine
):
    """Six hundred production traces landed after the experiment ran."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    (item_id,) = await make_dataset(admin_client, "buried-set", ["only case"])
    await sdk_case(
        ingest_client, agent="Support Bot", dataset="buried-set", item_id=item_id,
        scores={"answer_correctness": 0.9},
    )
    for _ in range(600):
        engine.add_trace(project_name=agent.engine_project_name, name="production traffic")

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "buried-set", "judge_model": "gpt-4o"}
    )
    evaluation_id = started.json()["id"]
    await until_terminal(admin_client, evaluation_id, polls=24)

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["status"] == "Completed", detail.get("notes")
    assert detail["correctness"] == 0.9

    searches = engine.calls_to("/traces/search")
    assert searches[0].body["filters"] == [
        {"field": "metadata", "key": "dataset", "operator": "=", "value": "buried-set"}
    ], "the store is asked for this dataset's experiment traces, not for everything"
    assert len(searches) == 1, "a narrowed search that found the experiment is the only one made"


async def test_unstamped_traces_are_still_found_by_following_the_cursor(
    admin_client, ingest_client, factory, workspace, engine
):
    """A trace carrying only the item id needs the unfiltered pass — and its cursor."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    (item_id,) = await make_dataset(admin_client, "unstamped-set", ["only case"])
    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    "name": "custom harness",
                    "start_time": iso(0),
                    "end_time": iso(1.0),
                    "metadata": {"dataset_item_id": item_id},
                    "feedback_scores": [{"name": "groundedness", "value": 0.6}],
                }
            ],
        },
    )
    assert posted.json()["accepted"] == 1, posted.text
    for _ in range(600):
        engine.add_trace(project_name=agent.engine_project_name, name="production traffic")

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "unstamped-set", "judge_model": "gpt-4o"}
    )
    evaluation_id = started.json()["id"]
    await until_terminal(admin_client, evaluation_id, polls=24)

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["status"] == "Completed", detail.get("notes")
    assert detail["grounding"] == 0.6


async def test_the_most_recently_active_agents_are_the_ones_searched(
    admin_client, ingest_client, factory, workspace, engine
):
    """Thirty agents, a cap on how many are searched: the quiet ones are dropped."""
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    for index in range(30):
        await factory.provisioned_agent(
            workspace, engine, name=f"Quiet Bot {index:02d}", last_used_at=long_ago
        )
    await factory.provisioned_agent(workspace, engine, name="Support Bot", last_used_at=long_ago)
    (item_id,) = await make_dataset(admin_client, "fleet-set", ["only case"])
    # Reporting the experiment is what makes Support Bot the most recently active.
    await sdk_case(
        ingest_client, agent="Support Bot", dataset="fleet-set", item_id=item_id,
        scores={"answer_correctness": 1.0},
    )

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "fleet-set", "judge_model": "gpt-4o"}
    )
    evaluation_id = started.json()["id"]
    await until_terminal(admin_client, evaluation_id, polls=24)

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["status"] == "Completed", detail.get("notes")
    searched = {call.body["project_name"] for call in engine.calls_to("/traces/search")}
    assert len(searched) == service.TRACE_LINK_MAX_PROJECTS


# ---------------------------------------------------------------------------
# The inspector needs the store for the case list, and for nothing else
# ---------------------------------------------------------------------------


async def judged_run(admin_client, ingest_client, factory, workspace, engine, dataset: str) -> str:
    """One completed, fully judged evaluation; returns its id."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    (item_id,) = await make_dataset(admin_client, dataset, ["only case"])
    await sdk_case(
        ingest_client, agent="Support Bot", dataset=dataset, item_id=item_id,
        scores={"answer_correctness": 0.9},
    )
    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": dataset, "judge_model": "gpt-4o"}
    )
    progress = await until_terminal(admin_client, started.json()["id"], polls=24)
    assert progress["status"] == "Completed", progress
    return started.json()["id"]


async def test_a_finished_run_opens_while_the_store_is_down(
    admin_client, ingest_client, factory, workspace, engine
):
    evaluation_id = await judged_run(
        admin_client, ingest_client, factory, workspace, engine, "outage-set"
    )
    engine.fail(503)

    opened = await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")
    assert opened.status_code == 200, opened.text
    detail = opened.json()
    assert detail["correctness"] == 0.9 and detail["avg_score"] == 0.9
    assert detail["scored_items"] == 1
    assert detail["trend"], "the trend is read from this database"
    assert detail["items"] == [] and detail["items_unavailable"] is True


async def test_a_finished_run_does_not_reread_its_experiment(
    admin_client, ingest_client, factory, workspace, engine
):
    evaluation_id = await judged_run(
        admin_client, ingest_client, factory, workspace, engine, "quiet-set"
    )
    engine.reset_calls()

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["items_unavailable"] is False and len(detail["items"]) == 1
    experiment_reads = [
        call
        for call in engine.calls
        if call.method == "GET" and call.path.endswith(detail["engine_experiment_id"])
    ]
    assert experiment_reads == [], "its averages and judged count are already on the row"


async def test_a_slow_store_costs_the_inspector_its_cases_not_the_whole_view(
    admin_client, ingest_client, factory, workspace, engine, monkeypatch
):
    evaluation_id = await judged_run(
        admin_client, ingest_client, factory, workspace, engine, "slow-set"
    )
    monkeypatch.setattr(service, "DETAIL_ENGINE_BUDGET_SECONDS", 0.2)

    async def crawl(_method: str, _path: str) -> None:
        await asyncio.sleep(1.5)

    engine.before_dispatch = crawl
    loop = asyncio.get_running_loop()
    began = loop.time()
    opened = await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")
    elapsed = loop.time() - began
    engine.before_dispatch = None

    assert opened.status_code == 200, opened.text
    assert opened.json()["items_unavailable"] is True
    assert opened.json()["correctness"] == 0.9
    assert elapsed < 1.2, f"the inspector waited {elapsed:.1f}s on the store"


# ---------------------------------------------------------------------------
# The dataset listing is searched and counted before it is paged
# ---------------------------------------------------------------------------


def store_datasets(engine, names: list[str]) -> None:
    """Put datasets straight into the store, under whatever names the test gives."""
    for name in names:
        dataset_id = new_id()
        engine.datasets[dataset_id] = {
            "id": dataset_id,
            "name": name,
            "description": None,
            "tags": [],
            "created_at": iso(),
            "last_updated_at": iso(),
        }
        engine.dataset_items[dataset_id] = []


async def test_a_dataset_search_finds_matches_beyond_the_stores_first_page(
    admin_client, engine, workspace
):
    """Sixty ordinary datasets, then three golden ones, then sixty more."""
    ordinary = [f"northwind::set-{index:03d}" for index in range(120)]
    golden = [f"northwind::golden-{region}" for region in ("emea", "apac", "amer")]
    store_datasets(engine, ordinary[:60] + golden + ordinary[60:])

    found = await admin_client.get("/api/v1/evaluations/datasets", params={"q": "golden"})
    assert found.status_code == 200, found.text
    page = found.json()
    assert sorted(row["name"] for row in page["items"]) == [
        "golden-amer", "golden-apac", "golden-emea",
    ], "the search was applied to one page of the store's listing, not to the datasets"
    assert page["total"] == 3 and page["pages"] == 1


async def test_a_dataset_page_is_full_and_its_total_counts_only_this_workspace(
    admin_client, engine, workspace
):
    """The store matches the namespace as text, so a neighbour's names come back too."""
    ours = [f"northwind::set-{index:03d}" for index in range(60)]
    theirs = [f"northwind-eu::set-{index:03d}" for index in range(5)]
    store_datasets(engine, theirs + ours)

    first = (
        await admin_client.get("/api/v1/evaluations/datasets", params={"page_size": 50})
    ).json()
    second = (
        await admin_client.get(
            "/api/v1/evaluations/datasets", params={"page_size": 50, "page": 2}
        )
    ).json()
    assert first["total"] == 60, "the total counted rows this workspace can never be shown"
    assert len(first["items"]) == 50, "a page came back short because of what was dropped from it"
    assert len(second["items"]) == 10
    names = [row["name"] for row in first["items"] + second["items"]]
    assert names == [f"set-{index:03d}" for index in range(60)]

    # The picker reads the whole list in one call.
    everything = (
        await admin_client.get("/api/v1/evaluations/datasets", params={"page_size": 200})
    ).json()
    assert len(everything["items"]) == 60


async def test_the_dataset_filter_offers_the_datasets_the_table_names(
    admin_client, factory, workspace, other_workspace, engine
):
    """Every dataset a row names — deleted from the store or not — and no other."""
    await factory.evaluation_run(workspace, dataset_ref="refunds")
    await factory.evaluation_run(workspace, dataset_ref="checkout-questions")
    await factory.evaluation_run(workspace, dataset_ref="refunds", status="Failed")
    await factory.evaluation_run(other_workspace, dataset_ref="somebody-elses")

    offered = await admin_client.get("/api/v1/evaluations/datasets/in-use")
    assert offered.status_code == 200, offered.text
    assert offered.json() == ["checkout-questions", "refunds"]
    assert engine.calls == [], "the filter is read from this database alone"


async def test_a_dataset_name_cannot_hold_a_slash(admin_client, engine):
    """The name is a path segment: ``a/b`` could be created but never filled or read."""
    refused = await admin_client.post("/api/v1/evaluations/datasets", json={"name": "emea/refunds"})
    assert refused.status_code == 422, refused.text
    assert engine.datasets == {}


# ---------------------------------------------------------------------------
# Watching a run costs the store nothing, and the KPI cards read what they use
# ---------------------------------------------------------------------------


async def test_progress_of_a_run_supervised_elsewhere_does_not_read_the_store(
    admin_client, factory, workspace, engine
):
    """Three workers in four have no counters for a run; the modal polls them all."""
    run = await factory.evaluation_run(
        workspace,
        status="Running",
        case_count=40,
        engine_experiment_id=new_id(),
        started_at=dt.datetime.now(dt.UTC),
    )

    polled = await admin_client.get(f"/api/v1/evaluations/{run.id}/progress")
    assert polled.status_code == 200, polled.text
    progress = polled.json()
    assert engine.calls == [], "a progress poll read the experiment from the store"
    assert progress["phase"] == "Scoring" and progress["is_terminal"] is False
    assert progress["total_cases"] == 40 and progress["processed_cases"] == 40
    assert progress["scored_cases"] == 0, "registered is not judged, and is not reported as it"


async def test_a_finished_runs_counters_are_not_kept_for_the_life_of_the_worker(
    admin_client, ingest_client, factory, workspace, engine
):
    evaluation_id = await judged_run(
        admin_client, ingest_client, factory, workspace, engine, "forgotten-set"
    )
    await asyncio.sleep(0.05)  # the task's done-callback runs on the next loop turn
    assert service.supervisor.progress(evaluation_id) is None

    progress = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}/progress")).json()
    assert progress["percent"] == 100.0 and progress["phase"] == "Finished"
    assert progress["scored_cases"] == 1


async def test_the_kpi_cards_load_the_columns_they_read_not_whole_rows(
    admin_client, factory, workspace, db_engine
):
    """Same cards, same numbers — from a scan that leaves the notes where they are."""
    from sqlalchemy import event

    now = dt.datetime.now(dt.UTC)
    await factory.evaluation_run(
        workspace, scores={"correctness": 0.9}, case_count=4, notes="n" * 4000,
        created_at=now - dt.timedelta(hours=2), finished_at=now - dt.timedelta(hours=2),
    )
    await factory.evaluation_run(
        workspace, scores={"correctness": 0.5}, case_count=6,
        created_at=now - dt.timedelta(hours=1), finished_at=now - dt.timedelta(hours=1),
    )
    # Neither of these judged a case: a live run's count is its dataset's size, and
    # a run the reaper failed for it (restart, timeout) is left holding that size.
    await factory.evaluation_run(workspace, status="Running", case_count=40)
    await factory.evaluation_run(workspace, status="Failed", case_count=25, notes="Interrupted")

    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest) -> None:
        if "evaluation_runs" in statement:
            statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        summary = (await admin_client.get("/api/v1/evaluations/summary")).json()
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)

    assert summary["evaluations"] == 4 and summary["failed"] == 1 and summary["running"] == 1
    assert summary["cases_run"] == 10, "cases nobody judged were counted as run"
    assert summary["avg_score"] == 0.7
    assert summary["regressions_caught"] == 1
    assert statements, "the summary did not read the evaluations at all"
    assert not [sql for sql in statements if "notes" in sql], (
        "the KPI scan materialised whole rows for cards that read five columns"
    )
