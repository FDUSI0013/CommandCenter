"""The judged loop, end to end through the product surface.

An SDK experiment runs the agent over a dataset and scores each case onto a
real trace; the platform's evaluation then links those traces into its own
experiment, and the judged averages land on the Evaluations screen. These
tests drive exactly that sequence — dataset in, traces with scores in,
evaluation out — and pin the linking that makes it close.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid


def iso(offset: float = 0.0) -> str:
    base = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=3)
    return (base + dt.timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


async def test_an_evaluation_judges_from_the_sdk_experiments_traces(
    admin_client, ingest_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    created = await admin_client.post(
        "/api/v1/evaluations/datasets",
        json={"name": "golden-qa", "description": "Three canonical questions."},
    )
    assert created.status_code == 201, created.text
    added = await admin_client.post(
        "/api/v1/evaluations/datasets/golden-qa/items",
        json={
            "items": [
                {"input": "Where is my order?", "expected_output": "Cite the tracking link."},
                {"input": "Cancel my plan.", "expected_output": "Offer retention, then cancel."},
            ]
        },
    )
    assert added.status_code == 201, added.text

    listed = await admin_client.get("/api/v1/evaluations/datasets/golden-qa/items")
    item_ids = [row["id"] for row in listed.json()["items"]]
    assert len(item_ids) == 2

    # What client.experiments.evaluate() would leave behind: one trace per
    # case, stamped with the dataset item id, carrying the scorer's verdicts.
    for index, item_id in enumerate(item_ids):
        posted = await ingest_client.post(
            "/api/v1/ingest/traces",
            json={
                "agent": "Support Bot",
                "traces": [
                    {
                        "id": str(uuid.uuid4()),
                        "name": "experiment:golden-qa",
                        "start_time": iso(index),
                        "end_time": iso(index + 1.0),
                        "metadata": {"dataset": "golden-qa", "dataset_item_id": item_id},
                        "feedback_scores": [
                            {"name": "answer_correctness", "value": 0.9 - index * 0.1},
                            {"name": "groundedness", "value": 0.8},
                        ],
                    }
                ],
            },
        )
        assert posted.json()["accepted"] == 1, posted.text

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "golden-qa", "judge_model": "gpt-5"}
    )
    assert started.status_code == 202, started.text
    evaluation_id = started.json()["id"]

    for _ in range(40):
        progress = (
            await admin_client.get(f"/api/v1/evaluations/{evaluation_id}/progress")
        ).json()
        if progress["is_terminal"]:
            break
        await asyncio.sleep(0.5)

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["status"] == "Completed", detail.get("notes")
    assert detail["avg_score"] is not None
    assert detail["correctness"] is not None
    assert detail["grounding"] is not None

    trend = (await admin_client.get("/api/v1/evaluations/trend")).json()
    assert any(point["evaluations"] for point in trend["points"]), (
        "the judged run must appear on the trend"
    )


async def test_dataset_items_carry_their_provenance(admin_client, engine):
    """The engine refuses source-less cases; the adapter must send one."""
    created = await admin_client.post(
        "/api/v1/evaluations/datasets", json={"name": "provenance-check"}
    )
    assert created.status_code == 201, created.text
    added = await admin_client.post(
        "/api/v1/evaluations/datasets/provenance-check/items",
        json={"items": [{"input": "case one"}]},
    )
    assert added.status_code == 201, added.text

    dataset = next(iter(engine.dataset_items.values()))
    assert dataset[0].get("source") == "manual"


async def test_the_newest_run_per_case_is_the_one_that_judges(
    admin_client, ingest_client, factory, workspace, engine
):
    """A case run twice links its newest trace, whatever order the stream uses."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    created = await admin_client.post(
        "/api/v1/evaluations/datasets", json={"name": "rerun-set"}
    )
    assert created.status_code == 201, created.text
    await admin_client.post(
        "/api/v1/evaluations/datasets/rerun-set/items",
        json={"items": [{"input": "only case"}]},
    )
    item_id = (await admin_client.get("/api/v1/evaluations/datasets/rerun-set/items")).json()[
        "items"
    ][0]["id"]

    async def run_case(offset: int, scores):
        body = {
            "agent": "Support Bot",
            "traces": [
                {
                    "name": "experiment:rerun-set",
                    "start_time": iso(offset),
                    "end_time": iso(offset + 1.0),
                    "metadata": {"dataset_item_id": item_id},
                }
            ],
        }
        if scores:
            body["traces"][0]["feedback_scores"] = scores
        posted = await ingest_client.post("/api/v1/ingest/traces", json=body)
        assert posted.json()["accepted"] == 1, posted.text

    await run_case(0, None)  # the first run produced no verdicts
    await run_case(5, [{"name": "answer_correctness", "value": 0.75}])

    started = await admin_client.post(
        "/api/v1/evaluations", json={"dataset": "rerun-set", "judge_model": "gpt-5"}
    )
    evaluation_id = started.json()["id"]
    for _ in range(40):
        progress = (
            await admin_client.get(f"/api/v1/evaluations/{evaluation_id}/progress")
        ).json()
        if progress["is_terminal"]:
            break
        await asyncio.sleep(0.5)

    detail = (await admin_client.get(f"/api/v1/evaluations/{evaluation_id}")).json()
    assert detail["status"] == "Completed", detail.get("notes")
    assert detail["correctness"] == 0.75, "the scored (newest) run judges the case"


async def test_a_suite_run_grades_every_case_and_promotes_a_baseline(
    admin_client, ingest_client, factory, workspace, engine
):
    """The full regression loop: scored traces in, per-case verdicts out.

    This is the path production shipped broken — the runner read the plain
    items listing, which carries no experiment results, and graded every case
    Unscored. The comparison endpoint is the one that joins the verdicts.
    """
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    created = await admin_client.post(
        "/api/v1/evaluations/datasets", json={"name": "suite-set"}
    )
    assert created.status_code == 201, created.text
    await admin_client.post(
        "/api/v1/evaluations/datasets/suite-set/items",
        json={"items": [{"input": "case A"}, {"input": "case B"}]},
    )
    items = (await admin_client.get("/api/v1/evaluations/datasets/suite-set/items")).json()[
        "items"
    ]
    for index, item in enumerate(items):
        posted = await ingest_client.post(
            "/api/v1/ingest/traces",
            json={
                "agent": "Support Bot",
                "traces": [
                    {
                        "name": "experiment:suite-set",
                        "start_time": iso(index),
                        "end_time": iso(index + 1.0),
                        "metadata": {"dataset_item_id": item["id"]},
                        "feedback_scores": [
                            {"name": "answer_correctness", "value": 0.9 if index == 0 else 0.2}
                        ],
                    }
                ],
            },
        )
        assert posted.json()["accepted"] == 1, posted.text

    suite = await admin_client.post(
        "/api/v1/testing/suites",
        json={
            "name": "Suite loop", "suite_type": "Regression",
            "environment": "Staging", "dataset": "suite-set", "status": "Active",
        },
    )
    assert suite.status_code == 201, suite.text
    suite_id = suite.json()["id"]

    started = await admin_client.post(
        f"/api/v1/testing/suites/{suite_id}/run", json={"trigger": "Manual"}
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    for _ in range(60):
        progress = (
            await admin_client.get(
                f"/api/v1/testing/suites/{suite_id}/runs/{run_id}/progress"
            )
        ).json()
        if progress["is_terminal"]:
            break
        await asyncio.sleep(0.5)

    detail = (await admin_client.get(f"/api/v1/testing/runs/{run_id}")).json()
    assert detail["status"] == "Failed", detail  # one case under 0.5 -> run Failed
    assert detail["pass_rate"] == 50.0
    statuses = sorted(case["status"] for case in detail["cases"])
    assert statuses == ["Failed", "Passed"], statuses
    assert all(case.get("trace_id") for case in detail["cases"]), (
        "each verdict must cite the trace it was judged from"
    )

    promoted = await admin_client.post(
        f"/api/v1/testing/suites/{suite_id}/promote-baseline", json={}
    )
    assert promoted.status_code == 200, promoted.text
