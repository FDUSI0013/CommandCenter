"""Test Connection always answers, and only ever calls somewhere it should.

Two defects from the September audit, both in the reachability probe behind
``POST /connectors/{id}/test``:

* an endpoint the HTTP library cannot parse (``https://erp.internal:port/``, or
  the form's own ``https://…`` placeholder pasted back in) registered cleanly
  and then answered 500 to every test, with no audit row;
* the probe went wherever the row pointed and followed redirects on its own, so
  from inside the container network it was a status-code-and-latency oracle for
  the unpublished services beside the control plane and for the instance
  metadata address.

The probe is exercised over a real socket against a small upstream started on
the loopback interface. Nothing here leaves the machine.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.models.governance import AuditEvent
from fulcrum_ops_api.models.identity import Role
from fulcrum_ops_api.services import connectors as connectors_service

CONNECTORS = "/api/v1/connectors"

#: Each of these starts with https:// or http:// and cannot be parsed.
MALFORMED = [
    pytest.param("https://erp.internal:port/", id="port-is-a-word"),
    pytest.param("http://[::1", id="unclosed-bracket"),
    pytest.param("https://…", id="the-forms-own-placeholder"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Upstream:
    """A connector endpoint on the loopback interface that remembers its callers."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.paths: list[str] = []
        self.hosts: list[str] = []


@pytest.fixture
async def upstream(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Upstream]:
    here = Upstream(_free_port())
    app = FastAPI()

    @app.middleware("http")
    async def remember(request: Request, call_next):
        here.paths.append(request.url.path)
        here.hosts.append(request.headers.get("host", ""))
        return await call_next(request)

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/guarded")
    async def guarded() -> JSONResponse:
        return JSONResponse({"detail": "credentials required"}, status_code=401)

    @app.get("/moved")
    async def moved() -> RedirectResponse:
        return RedirectResponse("/ok", status_code=308)

    @app.get("/bounce/{n}")
    async def bounce(n: int) -> RedirectResponse:
        """A redirect chain with no end."""
        return RedirectResponse(f"/bounce/{n + 1}", status_code=302)

    @app.get("/hop/{n}")
    async def hop(n: int) -> RedirectResponse:
        """The same chain, slowly: no one hop times out, the chain as a whole does."""
        await asyncio.sleep(0.3)
        return RedirectResponse(f"/hop/{n + 1}", status_code=302)

    @app.get("/inward")
    async def inward() -> RedirectResponse:
        """A public endpoint that redirects into the private network.

        Everything a test can start is on loopback, so "public first hop, private
        second hop" cannot be built out of addresses. It is built out of time
        instead: the first hop is let through by the deployment setting, and the
        setting is back to its default by the time the Location is looked at. A
        probe that vets only the URL on the row never notices; one that vets
        every hop stops here.
        """
        monkeypatch.setattr(settings, "connector_probe_allow_private", False)
        return RedirectResponse(f"{here.base_url}/secret", status_code=302)

    @app.get("/secret")
    async def secret() -> dict[str, str]:
        return {"role": "instance-credentials"}

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=here.port, log_level="error")
    )
    serving = asyncio.create_task(server.serve())
    for _ in range(500):  # the server exposes a flag, not an event; ten seconds at most
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield here
    finally:
        server.should_exit = True
        await serving


@pytest.fixture
def private_probes_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment whose governed connectors live on the control plane's network."""
    monkeypatch.setattr(settings, "connector_probe_allow_private", True)


async def _tested_events(db, connector_id: str) -> list[AuditEvent]:
    return await db.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "connector.tested", AuditEvent.entity_id == connector_id
        )
    )


# ---------------------------------------------------------------------------
# a malformed endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", MALFORMED)
async def test_a_malformed_endpoint_already_on_file_is_an_answer_not_a_500(
    admin_client, factory, workspace, db, endpoint: str
) -> None:
    # Straight into the table, as rows registered before the schema parsed URLs.
    connector = await factory.connector(workspace, name="ERP", endpoint_url=endpoint)

    response = await admin_client.post(f"{CONNECTORS}/{connector.id}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "Unreachable"
    assert "not a valid URL" in body["message"]
    assert body["http_status"] is None and body["latency_ms"] is None

    # The test was still an audited act.
    events = await _tested_events(db, connector.id)
    assert len(events) == 1
    assert events[0].event_metadata["ok"] is False


@pytest.mark.parametrize(
    "endpoint",
    [
        *MALFORMED,
        pytest.param("https://", id="no-host"),
        pytest.param("https:///mcp", id="path-only"),
        pytest.param("https://erp.internal:99999/", id="port-out-of-range"),
    ],
)
async def test_an_endpoint_that_cannot_be_parsed_is_refused_at_registration(
    admin_client, factory, workspace, endpoint: str
) -> None:
    created = await admin_client.post(
        CONNECTORS, json={"name": "ERP", "type": "API", "endpoint_url": endpoint}
    )
    assert created.status_code == 422, created.text

    existing = await factory.connector(workspace, name="CRM")
    edited = await admin_client.patch(
        f"{CONNECTORS}/{existing.id}", json={"endpoint_url": endpoint}
    )
    assert edited.status_code == 422, edited.text


async def test_an_ordinary_endpoint_still_registers(admin_client) -> None:
    for index, endpoint in enumerate(
        ["https://erp.example.com/mcp", "http://erp.internal:8443/v1?tenant=a", "https://[2001:db8::1]/"]
    ):
        created = await admin_client.post(
            CONNECTORS, json={"name": f"ERP {index}", "type": "API", "endpoint_url": endpoint}
        )
        assert created.status_code == 201, created.text
        assert created.json()["endpoint_url"] == endpoint


async def test_the_whole_probe_has_one_deadline(
    admin_client,
    factory,
    workspace,
    upstream: Upstream,
    private_probes_allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No single phase is slow enough to time out; the chain as a whole is.
    monkeypatch.setattr(connectors_service, "PROBE_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(connectors_service, "PROBE_DEADLINE_SECONDS", 0.5)
    connector = await factory.connector(
        workspace, name="Slow chain", endpoint_url=f"{upstream.base_url}/hop/0"
    )

    response = await admin_client.post(f"{CONNECTORS}/{connector.id}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False and body["status"] == "Unreachable"
    assert "No answer within" in body["message"]
    # 0.3 s a hop against a 0.5 s deadline: the second hop is where it ends.
    assert upstream.paths == ["/hop/0", "/hop/1"]


# ---------------------------------------------------------------------------
# where the probe may go
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700:4700::1111", True),
        ("::ffff:93.184.216.34", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("10.0.0.5", False),  # the container network
        ("172.18.0.4", False),
        ("192.168.1.10", False),
        ("169.254.169.254", False),  # instance metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),  # noqa: S104 - an address under test, not a bind
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("fe80::1", False),
        ("fd12:3456::1", False),
        ("::ffff:10.0.0.5", False),  # 10.0.0.5 by another spelling
        ("::ffff:169.254.169.254", False),
        ("2002:a00:5::", False),  # 6to4 carrying 10.0.0.5
        ("64:ff9b::a00:5", False),  # NAT64 carrying 10.0.0.5
    ],
)
def test_only_a_public_address_is_public(address: str, public: bool) -> None:
    assert connectors_service.is_public_address(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5:8123/?query=SELECT%201",
        "http://[::ffff:127.0.0.1]:8080/",
        "https://[fd12:3456::1]/",
        "http://localhost:8080/",
    ],
)
async def test_a_private_address_is_refused_before_anything_is_opened(endpoint: str) -> None:
    with pytest.raises(connectors_service.ProbeStopped) as refused:
        await connectors_service.probe_addresses(httpx.URL(endpoint))
    assert refused.value.status == "Warning"
    assert "private or internal" in str(refused.value)


async def test_a_private_endpoint_is_reported_as_not_probed_and_never_called(
    as_role, factory, workspace, db, upstream: Upstream
) -> None:
    by_number = await factory.connector(
        workspace, name="Engine", endpoint_url=f"{upstream.base_url}/ok"
    )
    by_name = await factory.connector(
        workspace, name="Engine by name", endpoint_url=f"http://localhost:{upstream.port}/ok"
    )

    # Testing needs no more than a member, which is what made this reachable.
    async with as_role(Role.MEMBER) as member:
        for connector in (by_number, by_name):
            response = await member.post(f"{CONNECTORS}/{connector.id}/test")

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["ok"] is False
            # Not "Unreachable": nothing was measured, so nothing is claimed.
            assert body["status"] == "Warning"
            assert "private or internal" in body["message"]
            assert body["http_status"] is None and body["latency_ms"] is None

            events = await _tested_events(db, connector.id)
            assert [event.event_metadata["status"] for event in events] == ["Warning"]

    assert upstream.paths == [], "the probe called an address it should have refused"


async def test_registering_a_private_endpoint_is_still_allowed(admin_client) -> None:
    # An internal MCP server is a legitimate thing to govern; only the call is withheld.
    created = await admin_client.post(
        CONNECTORS,
        json={"name": "Internal MCP", "type": "API", "endpoint_url": "http://10.0.0.5:9000/mcp"},
    )
    assert created.status_code == 201, created.text


async def test_a_deployment_may_allow_private_probes(
    admin_client, factory, workspace, upstream: Upstream, private_probes_allowed: None
) -> None:
    healthy = await factory.connector(workspace, name="CRM", endpoint_url=f"{upstream.base_url}/ok")
    guarded = await factory.connector(
        workspace, name="ERP", endpoint_url=f"{upstream.base_url}/guarded"
    )

    body = (await admin_client.post(f"{CONNECTORS}/{healthy.id}/test")).json()
    assert (body["ok"], body["status"], body["http_status"]) == (True, "Healthy", 200)
    assert isinstance(body["latency_ms"], int)

    # A 401 proves the endpoint is up and guarding itself.
    body = (await admin_client.post(f"{CONNECTORS}/{guarded.id}/test")).json()
    assert (body["ok"], body["status"], body["http_status"]) == (True, "Warning", 401)
    assert upstream.paths == ["/ok", "/guarded"]


async def test_the_probe_connects_to_the_address_it_vetted_under_the_name_it_was_given(
    admin_client, factory, workspace, upstream: Upstream, private_probes_allowed: None
) -> None:
    connector = await factory.connector(
        workspace, name="CRM", endpoint_url=f"http://localhost:{upstream.port}/ok"
    )

    body = (await admin_client.post(f"{CONNECTORS}/{connector.id}/test")).json()

    # "localhost" is ::1 as well as 127.0.0.1 and the upstream listens on one of
    # them: the probe moves on to the next vetted address rather than giving up...
    assert (body["ok"], body["status"]) == (True, "Healthy"), body
    # ...and the endpoint is still addressed by name, for virtual hosts and TLS.
    assert upstream.hosts == [f"localhost:{upstream.port}"]


async def test_a_redirect_is_followed_by_hand_and_only_so_far(
    admin_client, factory, workspace, upstream: Upstream, private_probes_allowed: None
) -> None:
    moved = await factory.connector(
        workspace, name="Moved", endpoint_url=f"{upstream.base_url}/moved"
    )
    looping = await factory.connector(
        workspace, name="Looping", endpoint_url=f"{upstream.base_url}/bounce/0"
    )

    body = (await admin_client.post(f"{CONNECTORS}/{moved.id}/test")).json()
    assert (body["ok"], body["status"], body["http_status"]) == (True, "Healthy", 200)
    assert upstream.paths == ["/moved", "/ok"]

    upstream.paths.clear()
    body = (await admin_client.post(f"{CONNECTORS}/{looping.id}/test")).json()
    assert (body["ok"], body["status"]) == (False, "Unreachable")
    assert "Redirected more than 3 times" in body["message"]
    assert upstream.paths == ["/bounce/0", "/bounce/1", "/bounce/2", "/bounce/3"]


async def test_a_redirect_into_the_private_network_is_not_followed(
    admin_client, factory, workspace, db, upstream: Upstream, private_probes_allowed: None
) -> None:
    connector = await factory.connector(
        workspace, name="Helpful redirector", endpoint_url=f"{upstream.base_url}/inward"
    )

    response = await admin_client.post(f"{CONNECTORS}/{connector.id}/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["ok"], body["status"]) == (False, "Warning")
    assert "redirects to" in body["message"] and "private or internal" in body["message"]
    assert body["http_status"] is None
    assert upstream.paths == ["/inward"], "the probe followed a redirect into the private network"
    assert len(await _tested_events(db, connector.id)) == 1
