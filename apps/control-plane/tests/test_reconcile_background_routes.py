"""Regression tests for the hand-offs reconciled into the background routes.

One was found by the owner of the console's quality screens during the
2026-09-18 fix pass and needed a change in ``api/v1/testing.py``:

* the Test Runs tab grew an Export button, and the route behind it took one
  200-row page and called that the export -- so a workspace with a few weeks of
  scheduled runs downloaded a file that stopped part-way and said nothing;
* the tab now shows how a run ended (Status) beside what its pass rate earned
  (Verdict) and filters on the former, while the file carried only the latter,
  so rows exported under Status = Failed could read "Passed".

Everything goes through the real request path, as the rest of the suite does.
"""

from __future__ import annotations

import csv
import io

from fulcrum_ops_api.api.common import MAX_PAGE_SIZE
from fulcrum_ops_api.models.quality import TestRun as RunRow
from fulcrum_ops_api.models.quality import TestRunStatus as RunStatus


def _exported(response) -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(response.text)))


def _run(workspace, suite, ref: str, **fields) -> RunRow:
    values = {
        "status": RunStatus.PASSED.value,
        "total_cases": 4,
        "passed": 4,
        "pass_rate": 100.0,
        "trigger": "Manual",
        "summary": {},
        "baseline_comparison": {},
    }
    values.update(fields)
    return RunRow(workspace_id=workspace.id, suite_id=suite.id, run_ref=ref, **values)


# ---------------------------------------------------------------------------
# 146 - the runs export is every run the tab can page to, not its first page
# ---------------------------------------------------------------------------


async def test_the_runs_export_does_not_stop_at_one_page(admin_client, factory, workspace):
    suite = await factory.test_suite(workspace, name="Nightly regression")
    count = MAX_PAGE_SIZE + 7
    await factory.add_all([_run(workspace, suite, f"tr-{n:04d}") for n in range(count)])

    listed = await admin_client.get("/api/v1/testing/runs", params={"page_size": 1})
    assert listed.json()["total"] == count

    rows = _exported(await admin_client.get("/api/v1/testing/runs/export"))

    assert len(rows) == count, "the file must carry every run the table counts"
    assert {row["Run ID"] for row in rows} == {f"tr-{n:04d}" for n in range(count)}


async def test_the_runs_export_still_honours_the_tabs_filters(admin_client, factory, workspace):
    suite = await factory.test_suite(workspace, name="Nightly regression")
    other = await factory.test_suite(workspace, name="Load")
    await factory.add_all(
        [
            _run(workspace, suite, "tr-manual"),
            _run(workspace, suite, "tr-nightly", trigger="Schedule"),
            _run(workspace, other, "tr-load", trigger="Schedule"),
        ]
    )

    rows = _exported(
        await admin_client.get(
            "/api/v1/testing/runs/export",
            params={"suite_id": suite.id, "trigger": "Schedule"},
        )
    )

    assert [row["Run ID"] for row in rows] == ["tr-nightly"]


# ---------------------------------------------------------------------------
# 146 - the file says how a run ended as well as what its pass rate earned
# ---------------------------------------------------------------------------


async def test_the_runs_export_carries_status_beside_the_verdict(
    admin_client, factory, workspace
):
    suite = await factory.test_suite(workspace, name="Nightly regression")
    await factory.add_all(
        [
            # One failing case in twenty: the run Failed, the verdict is Passed.
            _run(
                workspace,
                suite,
                "tr-mostly",
                status=RunStatus.FAILED.value,
                total_cases=20,
                passed=19,
                failed=1,
                pass_rate=95.0,
            ),
            _run(workspace, suite, "tr-clean"),
        ]
    )

    response = await admin_client.get(
        "/api/v1/testing/runs/export", params={"status": RunStatus.FAILED.value}
    )
    rows = _exported(response)

    header = response.text.splitlines()[0].split(",")
    assert header.count("Status") == 1
    assert header.index("Status") + 1 == header.index("Result"), "Status sits next to Result"

    # What the operator filtered on is in the file, so the file explains itself.
    assert [(row["Run ID"], row["Status"], row["Result"]) for row in rows] == [
        ("tr-mostly", "Failed", "Passed")
    ]
