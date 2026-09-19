"""Regressions for the 2026-09-18 audit of the Feedback & Quality Loop.

Each test pins one thing the screen claimed and the service did not do: a
rating "scored onto the trace" that was filed under a project no run lives in,
an issue that could be planned twice and then could not be planned at all, new
reports that never reached the theme or the issue they were about, a PII
switch that scrubbed nothing, an auto-issue threshold that opened nothing, and
a clustering pass that held the event loop while it ran.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from sqlalchemy import select

from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.quality import BacklogItem, FeedbackIssue, FeedbackItem

FEEDBACK = "/api/v1/feedback"
SCORES_PATH = "/traces/feedback-scores"


# ---------------------------------------------------------------------------
# 149 — the mirror files the score under the run's own project, or not at all
# ---------------------------------------------------------------------------


async def test_feedback_naming_no_agent_is_scored_under_the_runs_own_project(
    admin_client, factory, workspace, engine, db
):
    """'Not about a specific agent' used to send the workspace name as a project."""
    agent = await factory.provisioned_agent(workspace, engine, name="Invoice Bot")
    trace = engine.add_trace(project_name=agent.engine_project_name, name="extract totals")

    submitted = await admin_client.post(
        FEEDBACK, json={"trace_id": trace["id"], "rating": 1, "body": "Total was wrong"}
    )
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert body["scored_in_telemetry"] is True

    sent = engine.calls_to(SCORES_PATH)[-1].body["scores"][0]
    assert sent["project_name"] == agent.engine_project_name
    assert sent["project_name"] != workspace.engine_workspace

    # The run knows which agent produced it, so the row does too.
    assert body["agent_id"] == agent.id
    stored = await db.scalar(select(FeedbackItem).where(FeedbackItem.id == body["id"]))
    assert stored.agent_id == agent.id


async def test_the_wrong_agent_in_the_dialog_does_not_misfile_the_score(
    admin_client, factory, workspace, engine
):
    """The project comes from the trace, not from the dropdown."""
    right = await factory.provisioned_agent(workspace, engine, name="Invoice Bot")
    wrong = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace = engine.add_trace(project_name=right.engine_project_name, name="extract totals")

    submitted = await admin_client.post(
        FEEDBACK, json={"trace_id": trace["id"], "agent_id": wrong.id, "rating": 2}
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["scored_in_telemetry"] is True
    # What the submitter said the feedback is about is theirs to say.
    assert submitted.json()["agent_id"] == wrong.id

    sent = engine.calls_to(SCORES_PATH)[-1].body["scores"][0]
    assert sent["project_name"] == right.engine_project_name


async def test_a_run_outside_the_workspace_is_never_scored(
    admin_client, factory, workspace, other_workspace, engine
):
    """Quoting another tenant's run id must not put a score on it."""
    mine = await factory.provisioned_agent(workspace, engine, name="Invoice Bot")
    theirs = await factory.provisioned_agent(other_workspace, engine, name="Their Bot")
    foreign = engine.add_trace(project_name=theirs.engine_project_name, name="their run")

    submitted = await admin_client.post(
        FEEDBACK, json={"trace_id": foreign["id"], "agent_id": mine.id, "rating": 1}
    )
    assert submitted.status_code == 201, "the capture is ours and still succeeds"
    assert submitted.json()["scored_in_telemetry"] is False
    assert engine.calls_to(SCORES_PATH) == []
    assert engine.traces[foreign["id"]]["feedback_scores"] == []


async def test_a_run_the_store_does_not_have_reports_not_mirrored(
    admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Invoice Bot")

    submitted = await admin_client.post(
        FEEDBACK,
        json={"trace_id": "0198f3a2-0000-7000-8000-00000000dead", "rating": 4},
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["scored_in_telemetry"] is False
    assert engine.calls_to(SCORES_PATH) == [], "nothing is written for a run nobody has"


async def test_a_trace_id_that_is_not_an_id_never_reaches_the_store(
    admin_client, factory, workspace, engine
):
    """The ownership check reads the run by id, so the id is part of a path."""
    await factory.provisioned_agent(workspace, engine, name="Invoice Bot")
    engine.reset_calls()

    submitted = await admin_client.post(
        FEEDBACK, json={"trace_id": "../projects?name=x", "rating": 2}
    )
    assert submitted.status_code == 201, "free text is still a capture"
    assert submitted.json()["scored_in_telemetry"] is False
    assert engine.calls == [], "nothing that is not a run id is sent anywhere"


async def test_a_store_that_cannot_be_read_degrades_the_mirror_not_the_capture(
    admin_client, factory, workspace, engine, db
):
    agent = await factory.provisioned_agent(workspace, engine, name="Invoice Bot")
    trace = engine.add_trace(project_name=agent.engine_project_name, name="extract totals")
    engine.fail(503)

    submitted = await admin_client.post(
        FEEDBACK, json={"trace_id": trace["id"], "rating": 1, "body": "Total was wrong"}
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["scored_in_telemetry"] is False
    assert engine.calls_to(SCORES_PATH) == [], "no project is guessed for an unread run"
    assert await db.count(FeedbackItem, FeedbackItem.id == submitted.json()["id"]) == 1


async def test_an_agent_bound_key_cannot_score_a_neighbours_run(
    app, factory, workspace, engine
):
    import httpx

    from conftest import APP_BASE_URL

    own = await factory.provisioned_agent(workspace, engine, name="Invoice Bot")
    neighbour = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    trace = engine.add_trace(project_name=neighbour.engine_project_name, name="their run")
    token, _row = await factory.api_key(
        workspace, name="Invoice key", scopes=["ingest"], agent_id=own.id
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_BASE_URL
    ) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        submitted = await http.post(FEEDBACK, json={"trace_id": trace["id"], "rating": 1})

    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["scored_in_telemetry"] is False
    assert engine.traces[trace["id"]]["feedback_scores"] == []


# ---------------------------------------------------------------------------
# 150 — one backlog item plans an issue, whichever button asked for it
# ---------------------------------------------------------------------------


async def _issue_with_reports(http, factory, workspace, *, reports: int = 3) -> tuple[dict, list]:
    rows = [
        await factory.feedback_item(
            workspace,
            rating=1,
            sentiment="Negative",
            body=f"Invoice total extracted wrong again ({index})",
        )
        for index in range(reports)
    ]
    opened = await http.post(
        f"{FEEDBACK}/issues",
        json={"title": "Invoice total wrong", "feedback_ids": [row.id for row in rows]},
    )
    assert opened.status_code == 201, opened.text
    assert opened.json()["feedback_count"] == reports
    return opened.json(), rows


async def test_add_to_backlog_from_two_reports_of_one_issue_makes_one_item(
    admin_client, factory, workspace, db
):
    issue, rows = await _issue_with_reports(admin_client, factory, workspace)

    first = await admin_client.post(
        f"{FEEDBACK}/backlog", json={"title": "Fix totals", "feedback_id": rows[0].id}
    )
    assert first.status_code == 201, first.text
    assert first.json()["issue_id"] == issue["id"]

    second = await admin_client.post(
        f"{FEEDBACK}/backlog", json={"title": "Fix totals", "feedback_id": rows[1].id}
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["details"]["backlog_item_id"] == first.json()["id"]

    # And the Issues tab's own button gives the designed 409, not a 500.
    promoted = await admin_client.post(f"{FEEDBACK}/issues/{issue['id']}/backlog", json={})
    assert promoted.status_code == 409, promoted.text
    assert promoted.json()["error"]["details"]["backlog_item_id"] == first.json()["id"]

    assert await db.count(BacklogItem, BacklogItem.issue_id == issue["id"]) == 1


async def test_an_issue_that_already_has_two_items_still_answers_409(
    admin_client, factory, workspace
):
    """Workspaces that forked an issue before the check existed must not 500."""
    issue, _rows = await _issue_with_reports(admin_client, factory, workspace)
    older = await factory.add(
        BacklogItem(workspace_id=workspace.id, issue_id=issue["id"], title="Fix totals")
    )
    await factory.add(
        BacklogItem(workspace_id=workspace.id, issue_id=issue["id"], title="Fix totals (again)")
    )

    promoted = await admin_client.post(f"{FEEDBACK}/issues/{issue['id']}/backlog", json={})
    assert promoted.status_code == 409, promoted.text
    assert promoted.json()["error"]["details"]["backlog_item_id"] == older.id

    # The issue row names the same item the conflict does, not an arbitrary one.
    read = await admin_client.get(f"{FEEDBACK}/issues/{issue['id']}")
    assert read.json()["backlog_item_id"] == older.id


async def test_a_reopened_issue_can_be_planned_again_after_its_fix_shipped(
    admin_client, factory, workspace
):
    issue, _rows = await _issue_with_reports(admin_client, factory, workspace)
    planned = await admin_client.post(f"{FEEDBACK}/issues/{issue['id']}/backlog", json={})
    assert planned.status_code == 201, planned.text
    item_id = planned.json()["id"]

    for status in ("In Progress", "Done"):
        moved = await admin_client.patch(f"{FEEDBACK}/backlog/{item_id}", json={"status": status})
        assert moved.status_code == 200, moved.text

    reopened = await admin_client.patch(
        f"{FEEDBACK}/issues/{issue['id']}", json={"status": "In Progress"}
    )
    assert reopened.status_code == 200, reopened.text

    again = await admin_client.post(f"{FEEDBACK}/issues/{issue['id']}/backlog", json={})
    assert again.status_code == 201, "Done is terminal, so the regression needs a new row"
    assert again.json()["id"] != item_id

    read = await admin_client.get(f"{FEEDBACK}/issues/{issue['id']}")
    assert read.json()["backlog_item_id"] == again.json()["id"]


# ---------------------------------------------------------------------------
# 151 — a new report of a known problem reaches its theme and its issue
# ---------------------------------------------------------------------------

INVOICE_REPORTS = (
    "Invoice total extraction failed on the scanned invoice",
    "Invoice total extraction failed again",
    "The invoice total extraction failed for a scanned PDF",
)


async def _report(factory, workspace, body: str, **fields):
    return await factory.feedback_item(
        workspace, rating=1, sentiment="Negative", body=body, **fields
    )


async def test_a_new_report_joins_the_existing_theme_and_its_open_issue(
    admin_client, factory, workspace
):
    for body in INVOICE_REPORTS:
        await _report(factory, workspace, body)

    first_pass = await admin_client.post(f"{FEEDBACK}/analyze", json={})
    assert first_pass.status_code == 200, first_pass.text
    (cluster,) = first_pass.json()["clusters"]
    assert cluster["size"] == 3
    theme = cluster["theme"]

    opened = await admin_client.post(
        f"{FEEDBACK}/issues",
        json={"title": cluster["suggested_issue_title"], "cluster_id": cluster["cluster_id"]},
    )
    assert opened.status_code == 201, opened.text
    issue = opened.json()
    assert issue["feedback_count"] == 3

    planned = await admin_client.post(f"{FEEDBACK}/issues/{issue['id']}/backlog", json={})
    assert planned.status_code == 201, planned.text
    assert planned.json()["votes"] == 3

    # One more user hits the same problem. Alone it is below min_cluster_size,
    # and it used to stay "unclustered", and unlinked, for ever.
    late = await _report(factory, workspace, "Invoice total extraction failed for my supplier")

    second_pass = await admin_client.post(f"{FEEDBACK}/analyze", json={})
    assert second_pass.status_code == 200, second_pass.text
    result = second_pass.json()
    assert result["clustered"] == 1
    assert result["unclustered"] == 0
    assert result["linked_to_issues"] == 1
    (joined,) = result["clusters"]
    assert joined["theme"] == theme, "the persisted label is kept, not re-derived"
    assert joined["size"] == 4
    assert joined["new_members"] == 1
    assert joined["open_issue_id"] == issue["id"]
    assert joined["open_issue_ref"] == issue["issue_ref"]

    row = await admin_client.get(f"{FEEDBACK}/{late.id}")
    assert row.json()["theme"] == theme
    assert row.json()["issue_id"] == issue["id"]

    read = await admin_client.get(f"{FEEDBACK}/issues/{issue['id']}")
    assert read.json()["feedback_count"] == 4
    assert read.json()["reports_30d"] == 4

    backlog = await admin_client.get(f"{FEEDBACK}/backlog")
    assert backlog.json()["items"][0]["votes"] == 4, "demand is counted, not frozen"


async def test_an_unrelated_report_does_not_join_a_theme(admin_client, factory, workspace):
    for body in INVOICE_REPORTS:
        await _report(factory, workspace, body)
    await admin_client.post(f"{FEEDBACK}/analyze", json={})

    stranger = await _report(factory, workspace, "Login page keeps timing out on mobile")
    second_pass = await admin_client.post(f"{FEEDBACK}/analyze", json={})
    assert second_pass.json()["clustered"] == 0
    assert second_pass.json()["unclustered"] == 1
    assert second_pass.json()["clusters"] == []

    row = await admin_client.get(f"{FEEDBACK}/{stranger.id}")
    assert row.json()["theme"] is None


async def test_reports_themed_before_the_issue_linked_anything_are_caught_up(
    admin_client, factory, workspace
):
    """Rows an earlier pass themed, but no pass linked, reach the issue too."""
    for body in INVOICE_REPORTS:
        await _report(factory, workspace, body)
    (cluster,) = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()["clusters"]
    issue = (
        await admin_client.post(
            f"{FEEDBACK}/issues", json={"title": "Totals", "cluster_id": cluster["cluster_id"]}
        )
    ).json()

    # Themed by hand (or by the pass as it used to be) and never linked.
    await _report(
        factory, workspace, "Invoice total extraction failed yesterday", theme=cluster["theme"]
    )
    await _report(factory, workspace, "Invoice total extraction failed on a credit note")

    result = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()
    assert result["clustered"] == 1
    assert result["linked_to_issues"] == 2

    read = await admin_client.get(f"{FEEDBACK}/issues/{issue['id']}")
    assert read.json()["feedback_count"] == 5


async def test_the_catch_up_does_not_wait_for_a_new_report_of_the_theme(
    admin_client, factory, workspace
):
    """An issue opened before reports were linked is owed them by the next pass."""
    for body in INVOICE_REPORTS:
        await _report(factory, workspace, body)
    (cluster,) = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()["clusters"]
    issue = (
        await admin_client.post(
            f"{FEEDBACK}/issues", json={"title": "Totals", "cluster_id": cluster["cluster_id"]}
        )
    ).json()
    for index in range(2):
        await _report(
            factory,
            workspace,
            f"Invoice total extraction failed on batch {index}",
            theme=cluster["theme"],
        )

    # Nothing here is unthemed, so the pass clusters nothing — and still links.
    result = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()
    assert result["analysed"] == 0
    assert result["linked_to_issues"] == 2

    read = await admin_client.get(f"{FEEDBACK}/issues/{issue['id']}")
    assert read.json()["feedback_count"] == 5
    assert read.json()["reports_30d"] == 5


# ---------------------------------------------------------------------------
# 152 — the settings the last tab saves are settings the service obeys
# ---------------------------------------------------------------------------

PII_COMMENT = (
    "Wrong total. My SSN is 123-45-6789, card 4111 1111 1111 1111, "
    "reach me at jane.doe@example.com or (415) 555-0134"
)
PII_VALUES = ("123-45-6789", "4111 1111 1111 1111", "jane.doe@example.com", "555-0134")


async def test_pii_scrubbing_on_scrubs_the_row_the_mirror_and_the_export(
    admin_client, factory, workspace, engine, db
):
    """'PII scrubbing: Enabled' is the default, and used to scrub nothing."""
    agent = await factory.provisioned_agent(workspace, engine, name="Invoice Bot")
    trace = engine.add_trace(project_name=agent.engine_project_name, name="extract totals")

    submitted = await admin_client.post(
        FEEDBACK, json={"trace_id": trace["id"], "rating": 1, "body": PII_COMMENT}
    )
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert body["pii_scrubbed"] == ["email", "ssn", "card", "phone"]
    assert body["body"].startswith("Wrong total."), "only the identifiers go"

    stored = await db.scalar(select(FeedbackItem).where(FeedbackItem.id == body["id"]))
    reason = engine.calls_to(SCORES_PATH)[-1].body["scores"][0]["reason"]
    exported = await admin_client.get(f"{FEEDBACK}/export")
    assert exported.status_code == 200, exported.text
    for value in PII_VALUES:
        assert value not in stored.body
        assert value not in reason, "the mirror copies the comment into the telemetry store"
        assert value not in exported.text
        assert value not in str(stored.event_metadata), "the kinds are kept, never the values"


async def test_pii_scrubbing_off_keeps_the_comment_as_written(admin_client):
    saved = await admin_client.put(f"{FEEDBACK}/settings", json={"pii_scrubbing": False})
    assert saved.status_code == 200, saved.text

    submitted = await admin_client.post(FEEDBACK, json={"rating": 2, "body": PII_COMMENT})
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["body"] == PII_COMMENT
    assert submitted.json()["pii_scrubbed"] == []


async def test_a_comment_without_identifiers_is_stored_untouched(admin_client):
    comment = "Invoice 2024-0012-3345 was wrong on 2026-09-18; order 1234567890123, v1.2.3.4"
    submitted = await admin_client.post(FEEDBACK, json={"rating": 2, "body": comment})
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["body"] == comment
    assert submitted.json()["pii_scrubbed"] == []


async def test_the_scrub_marker_is_not_something_two_comments_have_in_common(admin_client):
    """Two unrelated complaints that both left an address are not a theme."""
    for comment in (
        "Refund never arrived, write to jane.doe@example.com",
        "Login crashes, write to bob@example.com",
    ):
        submitted = await admin_client.post(FEEDBACK, json={"rating": 1, "body": comment})
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["pii_scrubbed"] == ["email"]

    result = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()
    assert result["analysed"] == 2
    assert result["clustered"] == 0, "the marker's own words must not count as overlap"
    assert result["clusters"] == []


async def _auto_issue_at(http, threshold: int) -> None:
    saved = await http.put(f"{FEEDBACK}/sla-rules", json={"auto_issue_threshold": threshold})
    assert saved.status_code == 200, saved.text


async def test_a_theme_at_the_auto_issue_threshold_gets_its_issue_opened(
    admin_client, factory, workspace, db
):
    """'Issue creation: Auto for >= N similar reports' used to colour a badge."""
    await _auto_issue_at(admin_client, 3)
    for body in INVOICE_REPORTS:
        await _report(factory, workspace, body)

    result = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()
    assert result["issues_auto_opened"] == 1
    (cluster,) = result["clusters"]
    assert cluster["issue_auto_opened"] is True
    assert cluster["meets_auto_issue_threshold"] is True

    issues = (await admin_client.get(f"{FEEDBACK}/issues")).json()["items"]
    assert [issue["id"] for issue in issues] == [cluster["open_issue_id"]]
    assert issues[0]["theme"] == cluster["theme"]
    assert issues[0]["feedback_count"] == 3
    assert issues[0]["sla_due_at"] is not None, "an auto-opened issue is on the clock too"
    assert "automatically" in issues[0]["description"]

    trail = await db.scalars(
        select(AuditEvent).where(AuditEvent.action == "feedback.issue_created")
    )
    assert len(trail) == 1 and "automatically" in trail[0].detail

    # The next pass finds the issue open and does not open a second one.
    await _report(factory, workspace, "Invoice total extraction failed for my supplier")
    again = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()
    assert again["issues_auto_opened"] == 0
    assert again["linked_to_issues"] == 1
    assert again["clusters"][0]["issue_auto_opened"] is False
    assert again["clusters"][0]["open_issue_id"] == issues[0]["id"]
    assert await db.count(FeedbackIssue) == 1


async def test_create_issue_on_a_theme_the_pass_already_opened_names_that_issue(
    admin_client, factory, workspace, db
):
    """The Analyze card's Create Issue button must not fork an empty second issue."""
    await _auto_issue_at(admin_client, 3)
    for body in INVOICE_REPORTS:
        await _report(factory, workspace, body)
    (cluster,) = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()["clusters"]
    assert cluster["issue_auto_opened"] is True

    forked = await admin_client.post(
        f"{FEEDBACK}/issues",
        json={
            "title": cluster["suggested_issue_title"],
            "theme": cluster["theme"],
            "cluster_id": cluster["cluster_id"],
        },
    )
    assert forked.status_code == 409, forked.text
    details = forked.json()["error"]["details"]
    assert details["issue_id"] == cluster["open_issue_id"]
    assert details["issue_ref"] == cluster["open_issue_ref"]
    assert await db.count(FeedbackIssue) == 1

    # Once that issue is closed the theme is nobody's, and may be opened again.
    resolved = await admin_client.patch(
        f"{FEEDBACK}/issues/{cluster['open_issue_id']}", json={"status": "Resolved"}
    )
    assert resolved.status_code == 200, resolved.text
    await _report(factory, workspace, "Invoice total extraction failed for my supplier")
    await admin_client.post(f"{FEEDBACK}/analyze", json={})
    reopened = await admin_client.post(
        f"{FEEDBACK}/issues", json={"title": "Totals, again", "theme": cluster["theme"]}
    )
    assert reopened.status_code == 201, reopened.text
    assert reopened.json()["feedback_count"] == 1


async def test_praise_and_small_themes_do_not_open_issues(admin_client, factory, workspace, db):
    await _auto_issue_at(admin_client, 3)
    for body in (
        "Summary feature saved my whole afternoon",
        "The summary feature saved the afternoon",
        "Summary feature saved another afternoon",
    ):
        await factory.feedback_item(workspace, rating=5, sentiment="Positive", body=body)
    for body in INVOICE_REPORTS[:2]:
        await _report(factory, workspace, body)

    result = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()
    assert len(result["clusters"]) == 2
    assert result["issues_auto_opened"] == 0
    assert all(not cluster["meets_auto_issue_threshold"] for cluster in result["clusters"])
    assert await db.count(FeedbackIssue) == 0


async def test_reports_a_resolved_issue_answered_do_not_reopen_the_theme(
    admin_client, factory, workspace, db
):
    await _auto_issue_at(admin_client, 3)
    for body in INVOICE_REPORTS:
        await _report(factory, workspace, body)
    (cluster,) = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()["clusters"]
    resolved = await admin_client.patch(
        f"{FEEDBACK}/issues/{cluster['open_issue_id']}", json={"status": "Resolved"}
    )
    assert resolved.status_code == 200, resolved.text

    # One straggler after the fix: it joins the theme, but one unanswered report
    # is not three, and the three the fix answered must not count again.
    await _report(factory, workspace, "Invoice total extraction failed for my supplier")
    again = (await admin_client.post(f"{FEEDBACK}/analyze", json={})).json()
    assert again["clustered"] == 1
    assert again["issues_auto_opened"] == 0
    assert again["clusters"][0]["open_issue_id"] is None
    assert await db.count(FeedbackIssue) == 1


# ---------------------------------------------------------------------------
# 217 — the clustering pass is arithmetic, and the worker serves others meanwhile
# ---------------------------------------------------------------------------

#: Ten words every comment below shares, and twenty-two no other comment has: an
#: overlap of 10/54, well under the similarity threshold. It is the pass's worst
#: case — every comment is compared with every leader and joins none.
_SHARED_WORDS = (
    "checkout payment refund invoice shipping account currency supplier receipt voucher"
)
_STALL_ROWS = 1500


def _lonely_comment(index: int) -> str:
    return _SHARED_WORDS + " " + " ".join(f"w{index:04d}k{word:02d}" for word in range(22))


async def _longest_stall(stop: asyncio.Event) -> float:
    """The longest the event loop went without running this task."""
    longest = 0.0
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(0.005)
        now = time.perf_counter()
        longest = max(longest, now - last)
        last = now
    return longest


async def test_the_clustering_pass_does_not_hold_the_event_loop(
    admin_client, workspace, sessionmaker
):
    """Everything else on the worker — ingest, live-runs streams — shares this loop."""
    now = dt.datetime.now(dt.UTC)
    async with sessionmaker() as session:
        session.add_all(
            FeedbackItem(
                workspace_id=workspace.id,
                feedback_ref=f"FB-L{index}",
                rating=1,
                sentiment="Negative",
                body=_lonely_comment(index),
                submitted_at=now - dt.timedelta(seconds=_STALL_ROWS - index),
            )
            for index in range(_STALL_ROWS)
        )
        await session.commit()

    # The first request an app serves also builds its routes, on the loop; that
    # is the framework's cost, not the pass's, so it is paid before the clock
    # starts — by the same route, asked about an agent with no feedback.
    warm = await admin_client.post(f"{FEEDBACK}/analyze", json={"agent_id": "no-such-agent"})
    assert warm.status_code == 200 and warm.json()["analysed"] == 0, warm.text

    # What the grouping costs on this machine, run where it used to run: the
    # stall the loop would suffer if the pass still did it there. Timed on both
    # sides of the request and the quicker kept, so a machine that was busy for
    # one of them does not excuse a stall.
    from fulcrum_ops_api.services.feedback import _cluster_comments

    comments = [_lonely_comment(index) for index in range(_STALL_ROWS)]

    def grouping_seconds() -> float:
        started = time.perf_counter()
        _cluster_comments(comments, [], 0.34)
        return time.perf_counter() - started

    before = grouping_seconds()
    stop = asyncio.Event()
    watcher = asyncio.create_task(_longest_stall(stop))
    await asyncio.sleep(0.02)
    analysed = await admin_client.post(f"{FEEDBACK}/analyze", json={})
    stop.set()
    stall = await watcher
    on_the_loop = min(before, grouping_seconds())

    assert analysed.status_code == 200, analysed.text
    assert analysed.json()["analysed"] == _STALL_ROWS
    assert analysed.json()["clustered"] == 0, "the input is the worst case only if none join"
    assert stall < on_the_loop / 3, (
        f"the loop stalled {stall:.2f}s during a pass whose grouping takes {on_the_loop:.2f}s"
    )


def _grouping_by_definition(comments, seeds, similarity) -> list[list[int]]:
    """Greedy leader clustering as it is defined: every leader tried, in order."""
    from fulcrum_ops_api.services.feedback import _jaccard, _tokenise

    leaders: list[tuple[set[str], list[int]]] = []
    for _theme, comment, _existing in seeds:
        tokens = _tokenise(comment)
        if tokens:
            leaders.append((tokens, []))
    for position, comment in enumerate(comments):
        tokens = _tokenise(comment)
        if not tokens:
            continue
        best, best_score = None, 0.0
        for leader in leaders:
            score = _jaccard(tokens, leader[0])
            if score > best_score:
                best, best_score = leader, score
        if best is not None and best_score >= similarity:
            best[1].append(position)
        else:
            leaders.append((tokens, [position]))
    return [members for _tokens, members in leaders]


def test_the_word_index_groups_exactly_as_comparing_every_pair_did():
    """The shortcut that made the pass cheap must not change a single theme."""
    from fulcrum_ops_api.services.feedback import _cluster_comments

    vocabulary = [
        "invoice", "total", "extraction", "failed", "scanned", "login", "timeout",
        "mobile", "refund", "duplicate", "charge", "summary", "missing", "table",
        "export", "slow", "crash", "upload", "currency", "rounding", "supplier",
    ]
    # Two to six words each, strided through the vocabulary so that comments
    # overlap a lot, a little and not at all — and tie, which is where a
    # shortcut that visits leaders in another order would pick another theme.
    comments: list[str | None] = [
        " ".join(
            vocabulary[(index * 7 + step * (3 + index % 4)) % len(vocabulary)]
            for step in range(2 + index % 5)
        )
        for index in range(400)
    ]
    comments[17] = None
    comments[101] = "the and for"  # nothing left once the stopwords go
    seeds = [
        ("Invoice Total Failed", "invoice total extraction failed", 12),
        ("Login Timeout Mobile", "login timeout on mobile", 3),
        ("No Comment Behind It", None, 2),
    ]

    for similarity in (0.2, 0.34, 0.5):
        clusters, blank = _cluster_comments(comments, seeds, similarity)
        assert blank == 2
        assert [cluster.members for cluster in clusters] == _grouping_by_definition(
            comments, seeds, similarity
        )
    assert [cluster.theme for cluster in clusters[:2]] == [
        "Invoice Total Failed",
        "Login Timeout Mobile",
    ]


