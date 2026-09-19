"""Regressions for the guardrail defects the September audit found.

Each test drives the real request path -- the app, the database and the engine
double -- and fails without the fix it is named for.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event, select

from conftest import error_code
from engine_double import EngineFailure
from fulcrum_ops_api.models.quality import (
    GuardrailAction,
    GuardrailConfig,
    GuardrailEvent,
)


def iso(offset_seconds: float = 0.0) -> str:
    moment = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC) + dt.timedelta(
        seconds=offset_seconds
    )
    return moment.isoformat().replace("+00:00", "Z")


def trace(**overrides) -> dict:
    item = {
        "name": "answer customer question",
        "start_time": iso(),
        "end_time": iso(1.5),
        "input": {"question": "Where is my order?"},
        "output": {"answer": "It ships tomorrow."},
    }
    item.update(overrides)
    return item


def pii_row(*, start: int, end: int, text: str, label: str = "EMAIL_ADDRESS") -> dict:
    """A PII verdict shaped exactly as the deployed scanner answers."""
    return {
        "type": "PII",
        "validation_passed": False,
        "validation_details": {
            "detected_entities": {label: [{"start": start, "end": end, "score": 1.0, "text": text}]}
        },
    }


# ===========================================================================
# The ingest hot path
# ===========================================================================


async def test_identical_content_is_scanned_once_but_judged_per_item(
    ingest_client, db, factory, workspace, engine
):
    """Two items carrying the same text cost one scanner call, not two."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    guardrail = await factory.guardrail(workspace, name="PII shield")
    engine.checker_verdicts = [{"name": "PII", "triggered": True, "score": 0.97}]

    same = {"question": "my email is dana@example.com"}
    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(input=same), trace(input=same)]},
    )

    assert posted.status_code == 200, posted.text
    assert [row["outcome"] for row in posted.json()["results"]] == ["blocked", "blocked"]
    assert len(engine.checker_requests) == 1, "the repeat was not sent to the scanner again"

    events = await db.scalars(
        select(GuardrailEvent).where(GuardrailEvent.guardrail_id == guardrail.id)
    )
    assert len(events) == 2, "each item still leaves its own evidence"
    stored = await db.get(GuardrailConfig, guardrail.id)
    assert (stored.triggers_30d, stored.blocked_30d) == (2, 2)


async def test_guardrail_counters_are_written_after_the_engine_hand_off(
    ingest_client, db, db_engine, factory, workspace, engine
):
    """The counter UPDATE must not hold its row lock across the engine writes.

    The hook runs before the batch goes to the engine. An UPDATE issued there
    keeps the guardrail's row locked until the request commits -- across every
    engine write -- and a Global guardrail's row is shared by the whole
    workspace, so concurrent batches queued behind one another on it.
    """
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    guardrail = await factory.guardrail(
        workspace, name="PII watch", action=GuardrailAction.WARN.value
    )
    engine.checker_verdicts = [{"name": "PII", "triggered": True, "score": 0.9}]

    engine_writes_before_update: list[int] = []

    def watch(_conn, _cursor, statement, *_rest) -> None:
        if statement.lstrip().upper().startswith("UPDATE GUARDRAIL_CONFIGS"):
            engine_writes_before_update.append(len(engine.calls_to("/traces/batch")))

    event.listen(db_engine.sync_engine, "before_cursor_execute", watch)
    try:
        posted = await ingest_client.post(
            "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", watch)

    assert posted.status_code == 200, posted.text
    assert posted.json()["accepted"] == 1
    assert engine_writes_before_update, "the counters were written"
    assert min(engine_writes_before_update) >= 1, (
        "the guardrail row was locked before the batch reached the engine"
    )

    stored = await db.get(GuardrailConfig, guardrail.id)
    assert stored.triggers_30d == 1
    assert stored.last_triggered_at is not None


async def test_a_firing_guardrail_is_still_editable(
    ingest_client, admin_client, factory, workspace, engine
):
    """Counting a trigger is bookkeeping: it must not move the concurrency token."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    guardrail = await factory.guardrail(
        workspace, name="PII watch", action=GuardrailAction.WARN.value
    )
    before = (await admin_client.get(f"/api/v1/guardrails/{guardrail.id}")).json()
    engine.checker_verdicts = [{"name": "PII", "triggered": True, "score": 0.9}]

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert posted.status_code == 200, posted.text

    patched = await admin_client.patch(
        f"/api/v1/guardrails/{guardrail.id}",
        json={"threshold": 0.7, "expected_updated_at": before["updated_at"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["triggers_30d"] == 1


VALIDATIONS = "guardrails/validations"


def slow_scanner(engine, seconds: float, *, only: str | None = None):
    """A scanner that takes ``seconds`` to answer -- for ``only`` that type, or for all.

    Set as ``engine.before_dispatch``. The transport the double sits behind has
    no socket and so no read timeout of its own; the caller's deadline is what
    gives up on it, exactly as it would on a hung scanner.
    """
    import asyncio

    async def dispatch(_method: str, path: str) -> None:
        if VALIDATIONS not in path:
            return
        asked = {v.get("type") for v in (engine.calls[-1].body or {}).get("validations") or []}
        if only is None or only in asked:
            await asyncio.sleep(seconds)

    return dispatch


async def test_a_hung_scanner_is_paid_for_once_not_on_every_batch(
    ingest_client, admin_client, factory, workspace, engine, monkeypatch
):
    """With two guardrails in scope the failure has to be recorded inside the budget.

    The refused request was asked again one validation at a time on the full
    per-call timeout, which together overran the batch budget: the budget
    cancelled the retries before any of them could suspend anything, so nothing
    was ever learned and EVERY batch held its request for the whole budget.
    """
    from fulcrum_ops_api.core.config import settings
    from fulcrum_ops_api.models.quality import GuardrailType

    monkeypatch.setattr(settings, "guardrail_check_timeout_seconds", 1.0)
    monkeypatch.setattr(settings, "guardrail_batch_budget_seconds", 2.0)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII shield")
    await factory.guardrail(
        workspace, name="Injection shield", guardrail_type=GuardrailType.PROMPT_INJECTION.value
    )
    engine.before_dispatch = slow_scanner(engine, 3.0)

    first = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert first.status_code == 200, first.text
    assert first.json()["results"][0]["outcome"] == "accepted", "a hung scanner fails open"
    asked = len(engine.calls_to(VALIDATIONS))

    second = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert second.status_code == 200, second.text
    assert len(engine.calls_to(VALIDATIONS)) == asked, "the next batch waited on it all over again"

    summary = (await admin_client.get("/api/v1/guardrails/summary")).json()
    assert summary["suspended_validations"] == ["PII", "PROMPT_INJECTION"]
    assert summary["not_enforced"] == 2, "and the screen says nothing is being enforced"


async def test_one_slow_validation_does_not_cost_the_others_their_verdict(
    ingest_client, admin_client, factory, workspace, engine, monkeypatch
):
    """The healthy guardrail still answers, inside the same budget, and still blocks."""
    from fulcrum_ops_api.core.config import settings
    from fulcrum_ops_api.models.quality import GuardrailType

    monkeypatch.setattr(settings, "guardrail_check_timeout_seconds", 1.0)
    monkeypatch.setattr(settings, "guardrail_batch_budget_seconds", 2.0)
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII shield")
    await factory.guardrail(
        workspace,
        name="Regulated advice",
        guardrail_type=GuardrailType.TOPIC.value,
        config={"topics": ["advice"]},
    )
    engine.before_dispatch = slow_scanner(engine, 3.0, only="TOPIC")
    engine.checker_handler = lambda _text, _validation: {"validation_passed": False}

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["results"][0]["outcome"] == "blocked", "PII answered on its own"
    assert engine.trace_count(agent.engine_project_name) == 0
    summary = (await admin_client.get("/api/v1/guardrails/summary")).json()
    assert summary["suspended_validations"] == ["TOPIC"], "only the slow one is set aside"


async def test_running_out_of_budget_is_not_held_against_the_validation(
    ingest_client, admin_client, factory, workspace, engine, monkeypatch
):
    """A check that queued behind its batch and ran out of budget proves nothing.

    This guards the fix above rather than an audited defect: clipping every
    call to what is left of the budget, and then suspending PII because the
    ninth item's sliver "timed out", would let one oversized batch switch a
    healthy guardrail off for every workspace the worker serves.
    """
    from fulcrum_ops_api.core.config import settings
    from fulcrum_ops_api.services.guardrails import CHECK_CONCURRENCY

    monkeypatch.setattr(settings, "guardrail_check_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "guardrail_batch_budget_seconds", 2.0)
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII shield")
    engine.checker_verdicts = [{"name": "PII", "triggered": True, "score": 0.97}]
    # Slow but answering: the first wave is answered inside its allowance, and
    # the one item left queueing behind it is not.
    engine.before_dispatch = slow_scanner(engine, 1.2)

    traces = [
        trace(input={"question": f"where is order {number}?"})
        for number in range(CHECK_CONCURRENCY + 1)
    ]
    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": traces}
    )
    assert posted.status_code == 200, posted.text
    assert {row["outcome"] for row in posted.json()["results"]} == {"accepted"}, (
        "out of budget: stored unevaluated"
    )
    summary = (await admin_client.get("/api/v1/guardrails/summary")).json()
    assert summary["suspended_validations"] == [], "PII was not the problem"
    assert summary["not_enforced"] == 0

    # Given the time, the same guardrail is asked and enforces.
    monkeypatch.setattr(settings, "guardrail_batch_budget_seconds", 12.0)
    engine.before_dispatch = None
    again = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert again.json()["results"][0]["outcome"] == "blocked"


# ===========================================================================
# Two guardrails of one type
# ===========================================================================


def topic_scanner(text: str, validation: dict) -> dict:
    """Answer a TOPIC validation from its own topic list, as the scanner does."""
    topics = (validation.get("config") or {}).get("topics") or []
    found = [topic for topic in topics if topic.lower() in text.lower()]
    return {
        "validation_passed": not found,
        "validation_details": {"scores": {topic: 0.97 for topic in found}},
    }


async def test_two_guardrails_of_one_type_each_get_their_own_verdict(
    ingest_client, db, factory, workspace, engine
):
    """The scanner names a row only by its type, so position tells them apart.

    Pairing by name gave both Topic guardrails the FIRST Topic row: the second
    one never fired on its own verdict.
    """
    from fulcrum_ops_api.models.quality import GuardrailType

    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    politics = await factory.guardrail(
        workspace,
        name="A - politics",
        guardrail_type=GuardrailType.TOPIC.value,
        config={"topics": ["politics"]},
    )
    competitors = await factory.guardrail(
        workspace,
        name="B - competitors",
        guardrail_type=GuardrailType.TOPIC.value,
        config={"topics": ["competitors"]},
    )
    engine.checker_handler = topic_scanner

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(input={"question": "how do you compare to your competitors?"})],
        },
    )

    assert posted.status_code == 200, posted.text
    row = posted.json()["results"][0]
    assert row["outcome"] == "blocked", "the second Topic guardrail fired on its own verdict"
    assert row["guardrail_id"] == competitors.id
    assert engine.trace_count(agent.engine_project_name) == 0

    fired = await db.scalars(select(GuardrailEvent.guardrail_id))
    assert fired == [competitors.id], "and the first one did not fire on somebody else's"
    assert politics.id not in fired


async def test_the_first_verdict_is_not_handed_to_the_second_guardrail(
    ingest_client, db, factory, workspace, engine
):
    """The mirror image: only the FIRST of two same-type guardrails matches."""
    from fulcrum_ops_api.models.quality import GuardrailType

    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    competitors = await factory.guardrail(
        workspace,
        name="A - competitors",
        guardrail_type=GuardrailType.TOPIC.value,
        action=GuardrailAction.WARN.value,
        config={"topics": ["competitors"]},
    )
    await factory.guardrail(
        workspace,
        name="B - politics",
        guardrail_type=GuardrailType.TOPIC.value,
        config={"topics": ["politics"]},
    )
    engine.checker_handler = topic_scanner

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(input={"question": "how do you compare to your competitors?"})],
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["results"][0]["outcome"] == "accepted", (
        "the Block guardrail about politics must not fire on the competitors verdict"
    )
    assert engine.trace_count(agent.engine_project_name) == 1
    fired = await db.scalars(select(GuardrailEvent.guardrail_id))
    assert fired == [competitors.id]


# ===========================================================================
# What an event row keeps of the content
# ===========================================================================

EMAIL = "dana@acme.test"


def pii_scanner(text: str, validation: dict) -> dict:
    """Locate the e-mail address the way the deployed scanner reports it."""
    if validation.get("type") != "PII":
        return {"validation_passed": False}  # the Topic check beside it fires too
    at = text.find(EMAIL)
    if at < 0:
        return {"validation_passed": True}
    return pii_row(start=at, end=at + len(EMAIL), text=EMAIL)


async def test_a_mask_guardrail_does_not_keep_what_it_masked(
    ingest_client, admin_client, viewer_client, db, factory, workspace, engine
):
    """The sample on the event row is masked too, in the feed and in the export."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    guardrail = await factory.guardrail(
        workspace, name="PII mask", action=GuardrailAction.MASK.value
    )
    engine.checker_handler = pii_scanner

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(input={"question": f"please reach me at {EMAIL} today"})],
        },
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["results"][0]["masked"] is True

    stored = await db.scalar(
        select(GuardrailEvent).where(GuardrailEvent.guardrail_id == guardrail.id)
    )
    assert stored is not None
    assert EMAIL not in (stored.sample or ""), "the guardrail kept what it was told to protect"
    assert "please reach me at" in stored.sample, "the rest still identifies the case"

    feed = await viewer_client.get("/api/v1/guardrails/events")
    assert feed.status_code == 200, feed.text
    assert EMAIL not in feed.text
    exported = await admin_client.get("/api/v1/guardrails/events/export")
    assert exported.status_code == 200, exported.text
    assert EMAIL not in exported.text


async def test_a_verdict_that_cannot_say_where_the_data_is_keeps_no_sample(
    ingest_client, db, factory, workspace, engine
):
    """A PII trigger with no located span: nothing can be masked, so nothing is kept."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    guardrail = await factory.guardrail(workspace, name="PII shield")
    engine.checker_verdicts = [{"name": "PII", "triggered": True, "score": 0.97}]

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={"agent": "Support Bot", "traces": [trace(input={"question": f"I am {EMAIL}"})]},
    )
    assert posted.status_code == 200, posted.text

    stored = await db.scalar(
        select(GuardrailEvent).where(GuardrailEvent.guardrail_id == guardrail.id)
    )
    assert stored.sample == "[content withheld]"


async def test_one_guardrails_sample_does_not_carry_what_another_located(
    ingest_client, db, factory, workspace, engine
):
    """A Topic warning firing beside a PII mask must not keep the address either."""
    from fulcrum_ops_api.models.quality import GuardrailType

    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII mask", action=GuardrailAction.MASK.value)
    topic = await factory.guardrail(
        workspace,
        name="Regulated advice",
        guardrail_type=GuardrailType.TOPIC.value,
        action=GuardrailAction.WARN.value,
        config={"topics": ["advice"]},
    )
    engine.checker_handler = pii_scanner

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(input={"question": f"send the advice to {EMAIL}"})],
        },
    )
    assert posted.status_code == 200, posted.text

    stored = await db.scalar(
        select(GuardrailEvent).where(GuardrailEvent.guardrail_id == topic.id)
    )
    assert stored is not None, "the Topic guardrail fired"
    assert EMAIL not in stored.sample
    assert "send the advice to" in stored.sample


# ===========================================================================
# The engine-side rule is a mirror, and a mirror must not veto the write
# ===========================================================================

RULE_STORE = "automations/evaluators"


async def _mirrored_guardrail(admin_client, factory, workspace, engine, **body) -> dict:
    """A guardrail created through the API with an agent in scope, so it is mirrored."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    created = await admin_client.post(
        "/api/v1/guardrails",
        json={"name": "PII shield", "guardrail_type": "PII", "action": "Block", **body},
    )
    assert created.status_code == 201, created.text
    assert created.json()["engine_guardrail_id"], "the rule was mirrored into the store"
    return created.json()


async def test_disable_works_while_the_rule_store_is_down(
    admin_client, ingest_client, db, factory, workspace, engine
):
    """Disable is the kill switch. An engine outage must not take it away."""
    guardrail = await _mirrored_guardrail(admin_client, factory, workspace, engine)
    engine.fail_path(RULE_STORE, 503)

    disabled = await admin_client.post(f"/api/v1/guardrails/{guardrail['id']}/disable")

    assert disabled.status_code == 200, disabled.text
    stored = await db.get(GuardrailConfig, guardrail["id"])
    assert stored.status == "Disabled"

    # ...and it really has stopped blocking.
    engine.checker_verdicts = [{"name": "PII", "triggered": True, "score": 0.97}]
    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["results"][0]["outcome"] == "accepted"


async def test_disable_works_after_the_store_lost_the_rule(
    admin_client, db, factory, workspace, engine
):
    """A 404 from the store used to be permanent: the rule is registered again."""
    guardrail = await _mirrored_guardrail(admin_client, factory, workspace, engine)
    engine.guardrail_rules.clear()  # the store was reset

    disabled = await admin_client.post(f"/api/v1/guardrails/{guardrail['id']}/disable")

    assert disabled.status_code == 200, disabled.text
    stored = await db.get(GuardrailConfig, guardrail["id"])
    assert stored.status == "Disabled"
    assert stored.engine_guardrail_id in engine.guardrail_rules, "re-registered, not orphaned"
    assert stored.engine_guardrail_id != guardrail["engine_guardrail_id"]
    assert engine.guardrail_rules[stored.engine_guardrail_id]["enabled"] is False


async def test_tune_edit_and_create_survive_a_rule_store_outage(
    admin_client, db, factory, workspace, engine
):
    guardrail = await _mirrored_guardrail(admin_client, factory, workspace, engine)
    engine.fail_path(RULE_STORE, 503)

    tuned = await admin_client.patch(
        f"/api/v1/guardrails/{guardrail['id']}/threshold", json={"threshold": 0.9}
    )
    assert tuned.status_code == 200, tuned.text
    assert tuned.json()["threshold"] == 0.9

    edited = await admin_client.patch(
        f"/api/v1/guardrails/{guardrail['id']}", json={"action": "Mask"}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["action"] == "Mask"

    created = await admin_client.post(
        "/api/v1/guardrails", json={"name": "Second shield", "guardrail_type": "PII"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["engine_guardrail_id"] is None, "saved; the mirror is owed"

    # The next edit, once the store is back, pays the debt.
    engine.recover()
    again = await admin_client.patch(
        f"/api/v1/guardrails/{created.json()['id']}", json={"threshold": 0.6}
    )
    assert again.status_code == 200, again.text
    assert again.json()["engine_guardrail_id"] in engine.guardrail_rules


async def test_a_scope_with_no_provisioned_agent_removes_the_rule(
    admin_client, db, factory, workspace, engine
):
    """The rule is deleted, not patched to an empty namespace list."""
    guardrail = await _mirrored_guardrail(admin_client, factory, workspace, engine)
    unprovisioned = await factory.agent(workspace, name="Draft Bot")

    moved = await admin_client.patch(
        f"/api/v1/guardrails/{guardrail['id']}",
        json={"scope": "Agent", "scope_ref": unprovisioned.id},
    )

    assert moved.status_code == 200, moved.text
    assert moved.json()["engine_guardrail_id"] is None
    assert engine.guardrail_rules == {}
    patches = [call for call in engine.calls_to(RULE_STORE) if call.method == "PATCH"]
    assert not [call for call in patches if not (call.body or {}).get("project_ids")]


async def test_switching_the_mirror_off_unloads_the_rules_already_mirrored(
    admin_client, db, factory, workspace, engine, monkeypatch
):
    """Nothing reads the mirrored scores, so a deployment may switch the mirror off.

    Doing so used to leave every rule already registered running on the metric
    runner, against every trace, for good: the early return never looked at it.
    """
    from fulcrum_ops_api.core.config import settings

    guardrail = await _mirrored_guardrail(admin_client, factory, workspace, engine)
    monkeypatch.setattr(settings, "engine_metric_library", "")

    tuned = await admin_client.patch(
        f"/api/v1/guardrails/{guardrail['id']}/threshold", json={"threshold": 0.8}
    )

    assert tuned.status_code == 200, tuned.text
    assert tuned.json()["engine_guardrail_id"] is None
    assert engine.guardrail_rules == {}, "the rule is no longer costing the runner anything"

    # And with the store down the write still lands; the rule is removed next time.
    monkeypatch.setattr(settings, "engine_metric_library", "enginelib")
    second = await admin_client.post(
        "/api/v1/guardrails", json={"name": "Second shield", "guardrail_type": "PII"}
    )
    assert second.json()["engine_guardrail_id"] in engine.guardrail_rules
    monkeypatch.setattr(settings, "engine_metric_library", "")
    engine.fail_path(RULE_STORE, 503)
    disabled = await admin_client.post(f"/api/v1/guardrails/{second.json()['id']}/disable")
    assert disabled.status_code == 200, disabled.text
    stored = await db.get(GuardrailConfig, second.json()["id"])
    assert stored.status == "Disabled"
    assert stored.engine_guardrail_id == second.json()["engine_guardrail_id"], "still owed"


# ===========================================================================
# "Active" is a setting; "enforced" is a fact
# ===========================================================================


@pytest.fixture(autouse=True)
def _no_remembered_suspensions():
    """Suspensions are per process; a test must not inherit another's."""
    from fulcrum_ops_api.services import guardrails as service

    service._SUSPENDED.clear()
    yield
    service._SUSPENDED.clear()


async def test_a_topic_guardrail_without_topics_is_not_sent_and_says_so(
    admin_client, ingest_client, db, factory, workspace, engine
):
    """One unfinished Topic guardrail must not switch the finished ones off.

    The scanner refuses a TOPIC validation with no topics, and refuses the whole
    request with it; the refusal then suspended TOPIC for everybody.
    """
    await factory.provisioned_agent(workspace, engine, name="Support Bot")

    def strict_topic_scanner(text: str, validation: dict) -> dict:
        if not (validation.get("config") or {}).get("topics"):
            raise EngineFailure(400, {"errors": ["topics: field required"]})
        return topic_scanner(text, validation)

    engine.checker_handler = strict_topic_scanner

    unfinished = await admin_client.post(
        "/api/v1/guardrails",
        json={"name": "A - unfinished", "guardrail_type": "Topic", "action": "Block"},
    )
    assert unfinished.status_code == 201, unfinished.text
    assert unfinished.json()["enforcement"] == "misconfigured"
    assert "topics" in unfinished.json()["not_enforced_reason"]

    finished = await admin_client.post(
        "/api/v1/guardrails",
        json={
            "name": "B - competitors",
            "guardrail_type": "Topic",
            "action": "Block",
            "config": {"topics": [" competitors ", "Competitors", ""]},
        },
    )
    assert finished.status_code == 201, finished.text
    assert finished.json()["config"]["topics"] == ["competitors"], "trimmed and de-duplicated"
    assert finished.json()["enforcement"] == "enforced"

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [trace(input={"question": "are you better than your competitors?"})],
        },
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["results"][0]["outcome"] == "blocked", "the finished one still enforces"
    for sent in engine.checker_requests:
        assert all((v.get("config") or {}).get("topics") for v in sent["validations"])

    summary = (await admin_client.get("/api/v1/guardrails/summary")).json()
    assert summary["suspended_validations"] == []
    assert (summary["active"], summary["not_enforced"]) == (2, 1)

    # Testing it explains the problem instead of bouncing off the scanner...
    tested = await admin_client.post(
        f"/api/v1/guardrails/{unfinished.json()['id']}/test", json={"input": "anything"}
    )
    assert tested.status_code == 412, tested.text
    assert "topics" in tested.text

    # ...and giving it topics is an ordinary edit.
    fixed = await admin_client.patch(
        f"/api/v1/guardrails/{unfinished.json()['id']}",
        json={"config": {"topics": ["politics"], "mode": "restrict"}},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["enforcement"] == "enforced"
    assert fixed.json()["not_enforced_reason"] is None


async def test_a_malformed_config_is_refused_with_the_key_named(admin_client):
    for config, key in (
        ({"topics": "politics"}, "config.topics"),
        ({"topics": ["politics"], "mode": "forbid"}, "config.mode"),
        ({"patterns": {"BAD": "("}}, "config.patterns"),
    ):
        refused = await admin_client.post(
            "/api/v1/guardrails",
            json={"name": f"Topic {key}", "guardrail_type": "Topic", "config": config},
        )
        assert refused.status_code == 422, refused.text
        assert error_code(refused) == "validation_failed"
        assert key in refused.text


async def test_a_suspended_validation_is_reported_not_hidden(
    admin_client, ingest_client, factory, workspace, engine
):
    """The scanner cannot run one validation: the screen has to say which."""
    from fulcrum_ops_api.models.quality import GuardrailType

    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII shield")
    injection = await factory.guardrail(
        workspace, name="Injection shield", guardrail_type=GuardrailType.PROMPT_INJECTION.value
    )

    def scanner(text: str, validation: dict) -> dict:
        if validation.get("type") == "PROMPT_INJECTION":
            raise EngineFailure(500, {"errors": ["model not installed"]})
        return {"validation_passed": False}

    engine.checker_handler = scanner

    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["results"][0]["outcome"] == "blocked", "PII is still enforced"

    listed = (await admin_client.get("/api/v1/guardrails")).json()["items"]
    by_name = {row["name"]: row for row in listed}
    assert by_name["PII shield"]["enforcement"] == "enforced"
    assert by_name["Injection shield"]["enforcement"] == "suspended"
    assert by_name["Injection shield"]["status"] == "Active", "the setting has not changed"
    assert by_name["Injection shield"]["not_enforced_reason"]

    summary = (await admin_client.get("/api/v1/guardrails/summary")).json()
    assert summary["suspended_validations"] == ["PROMPT_INJECTION"]
    assert summary["not_enforced"] == 1
    assert injection.id


async def test_a_refused_config_suspends_that_guardrail_not_the_validation(
    admin_client, ingest_client, factory, workspace, engine
):
    """A 4xx is about what was sent. One bad config must not silence its type."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="A - typo", config={"entities": ["NOT_AN_ENTITY"]})
    await factory.guardrail(workspace, name="B - PII shield")

    def scanner(text: str, validation: dict) -> dict:
        if "NOT_AN_ENTITY" in ((validation.get("config") or {}).get("entities") or []):
            raise EngineFailure(400, {"errors": ["unknown entity"]})
        return {"validation_passed": False}

    engine.checker_handler = scanner

    for _ in range(2):  # the second batch must not find PII suspended
        posted = await ingest_client.post(
            "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["results"][0]["outcome"] == "blocked"

    summary = (await admin_client.get("/api/v1/guardrails/summary")).json()
    assert summary["suspended_validations"] == [], "PII itself still runs"
    listed = (await admin_client.get("/api/v1/guardrails")).json()["items"]
    assert {row["name"]: row["enforcement"] for row in listed} == {
        "A - typo": "suspended",
        "B - PII shield": "enforced",
    }


async def test_shadow_puts_a_guardrail_into_tuning(
    as_role, ingest_client, db, factory, workspace, engine
):
    from fulcrum_ops_api.models.governance import AuditEvent
    from fulcrum_ops_api.models.identity import Role

    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    guardrail = await factory.guardrail(workspace, name="PII shield")

    async with as_role(Role.OPERATOR) as http:
        shadowed = await http.post(f"/api/v1/guardrails/{guardrail.id}/shadow")
        assert shadowed.status_code == 200, shadowed.text
        assert shadowed.json()["data"]["status"] == "Tuning"
        assert shadowed.json()["data"]["enforcement"] == "shadow"
        again = await http.post(f"/api/v1/guardrails/{guardrail.id}/shadow")
        assert again.status_code == 412, again.text

    actions = await db.scalars(
        select(AuditEvent.action).where(AuditEvent.entity_id == guardrail.id)
    )
    assert actions == ["guardrail.shadowed"], "not recorded as a disable"

    engine.checker_verdicts = [{"name": "PII", "triggered": True, "score": 0.97}]
    posted = await ingest_client.post(
        "/api/v1/ingest/traces", json={"agent": "Support Bot", "traces": [trace()]}
    )
    assert posted.json()["results"][0]["outcome"] == "accepted"
    assert engine.trace_count(agent.engine_project_name) == 1
    events = await db.scalars(
        select(GuardrailEvent.action_taken).where(GuardrailEvent.guardrail_id == guardrail.id)
    )
    assert events == ["Log"]


async def test_enabling_a_rule_nothing_can_enforce_says_so(admin_client, factory, workspace):
    from fulcrum_ops_api.models.quality import GuardrailStatus, GuardrailType

    guardrail = await factory.guardrail(
        workspace,
        name="Toxicity screen",
        guardrail_type=GuardrailType.TOXICITY.value,
        status=GuardrailStatus.DISABLED.value,
    )
    enabled = await admin_client.post(f"/api/v1/guardrails/{guardrail.id}/enable")
    assert enabled.status_code == 200, enabled.text
    assert "not enforced" in enabled.json()["message"]
    assert enabled.json()["data"]["enforcement"] == "unsupported"
