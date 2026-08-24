"""The request rate limit: configured since day one, enforced as of now.

Ingest and read traffic get separate per-minute windows per credential, and a
full window answers 429 with a Retry-After rather than queueing or dropping.
"""

from __future__ import annotations

from conftest import error_code
from fulcrum_ops_api.core.config import settings


async def test_a_full_read_window_answers_429_with_retry_after(
    admin_client, monkeypatch
):
    monkeypatch.setattr(settings, "rate_limit_read_per_minute", 3)

    for _ in range(3):
        ok = await admin_client.get("/api/v1/agents")
        assert ok.status_code == 200, ok.text

    refused = await admin_client.get("/api/v1/agents")
    assert refused.status_code == 429, refused.text
    assert error_code(refused) == "rate_limited"
    assert int(refused.headers["Retry-After"]) >= 1


async def test_ingest_and_read_windows_are_separate(
    ingest_client, factory, workspace, engine, monkeypatch
):
    """A busy reporter must not lock its own operator out, or vice versa."""
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    monkeypatch.setattr(settings, "rate_limit_read_per_minute", 2)

    for _ in range(2):
        listed = await ingest_client.get("/api/v1/runs")
        assert listed.status_code == 200, listed.text
    read_refused = await ingest_client.get("/api/v1/runs")
    assert read_refused.status_code == 429

    posted = await ingest_client.post(
        "/api/v1/ingest/traces",
        json={
            "agent": "Support Bot",
            "traces": [
                {
                    "name": "still flowing",
                    "start_time": "2026-08-24T12:00:00Z",
                    "end_time": "2026-08-24T12:00:01Z",
                }
            ],
        },
    )
    assert posted.status_code == 200, "the ingest window is its own bucket"


async def test_the_limiter_can_be_switched_off(admin_client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_read_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)

    for _ in range(5):
        ok = await admin_client.get("/api/v1/agents")
        assert ok.status_code == 200, ok.text
