"""Regression tests for the 2026-09-18 audit of Testing & Regression.

Each test drives the product surface the way the console and the SDK do — a
dataset in, SDK-shaped traces with scorer verdicts in, Run Suite, then the
reads the screen makes — against the engine double, and fails without the fix
it is named for.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from sqlalchemy import event, select

from conftest import error_code, principal_for
from fulcrum_ops_api.models import quality as quality_models
from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.services import evaluations as evaluations_service
from fulcrum_ops_api.services import testing as service


def iso(offset: float = 0.0) -> str:
    base = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=3)
    return (base + dt.timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


async def scored_dataset(
    admin_client,
    ingest_client,
    name: str,
    verdicts: list[list[dict[str, Any]] | None],
    *,
    agent: str = "Support Bot",
) -> list[str]:
    """A dataset with one case per entry of ``verdicts``.

    Each entry is what an SDK experiment left on that case's trace: a list of
    feedback scores, an empty list for a trace nobody scored, or ``None`` for a
    case the SDK never ran (no trace at all). Returns the case ids in order.
    """
    created = await admin_client.post("/api/v1/evaluations/datasets", json={"name": name})
    assert created.status_code == 201, created.text
    added = await admin_client.post(
        f"/api/v1/evaluations/datasets/{name}/items",
        json={"items": [{"input": f"case {index}"} for index in range(len(verdicts))]},
    )
    assert added.status_code == 201, added.text
    listed = await admin_client.get(
        f"/api/v1/evaluations/datasets/{name}/items", params={"page_size": 100}
    )
    item_ids = [row["id"] for row in listed.json()["items"]]
    assert len(item_ids) == len(verdicts)

    for index, (item_id, scores) in enumerate(zip(item_ids, verdicts, strict=True)):
        if scores is None:
            continue
        trace: dict[str, Any] = {
            "name": f"experiment:{name}",
            "start_time": iso(index),
            "end_time": iso(index + 1.0),
            "tags": ["experiment", name],
            "metadata": {"dataset": name, "dataset_item_id": item_id},
        }
        if scores:
            trace["feedback_scores"] = scores
        posted = await ingest_client.post(
            "/api/v1/ingest/traces", json={"agent": agent, "traces": [trace]}
        )
        assert posted.json()["accepted"] == 1, posted.text
    return item_ids


async def new_suite(http, name: str, dataset: str, **extra: Any) -> str:
    created = await http.post(
        "/api/v1/testing/suites",
        json={
            "name": name,
            "suite_type": "Regression",
            "environment": "Staging",
            "dataset": dataset,
            "status": "Active",
            **extra,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def run_to_the_end(http, suite_id: str, *, seconds: float = 20.0) -> dict[str, Any]:
    """Click Run Suite, poll the progress bar like the modal does, return the run."""
    started = await http.post(f"/api/v1/testing/suites/{suite_id}/run", json={})
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    await until_terminal(http, suite_id, run_id, seconds=seconds)
    return (await http.get(f"/api/v1/testing/runs/{run_id}")).json()


async def until_terminal(http, suite_id: str, run_id: str, *, seconds: float = 20.0) -> dict:
    progress: dict[str, Any] = {}
    for _ in range(int(seconds / 0.2)):
        progress = (
            await http.get(f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/progress")
        ).json()
        if progress["is_terminal"]:
            return progress
        await asyncio.sleep(0.2)
    raise AssertionError(f"the run never reached a terminal state: {progress}")


# ---------------------------------------------------------------------------
# #44 -- the run row is durable before the runner is allowed to look for it
# ---------------------------------------------------------------------------


async def test_run_suite_commits_the_run_before_it_schedules_the_runner(
    admin_client, ingest_client, factory, workspace, engine, db_engine
):
    """On Postgres the runner's SELECT raced the request's COMMIT and, when it
    won, the run sat Queued for half an hour. SQLite cannot lose that race, so
    the order itself is what is pinned: at the commit that first carries the
    run row, the runner must not have been started yet."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client, ingest_client, "order-set", [[{"name": "accuracy", "value": 1.0}]]
    )
    suite_id = await new_suite(admin_client, "Order of events", "order-set")

    commits: list[tuple[int, frozenset[str]]] = []

    def on_commit(connection) -> None:
        rows = connection.exec_driver_sql("SELECT COUNT(*) FROM test_runs").scalar()
        commits.append((int(rows or 0), frozenset(service.runner._state)))

    event.listen(db_engine.sync_engine, "commit", on_commit)
    try:
        started = await admin_client.post(f"/api/v1/testing/suites/{suite_id}/run", json={})
        assert started.status_code == 202, started.text
        run_id = started.json()["id"]
        await until_terminal(admin_client, suite_id, run_id)
    finally:
        event.remove(db_engine.sync_engine, "commit", on_commit)

    carrying = next(known for rows, known in commits if rows >= 1)
    assert run_id not in carrying, (
        "the runner was started before the run row was committed; on Postgres it "
        "can look, find nothing, and leave the run Queued with nobody driving it"
    )


async def test_the_runner_waits_for_a_run_row_that_has_not_landed_yet(
    admin_client, ingest_client, factory, workspace, engine, admin, sessionmaker
):
    """The second guard: a runner handed a row its caller has not committed yet
    looks again instead of giving up silently."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client, ingest_client, "late-set", [[{"name": "accuracy", "value": 1.0}]]
    )
    suite_id = await new_suite(admin_client, "Late commit", "late-set")

    principal = principal_for(workspace, admin, Role.ADMIN)
    async with sessionmaker() as session:
        run = await service.start_run(session, principal, suite_id)
        run_id = run.id
        await service.execute_run(run_id, workspace.id)
        await asyncio.sleep(0.6)  # the runner has looked, and found nothing
        await session.commit()

    progress = await until_terminal(admin_client, suite_id, run_id)
    assert progress["status"] == "Passed", progress


# ---------------------------------------------------------------------------
# #45 -- a case is graded by whatever scorer the team wrote
# ---------------------------------------------------------------------------


async def test_a_case_scored_by_the_teams_own_scorer_is_graded(
    admin_client, ingest_client, factory, workspace, engine
):
    """The SDK names a score after the scorer function. The README's own
    example, ``contains_expected``, is none of the four published metrics, and
    every case it scored read as Unscored."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client,
        ingest_client,
        "own-scorer-set",
        [
            [{"name": "contains_expected", "value": 1.0}],
            [{"name": "contains_expected", "value": 0.0}],
            # A known metric still goes through its alias: a hallucination
            # score of 0.9 is a bad answer, not a good one.
            [{"name": "hallucination", "value": 0.9}, {"name": "latency_ok", "value": 1.0}],
        ],
    )
    suite_id = await new_suite(admin_client, "Own scorer", "own-scorer-set")

    detail = await run_to_the_end(admin_client, suite_id)
    assert detail["status"] == "Failed", detail
    assert [case["status"] for case in detail["cases"]] == ["Passed", "Failed", "Failed"]
    assert [case["score"] for case in detail["cases"]] == [1.0, 0.0, 0.1]
    assert detail["unscored"] == 0
    assert detail["pass_rate"] == 33.3


async def test_the_experiment_is_still_found_under_the_agents_later_traffic(
    admin_client, ingest_client, factory, workspace, engine, monkeypatch
):
    """The second way a scored case read as Unscored: the runner looked for the
    experiment's traces among each project's newest traces of any kind, so an
    agent with real traffic since the experiment pushed them out of the window.
    The window is shrunk here so eight production traces are enough to do it."""
    monkeypatch.setattr(evaluations_service, "TRACE_LINK_PAGE_SIZE", 5)
    monkeypatch.setattr(evaluations_service, "TRACE_LINK_MAX_UNFILTERED_TRACES", 5)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client,
        ingest_client,
        "busy-agent-set",
        [[{"name": "accuracy", "value": 0.9}], [{"name": "accuracy", "value": 0.2}]],
    )
    traffic = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {"name": "chat", "start_time": iso(100 + index), "end_time": iso(101 + index)}
                for index in range(8)
            ],
        },
    )
    assert traffic.json()["accepted"] == 8, traffic.text
    suite_id = await new_suite(admin_client, "Busy agent", "busy-agent-set")

    detail = await run_to_the_end(admin_client, suite_id)
    assert detail["status"] == "Failed", detail
    assert [case["status"] for case in detail["cases"]] == ["Passed", "Failed"]
    assert all(case["trace_id"] for case in detail["cases"])


# ---------------------------------------------------------------------------
# #46 -- the scoring wait ends when nothing more can arrive, and can be stopped
# ---------------------------------------------------------------------------


async def wait_for_phase(http, suite_id: str, run_id: str, phase: str) -> dict[str, Any]:
    progress: dict[str, Any] = {}
    for _ in range(100):
        progress = (
            await http.get(f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/progress")
        ).json()
        if progress["phase"] == phase:
            return progress
        await asyncio.sleep(0.1)
    raise AssertionError(f"the run never reached {phase}: {progress}")


async def supervisors_gone() -> None:
    """Give finished supervisors a moment to leave the loop before it closes."""
    for _ in range(100):
        if not service.runner._tasks:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"supervisors still running: {list(service.runner._tasks)}")


async def test_a_case_no_experiment_ever_ran_does_not_hold_the_run_open(
    admin_client, ingest_client, factory, workspace, engine
):
    """49 of 50 cases with SDK traces used to mean a fixed half-hour wait: the
    supervisor waited for a verdict on the fiftieth, and nothing produces one."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client,
        ingest_client,
        "one-unlinked-set",
        [
            [{"name": "accuracy", "value": 0.9}],
            None,  # the SDK experiment never ran this case
            [{"name": "accuracy", "value": 0.8}],
        ],
    )
    suite_id = await new_suite(admin_client, "One unlinked", "one-unlinked-set")

    detail = await run_to_the_end(admin_client, suite_id, seconds=10.0)
    assert detail["status"] == "Passed", detail
    assert sorted(case["status"] for case in detail["cases"]) == [
        "Passed", "Passed", "Unscored",
    ]
    assert detail["unscored"] == 1
    assert detail["pass_rate"] == 100.0, "an unjudged case is missing evidence, not a failure"


async def test_a_run_nothing_can_grade_says_why_at_once(
    admin_client, ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(admin_client, ingest_client, "never-run-set", [None, None])
    suite_id = await new_suite(admin_client, "Never run", "never-run-set")

    started = await admin_client.post(f"/api/v1/testing/suites/{suite_id}/run", json={})
    run_id = started.json()["id"]
    progress = await until_terminal(admin_client, suite_id, run_id, seconds=10.0)
    assert progress["status"] == "Error", progress
    assert "client.evaluate" in (progress["detail"] or ""), (
        "the modal must say what to do about it, not only that it failed"
    )


async def test_the_wait_settles_once_the_verdict_count_stops_moving(
    admin_client, ingest_client, factory, workspace, engine, monkeypatch
):
    """A linked trace that nobody scored cannot hold the run for the deadline."""
    monkeypatch.setattr(service, "SETTLE_SECONDS", 1.0)
    monkeypatch.setattr(service, "POLL_SECONDS", 0.2)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client,
        ingest_client,
        "unscored-trace-set",
        [[{"name": "accuracy", "value": 0.9}], []],  # the second ran, but was never scored
    )
    suite_id = await new_suite(admin_client, "Unscored trace", "unscored-trace-set")

    started = await admin_client.post(f"/api/v1/testing/suites/{suite_id}/run", json={})
    run_id = started.json()["id"]

    scoring = await wait_for_phase(admin_client, suite_id, run_id, "Scoring")
    assert scoring["processed_cases"] == 2
    assert scoring["scored_cases"] == 1, (
        "the experiment's trace_count is cases registered, not cases judged: "
        "reading it as 'scored' pinned the bar at 99% from the first poll"
    )
    assert (scoring["passed"], scoring["failed"]) == (1, 0)

    await until_terminal(admin_client, suite_id, run_id, seconds=10.0)
    detail = (await admin_client.get(f"/api/v1/testing/runs/{run_id}")).json()
    assert detail["status"] == "Passed", detail
    assert detail["unscored"] == 1


async def test_progress_served_by_another_worker_reads_the_run_row(
    admin_client, ingest_client, factory, workspace, engine, monkeypatch
):
    """Three of four workers do not own the run. They used to call the engine on
    every 1.4 s poll and answer Passed 0 / Failed 0, so the modal flickered."""
    monkeypatch.setattr(service, "SETTLE_SECONDS", 30.0)
    monkeypatch.setattr(service, "POLL_SECONDS", 0.2)
    monkeypatch.setattr(service, "POLL_MAX_SECONDS", 0.2)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client,
        ingest_client,
        "other-worker-set",
        [[{"name": "accuracy", "value": 0.9}], [{"name": "accuracy", "value": 0.1}], []],
    )
    suite_id = await new_suite(admin_client, "Other worker", "other-worker-set")
    started = await admin_client.post(f"/api/v1/testing/suites/{suite_id}/run", json={})
    run_id = started.json()["id"]
    await wait_for_phase(admin_client, suite_id, run_id, "Scoring")

    # A worker that does not own the run is a process with no live counters.
    service.runner._state.pop(run_id)
    experiment_reads = len(
        [call for call in engine.calls if call.method == "GET" and "/experiments/" in call.path]
    )
    elsewhere = (
        await admin_client.get(f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/progress")
    ).json()
    assert elsewhere["phase"] == "Scoring", elsewhere
    assert (elsewhere["scored_cases"], elsewhere["passed"], elsewhere["failed"]) == (2, 1, 1)
    assert elsewhere["processed_cases"] == 3
    assert experiment_reads == len(
        [call for call in engine.calls if call.method == "GET" and "/experiments/" in call.path]
    ), "a progress read must not cost an engine call"

    # ...and a cancel served there still reaches the supervisor: it reads the
    # row at its next poll.
    cancelled = await admin_client.post(
        f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    await supervisors_gone()
    final = (await admin_client.get(f"/api/v1/testing/runs/{run_id}")).json()
    assert final["status"] == "Cancelled", final


async def test_a_run_can_be_cancelled_and_the_suite_is_free_again(
    as_role, admin_client, ingest_client, factory, workspace, engine, db, monkeypatch
):
    monkeypatch.setattr(service, "SETTLE_SECONDS", 30.0)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client,
        ingest_client,
        "cancel-set",
        [[{"name": "accuracy", "value": 0.9}], []],
    )
    suite_id = await new_suite(admin_client, "Cancel me", "cancel-set")
    started = await admin_client.post(f"/api/v1/testing/suites/{suite_id}/run", json={})
    run_id = started.json()["id"]
    await wait_for_phase(admin_client, suite_id, run_id, "Scoring")

    # While it runs the suite is held, which is why a way out matters.
    again = await admin_client.post(f"/api/v1/testing/suites/{suite_id}/run", json={})
    assert again.status_code == 409, again.text

    async with as_role(Role.VIEWER) as viewer:
        refused = await viewer.post(
            f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/cancel"
        )
        assert refused.status_code == 403, refused.text

    async with as_role(Role.MEMBER) as member:
        cancelled = await member.post(
            f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/cancel"
        )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "Cancelled"
    assert cancelled.json()["result"] == "Cancelled"
    await supervisors_gone()

    progress = (
        await admin_client.get(f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/progress")
    ).json()
    assert progress["is_terminal"] is True
    assert progress["status"] == "Cancelled"
    assert progress["scored_cases"] == 1, "what was measured before the stop is kept"
    assert "Cancelled by" in progress["detail"]

    twice = await admin_client.post(
        f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/cancel"
    )
    assert twice.status_code == 409, twice.text
    assert error_code(twice) == "conflict"

    # A supervisor that reaches its finish line late must not bring the run back.
    await service.runner._finish(
        run_id,
        workspace.id,
        [{"id": "case-1", "name": "case 0", "status": "Passed", "score": 0.9}],
        1,
    )
    row = await db.get(quality_models.TestRun, run_id)
    assert row.status == "Cancelled"
    assert row.pass_rate is None

    actions = await db.scalars(
        select(AuditEvent.action).where(AuditEvent.entity_id == run_id)
    )
    assert "test_run.cancelled" in actions
    assert "test_run.completed" not in actions

    deleted = await admin_client.delete(f"/api/v1/testing/suites/{suite_id}")
    assert deleted.status_code == 204, deleted.text


# ---------------------------------------------------------------------------
# #147 -- a candidate is newer than the baseline; a comparison needs verdicts
# ---------------------------------------------------------------------------


async def baseline_row(http, suite_id: str) -> dict[str, Any]:
    listed = (await http.get("/api/v1/testing/baselines")).json()["items"]
    return next(row for row in listed if row["suite_id"] == suite_id)


async def test_the_candidate_is_never_a_run_older_than_the_baseline(
    admin_client, ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    item_ids = await scored_dataset(
        admin_client,
        ingest_client,
        "baseline-set",
        [[{"name": "accuracy", "value": 0.9}], [{"name": "accuracy", "value": 0.9}]],
    )
    suite_id = await new_suite(admin_client, "Baseline order", "baseline-set")

    first = await run_to_the_end(admin_client, suite_id)
    second = await run_to_the_end(admin_client, suite_id)
    assert (first["status"], second["status"]) == ("Passed", "Passed")

    # The normal flow: promote the newest run.
    promoted = await admin_client.post(
        f"/api/v1/testing/suites/{suite_id}/promote-baseline", json={}
    )
    assert promoted.json()["data"]["baseline_run_id"] == second["id"], promoted.text

    row = await baseline_row(admin_client, suite_id)
    assert row["baseline_run_id"] == second["id"]
    assert row["candidate_run_id"] is None, (
        f"the run before the baseline ({first['run_ref']}) was offered as the candidate: "
        "Promote Candidate would have walked the baseline backwards"
    )
    assert row["promotable"] is False
    assert row["pass_rate_delta"] is None

    # A later run in which the second case now fails is a real candidate.
    regressed = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    "name": "experiment:baseline-set",
                    "start_time": iso(60),
                    "end_time": iso(61),
                    "metadata": {"dataset": "baseline-set", "dataset_item_id": item_ids[1]},
                    "feedback_scores": [{"name": "accuracy", "value": 0.1}],
                }
            ],
        },
    )
    assert regressed.json()["accepted"] == 1, regressed.text
    third = await run_to_the_end(admin_client, suite_id)
    assert third["status"] == "Failed", third

    row = await baseline_row(admin_client, suite_id)
    assert row["candidate_run_id"] == third["id"]
    assert row["promotable"] is True
    assert row["pass_rate_delta"] == -50.0
    assert row["regressions"] == 1


async def test_compare_baselines_is_pointed_at_a_run_that_has_verdicts(
    admin_client, ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await scored_dataset(
        admin_client,
        ingest_client,
        "compare-set",
        [[{"name": "accuracy", "value": 0.9}], [{"name": "accuracy", "value": 0.2}]],
    )
    suite_id = await new_suite(admin_client, "Compare target", "compare-set")
    baseline = await run_to_the_end(admin_client, suite_id)
    await admin_client.post(f"/api/v1/testing/suites/{suite_id}/promote-baseline", json={})
    finished = await run_to_the_end(admin_client, suite_id)

    # The newest run errors: the store refuses the experiment.
    engine.fail_path("/experiments", 503)
    errored = await run_to_the_end(admin_client, suite_id)
    engine.recover()
    assert errored["status"] == "Error", errored

    suite = (await admin_client.get(f"/api/v1/testing/suites/{suite_id}")).json()
    assert suite["last_run_id"] == errored["id"]
    assert suite["last_finished_run_id"] == finished["id"], (
        "the console compares the baseline with this, not with whatever ran last"
    )

    bogus = await admin_client.get(
        "/api/v1/testing/compare",
        params={"baseline": baseline["id"], "candidate": errored["id"]},
    )
    assert bogus.status_code == 412, (
        "a run with no verdicts diffed as every case 'Removed' under a verdict of "
        f"'Unchanged': {bogus.text}"
    )

    real = await admin_client.get(
        "/api/v1/testing/compare",
        params={"baseline": baseline["id"], "candidate": finished["id"]},
    )
    assert real.status_code == 200, real.text
    assert real.json()["removed"] == 0
    assert real.json()["unchanged"] == 2


# ---------------------------------------------------------------------------
# #148 -- a cadence is an operator's call through every door
# ---------------------------------------------------------------------------


async def test_a_member_cannot_schedule_a_suite_through_the_suites_endpoints(
    as_role, admin_client, db, factory, workspace
):
    created = await admin_client.post(
        "/api/v1/evaluations/datasets", json={"name": "cadence-set"}
    )
    assert created.status_code == 201, created.text
    nightly = await factory.test_suite(
        workspace, name="Nightly regression", dataset_ref="cadence-set",
        schedule_cron="0 2 * * *",
    )
    body = {
        "name": "Member suite", "suite_type": "Regression", "environment": "Staging",
        "dataset": "cadence-set", "status": "Active",
    }

    async with as_role(Role.MEMBER) as member:
        refused = await member.post(
            "/api/v1/testing/suites", json={**body, "schedule_cron": "*/5 * * * *"}
        )
        assert refused.status_code == 403, refused.text
        assert error_code(refused) == "permission_denied"

        # The same suite without a cadence is still a member's to create...
        allowed = await member.post("/api/v1/testing/suites", json=body)
        assert allowed.status_code == 201, allowed.text
        own = allowed.json()["id"]

        # ...but not to put on a clock afterwards,
        patched = await member.patch(
            f"/api/v1/testing/suites/{own}", json={"schedule_cron": "* * * * *"}
        )
        assert patched.status_code == 403, patched.text
        # nor may a member re-point or clear the schedule an operator set.
        for change in ({"schedule_cron": "* * * * *"}, {"schedule_cron": None}):
            touched = await member.patch(f"/api/v1/testing/suites/{nightly.id}", json=change)
            assert touched.status_code == 403, touched.text
        kept = await db.get(quality_models.TestSuite, nightly.id)
        assert kept.schedule_cron == "0 2 * * *"

        # Everything else about a suite stays a member's to edit, including a
        # form that sends the cadence back exactly as it read it.
        renamed = await member.patch(
            f"/api/v1/testing/suites/{nightly.id}",
            json={"name": "Nightly regression v2", "schedule_cron": "0 2 * * *"},
        )
        assert renamed.status_code == 200, renamed.text

    async with as_role(Role.OPERATOR) as operator_http:
        moved = await operator_http.patch(
            f"/api/v1/testing/suites/{nightly.id}", json={"schedule_cron": "30 3 * * *"}
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["schedule_cron"] == "30 3 * * *"
        scheduled = await operator_http.post(
            "/api/v1/testing/suites",
            json={**body, "name": "Operator suite", "schedule_cron": "0 4 * * *"},
        )
        assert scheduled.status_code == 201, scheduled.text

    trail = await db.rows(
        select(AuditEvent.action, AuditEvent.event_metadata).where(
            AuditEvent.entity_id == nightly.id, AuditEvent.action == "test_suite.updated"
        )
    )
    cadence_changes = [meta for _action, meta in trail if (meta or {}).get("cron")]
    assert cadence_changes == [
        {"fields": ["schedule_cron"], "previous_cron": "0 2 * * *", "cron": "30 3 * * *"}
    ]
