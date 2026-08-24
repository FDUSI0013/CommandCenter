"""The inline scanner's real contract, learned the hard way in production.

The scanner implements exactly four validations and refuses whole batches on
the first unknown type, and it answers ``validation_passed`` plus a
``detected_entities`` map rather than a flat match list. These tests pin the
adapter to that contract.
"""

from __future__ import annotations

from conftest import error_code
from fulcrum_ops_api.models.quality import GuardrailType
from fulcrum_ops_api.schemas.ingest import RejectionCode


def real_scanner_row() -> dict:
    """A verdict shaped exactly as the deployed scanner answers."""
    return {
        "type": "PII",
        "validation_passed": False,
        "validation_config": {"threshold": 0.5, "language": "en"},
        "validation_details": {
            "detected_entities": {
                "EMAIL_ADDRESS": [
                    {"start": 12, "end": 27, "score": 1.0, "text": "dana@acme.test"}
                ]
            }
        },
    }


async def test_the_scanners_own_answer_shape_is_understood(
    ingest_client, factory, workspace, engine
):
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII shield")
    engine.checker_verdicts = [real_scanner_row()]

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    "name": "reply",
                    "start_time": "2026-08-24T12:00:00Z",
                    "end_time": "2026-08-24T12:00:01Z",
                    "input": {"question": "reach me at dana@acme.test"},
                }
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    row = posted.json()["results"][0]
    assert (row["outcome"], row["code"]) == ("blocked", RejectionCode.GUARDRAIL_BLOCKED.value)
    assert engine.trace_count(agent.engine_project_name) == 0


async def test_an_unsupported_validation_is_left_out_not_sent(
    ingest_client, factory, workspace, engine
):
    """One Toxicity guardrail must not poison the batch for the PII one."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    await factory.guardrail(workspace, name="PII shield")
    await factory.guardrail(
        workspace, name="Toxicity screen", guardrail_type=GuardrailType.TOXICITY.value
    )
    engine.checker_verdicts = [real_scanner_row()]

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    "name": "reply",
                    "start_time": "2026-08-24T12:00:00Z",
                    "end_time": "2026-08-24T12:00:01Z",
                    "input": {"question": "reach me at dana@acme.test"},
                }
            ],
        },
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["guardrails_evaluated"] is True
    row = posted.json()["results"][0]
    assert row["code"] == RejectionCode.GUARDRAIL_BLOCKED.value, "the PII shield still fires"
    sent = engine.checker_requests[-1]
    types = {v.get("type") for v in sent.get("validations", [])}
    assert types == {"PII"}, "only the supported validation was sent"


async def test_testing_an_unsupported_guardrail_refuses_rather_than_simulating(
    admin_client, factory, workspace, engine
):
    guardrail = await factory.guardrail(
        workspace, name="Toxicity screen", guardrail_type=GuardrailType.TOXICITY.value
    )
    tested = await admin_client.post(
        f"/api/v1/guardrails/{guardrail.id}/test", json={"input": "you are awful"}
    )
    assert tested.status_code == 412, tested.text
    assert error_code(tested) == "precondition_failed"
    assert "does not implement" in tested.text
