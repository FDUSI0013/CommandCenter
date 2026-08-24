"""The list envelope, proved once and then held to across the domains.

Twenty-three console screens are driven by one table component, and that only
works because every collection answers the same questions the same way: the
same query parameters, the same envelope, the same refusal when asked for a
column it cannot order by, and a CSV export that reflects whatever the operator
is currently looking at.

Rather than write that contract out nine times, the domains are described once
in :data:`DOMAINS` — how to build a row, what its label is called on the wire,
which key it sorts by and which dropdown narrows it — and every test below is
parametrised over the table. A new domain becomes one entry rather than a new
file, and a domain that quietly stops honouring ``sort`` fails here rather than
on somebody's screen.

The sample is deliberately mixed: registry rows (agents, connectors), governance
rows (policies, approvals, secrets), configuration rows and operational rows
(alerts). What they have in common is the envelope; nothing else about them is
alike, which is the point. Two collections that are *not* rows in our database —
runs, which live in the telemetry engine, and the audit trail, which no endpoint
creates directly — are covered at the end of the file, because the contract is
supposed to hold for them too.
"""

from __future__ import annotations

import csv
import dataclasses
import io
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from conftest import error_code
from fulcrum_ops_api.api.common import MAX_PAGE_SIZE
from fulcrum_ops_api.models.governance import ApprovalStatus, PolicyStatus, SecretStatus
from fulcrum_ops_api.models.operations import AlertStatus
from fulcrum_ops_api.models.quality import GuardrailStatus
from fulcrum_ops_api.models.registry import (
    AgentStatus,
    ConfigurationStatus,
    ConnectorStatus,
    KnowledgeSourceStatus,
)

# ---------------------------------------------------------------------------
# The domains under test
# ---------------------------------------------------------------------------

Builder = Callable[[Any, Any, str], Awaitable[Any]]


@dataclasses.dataclass(frozen=True)
class Domain:
    """One collection, described in the terms every list endpoint shares.

    ``build`` makes an ordinary row carrying ``label``; ``build_odd`` makes one
    that the dropdown in ``filter_param``/``filter_value`` — and only that one —
    selects.
    """

    name: str
    path: str
    label_field: str
    sort_key: str
    build: Builder
    build_odd: Builder
    filter_param: str
    filter_value: str

    @property
    def export_path(self) -> str:
        return f"{self.path}/export"

    def label_of(self, item: dict[str, Any]) -> str:
        return item[self.label_field]


def _named(method: str, field: str, **fields: Any) -> Builder:
    """A builder that calls ``factory.<method>`` with ``field=<label>``."""

    async def build(factory, workspace, label):
        return await getattr(factory, method)(workspace, **{field: label}, **fields)

    return build


DOMAINS: list[Domain] = [
    Domain(
        name="agents",
        path="/api/v1/agents",
        label_field="name",
        sort_key="name",
        build=_named("agent", "name", status=AgentStatus.ACTIVE.value),
        build_odd=_named("agent", "name", status=AgentStatus.INACTIVE.value),
        filter_param="status",
        filter_value=AgentStatus.INACTIVE.value,
    ),
    Domain(
        name="secrets",
        path="/api/v1/secrets",
        label_field="name",
        sort_key="name",
        build=_named("secret", "name", status=SecretStatus.ACTIVE.value),
        build_odd=_named("secret", "name", status=SecretStatus.DISABLED.value),
        filter_param="status",
        filter_value=SecretStatus.DISABLED.value,
    ),
    Domain(
        name="policies",
        path="/api/v1/policies",
        label_field="name",
        sort_key="name",
        build=_named("policy", "name", status=PolicyStatus.ACTIVE.value),
        build_odd=_named("policy", "name", status=PolicyStatus.INACTIVE.value),
        filter_param="status",
        filter_value=PolicyStatus.INACTIVE.value,
    ),
    Domain(
        name="guardrails",
        path="/api/v1/guardrails",
        label_field="name",
        sort_key="name",
        build=_named("guardrail", "name", status=GuardrailStatus.ACTIVE.value),
        build_odd=_named("guardrail", "name", status=GuardrailStatus.DISABLED.value),
        filter_param="status",
        filter_value=GuardrailStatus.DISABLED.value,
    ),
    Domain(
        name="knowledge",
        path="/api/v1/knowledge",
        label_field="name",
        sort_key="name",
        build=_named("knowledge_source", "name", status=KnowledgeSourceStatus.ACTIVE.value),
        build_odd=_named("knowledge_source", "name", status=KnowledgeSourceStatus.PAUSED.value),
        filter_param="status",
        filter_value=KnowledgeSourceStatus.PAUSED.value,
    ),
    Domain(
        name="connectors",
        path="/api/v1/connectors",
        label_field="name",
        sort_key="name",
        build=_named("connector", "name", status=ConnectorStatus.ACTIVE.value),
        build_odd=_named("connector", "name", status=ConnectorStatus.BLOCKED.value),
        filter_param="status",
        filter_value=ConnectorStatus.BLOCKED.value,
    ),
    Domain(
        name="configurations",
        path="/api/v1/configurations",
        label_field="name",
        sort_key="name",
        build=_named("configuration", "name", status=ConfigurationStatus.ACTIVE.value),
        build_odd=_named("configuration", "name", status=ConfigurationStatus.DRAFT.value),
        filter_param="status",
        filter_value=ConfigurationStatus.DRAFT.value,
    ),
    Domain(
        name="alerts",
        path="/api/v1/alerts",
        label_field="title",
        sort_key="title",
        build=_named("alert", "title", status=AlertStatus.OPEN.value),
        build_odd=_named("alert", "title", status=AlertStatus.RESOLVED.value),
        filter_param="status",
        filter_value=AlertStatus.RESOLVED.value,
    ),
    Domain(
        name="approvals",
        path="/api/v1/approvals",
        label_field="action",
        sort_key="action",
        build=_named("approval", "action", status=ApprovalStatus.PENDING.value),
        build_odd=_named("approval", "action", status=ApprovalStatus.APPROVED.value),
        filter_param="status",
        filter_value=ApprovalStatus.APPROVED.value,
    ),
]

#: Labels chosen so alphabetical order is not insertion order — a list that
#: ignored ``sort`` and handed back insertion order would otherwise pass.
LABELS = [
    "Delta record",
    "Alpha record",
    "Foxtrot record",
    "Bravo record",
    "Golf record",
    "Charlie record",
    "Echo record",
]

EMPTY_PAGE = {"items": [], "total": 0, "page": 1, "page_size": 25, "pages": 1}


@pytest.fixture(params=DOMAINS, ids=[domain.name for domain in DOMAINS])
def domain(request) -> Domain:
    return request.param


async def seed(domain: Domain, factory, workspace, labels: list[str] = LABELS) -> None:
    for label in labels:
        await domain.build(factory, workspace, label)


def parse_csv(response: httpx.Response) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows, "a CSV export always carries at least its header row"
    return rows[0], rows[1:]


def labels_in(response: httpx.Response, domain: Domain) -> list[str]:
    return [domain.label_of(item) for item in response.json()["items"]]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_the_pages_partition_the_collection(admin_client, factory, workspace, domain):
    """Each page reports itself and the whole, and every row appears once."""
    await seed(domain, factory, workspace)

    seen: list[str] = []
    for page, expected in ((1, 3), (2, 3), (3, 1)):
        response = await admin_client.get(
            domain.path, params={"page": page, "page_size": 3, "sort": domain.sort_key}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == expected, "the last page is short, never padded"
        assert body["total"] == len(LABELS)
        assert body["page"] == page
        assert body["page_size"] == 3
        assert body["pages"] == 3, "seven rows at three a page is three pages"
        seen.extend(labels_in(response, domain))

    assert sorted(seen) == sorted(LABELS), "no row was dropped, repeated or invented"


async def test_a_page_past_the_end_is_empty_but_still_counts(
    admin_client, factory, workspace, domain
):
    """The console keeps its "7 results" label while showing an empty page."""
    await seed(domain, factory, workspace)
    response = await admin_client.get(domain.path, params={"page": 9, "page_size": 3})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total"] == len(LABELS)
    assert body["pages"] == 3


async def test_an_empty_collection_still_reports_one_page(admin_client, domain):
    """``pages: 0`` would build a page selector with no pages in it."""
    response = await admin_client.get(domain.path)
    assert response.status_code == 200, response.text
    assert response.json() == EMPTY_PAGE


@pytest.mark.parametrize(
    "params",
    [
        {"page_size": MAX_PAGE_SIZE + 1},
        {"page": 0},
        {"page": -1},
        {"page_size": 0},
    ],
    ids=["over-the-ceiling", "page-zero", "negative-page", "zero-page-size"],
)
async def test_a_nonsensical_page_is_refused(admin_client, domain, params):
    response = await admin_client.get(domain.path, params=params)
    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_failed"


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


async def test_sort_orders_by_the_named_column_in_both_directions(
    admin_client, factory, workspace, domain
):
    await seed(domain, factory, workspace)

    ascending = await admin_client.get(
        domain.path, params={"sort": domain.sort_key, "page_size": 50}
    )
    descending = await admin_client.get(
        domain.path, params={"sort": f"-{domain.sort_key}", "page_size": 50}
    )

    assert ascending.status_code == 200, ascending.text
    assert labels_in(ascending, domain) == sorted(LABELS)
    assert labels_in(descending, domain) == sorted(LABELS, reverse=True)


async def test_sort_survives_paging(admin_client, factory, workspace, domain):
    """Page two continues where page one stopped, rather than re-sorting a slice."""
    await seed(domain, factory, workspace)

    ordered: list[str] = []
    for page in (1, 2):
        response = await admin_client.get(
            domain.path, params={"sort": domain.sort_key, "page": page, "page_size": 4}
        )
        ordered.extend(labels_in(response, domain))

    assert ordered == sorted(LABELS)


async def test_an_unsortable_key_is_refused_and_names_the_ones_that_work(
    admin_client, factory, workspace, domain
):
    """A 422 naming the legal keys beats silently ordering by something else."""
    await seed(domain, factory, workspace, LABELS[:2])

    response = await admin_client.get(domain.path, params={"sort": "not_a_column"})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert "not_a_column" in error["message"]
    sortable = error["details"]["sortable"]
    assert isinstance(sortable, list) and sortable, "the refusal must say what is allowed"
    assert domain.sort_key in sortable

    # The export shares the query contract, so it shares the refusal.
    exported = await admin_client.get(domain.export_path, params={"sort": "not_a_column"})
    assert exported.status_code == 422, exported.text
    assert error_code(exported) == "validation_failed"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_free_text_search_narrows_to_the_matching_rows(
    admin_client, factory, workspace, domain
):
    await seed(domain, factory, workspace)

    exact = await admin_client.get(domain.path, params={"q": "Foxtrot"})
    lowered = await admin_client.get(domain.path, params={"q": "foxtrot"})
    fragment = await admin_client.get(domain.path, params={"q": "record"})

    assert exact.status_code == 200, exact.text
    assert exact.json()["total"] == 1
    assert domain.label_of(exact.json()["items"][0]) == "Foxtrot record"
    assert lowered.json()["total"] == 1, "search ignores case"
    assert fragment.json()["total"] == len(LABELS), "a fragment matches, not just the whole"


async def test_the_total_reflects_the_search_not_the_collection(
    admin_client, factory, workspace, domain
):
    """A total that ignored the search would make the pager lie."""
    await seed(domain, factory, workspace)
    response = await admin_client.get(domain.path, params={"q": "Foxtrot", "page_size": 2})
    body = response.json()
    assert body["total"] == 1
    assert body["pages"] == 1


async def test_search_that_matches_nothing_is_an_empty_page_not_an_error(
    admin_client, factory, workspace, domain
):
    await seed(domain, factory, workspace)
    response = await admin_client.get(domain.path, params={"q": "zzz-no-such-thing"})
    assert response.status_code == 200, response.text
    assert response.json() == EMPTY_PAGE


# ---------------------------------------------------------------------------
# Dropdown filters
# ---------------------------------------------------------------------------


async def test_a_dropdown_filter_selects_only_its_own_rows(
    admin_client, factory, workspace, domain
):
    await seed(domain, factory, workspace)
    await domain.build_odd(factory, workspace, "Odd one out")

    filtered = await admin_client.get(
        domain.path, params={domain.filter_param: domain.filter_value}
    )

    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert domain.label_of(filtered.json()["items"][0]) == "Odd one out"

    # Filter and search narrow together rather than either winning.
    both = await admin_client.get(
        domain.path, params={domain.filter_param: domain.filter_value, "q": "record"}
    )
    assert both.json()["total"] == 0, "the odd row does not match the search"


async def test_an_unset_filter_narrows_nothing(admin_client, factory, workspace, domain):
    """An empty dropdown must not be read as "match the empty string"."""
    await seed(domain, factory, workspace)
    response = await admin_client.get(domain.path, params={domain.filter_param: ""})
    assert response.status_code in (200, 422), response.text
    if response.status_code == 200:
        assert response.json()["total"] == len(LABELS)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


async def test_the_export_is_really_csv_and_carries_the_rows(
    admin_client, factory, workspace, domain
):
    await seed(domain, factory, workspace)

    response = await admin_client.get(domain.export_path)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert ".csv" in disposition

    header, rows = parse_csv(response)
    assert len(header) > 1, "a one-column export is not a table"
    assert len(rows) == len(LABELS)
    assert all(len(row) == len(header) for row in rows), "every row fits the header"
    assert set(LABELS) <= {cell for row in rows for cell in row}


async def test_the_export_reflects_the_view_on_screen(
    admin_client, factory, workspace, domain
):
    """Export means "what I am looking at", not "everything"."""
    await seed(domain, factory, workspace)
    await domain.build_odd(factory, workspace, "Odd one out")

    filtered = await admin_client.get(
        domain.export_path, params={domain.filter_param: domain.filter_value}
    )
    searched = await admin_client.get(domain.export_path, params={"q": "Foxtrot"})
    paged = await admin_client.get(domain.export_path, params={"page_size": 2})

    _header, filtered_rows = parse_csv(filtered)
    assert len(filtered_rows) == 1
    assert "Odd one out" in set(filtered_rows[0])

    _header, searched_rows = parse_csv(searched)
    assert len(searched_rows) == 1

    _header, paged_rows = parse_csv(paged)
    assert len(paged_rows) == len(LABELS) + 1, "page_size bounds the table, never the file"


async def test_an_empty_collection_exports_a_header_and_nothing_else(admin_client, domain):
    response = await admin_client.get(domain.export_path)
    assert response.status_code == 200, response.text
    header, rows = parse_csv(response)
    assert header and rows == []


async def test_export_is_a_read_not_a_privilege(
    client, viewer_client, factory, workspace, domain
):
    """A viewer may export; refusing would only push people to screenshots."""
    await seed(domain, factory, workspace, LABELS[:2])

    allowed = await viewer_client.get(domain.export_path)
    refused = await client.get(domain.export_path)

    assert allowed.status_code == 200, allowed.text
    assert refused.status_code == 401


# ---------------------------------------------------------------------------
# Two collections that are not rows in our database
# ---------------------------------------------------------------------------


async def test_the_run_list_answers_the_same_envelope(
    admin_client, factory, workspace, engine
):
    """Runs live in the telemetry engine, and still page like everything else."""
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    for index in range(7):
        engine.add_trace(project_name=agent.engine_project_name, name=f"run-{index}")

    response = await admin_client.get("/api/v1/runs", params={"page": 2, "page_size": 3})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 7
    assert body["page"] == 2
    assert body["page_size"] == 3
    assert body["pages"] == 3
    assert len(body["items"]) == 3


async def test_the_run_list_refuses_an_unsortable_key(
    admin_client, factory, workspace, engine
):
    await factory.provisioned_agent(workspace, engine, name="Support Bot")
    response = await admin_client.get("/api/v1/runs", params={"sort": "not_a_column"})
    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_failed"
    assert response.json()["error"]["details"]["sortable"]


async def test_the_run_list_searches_and_exports_like_the_rest(
    admin_client, factory, workspace, engine
):
    # A run's searchable text is the table's own columns — the agent, the model
    # and the preview of what was asked — not the trace's internal name.
    agent = await factory.provisioned_agent(workspace, engine, name="Support Bot")
    engine.add_trace(
        project_name=agent.engine_project_name,
        name="handle request",
        input={"question": "Where is my refund?"},
    )
    engine.add_trace(
        project_name=agent.engine_project_name,
        name="handle request",
        input={"question": "Where is my order?"},
    )

    searched = await admin_client.get("/api/v1/runs", params={"q": "refund"})
    exported = await admin_client.get("/api/v1/runs/export")

    assert searched.status_code == 200, searched.text
    assert searched.json()["total"] == 1
    assert exported.headers["content-type"].startswith("text/csv")
    header, rows = parse_csv(exported)
    assert "Run ID" in header
    assert len(rows) == 2


async def test_the_audit_list_answers_the_same_envelope(admin_client, factory, workspace):
    """The trail is written by other endpoints; its list contract is still ours."""
    for index in range(7):
        secret = await factory.secret(workspace, name=f"Credential {index}")
        await admin_client.post(f"/api/v1/secrets/{secret.id}/disable", json={})

    response = await admin_client.get(
        "/api/v1/audit", params={"page": 1, "page_size": 3, "sort": "occurred_at"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 7
    assert body["pages"] == 3
    assert len(body["items"]) == 3


async def test_the_audit_list_filters_by_entity_and_refuses_a_bad_sort(
    admin_client, factory, workspace
):
    secret = await factory.secret(workspace, name="Payments key")
    other = await factory.secret(workspace, name="Analytics key")
    await admin_client.post(f"/api/v1/secrets/{secret.id}/disable", json={})
    await admin_client.post(f"/api/v1/secrets/{other.id}/disable", json={})

    filtered = await admin_client.get("/api/v1/audit", params={"entity_id": secret.id})
    refused = await admin_client.get("/api/v1/audit", params={"sort": "not_a_column"})

    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["entity_id"] == secret.id
    assert refused.status_code == 422
    assert error_code(refused) == "validation_failed"


async def test_the_audit_export_is_csv_and_carries_the_checksums(
    admin_client, factory, workspace
):
    secret = await factory.secret(workspace, name="Payments key")
    await admin_client.post(f"/api/v1/secrets/{secret.id}/disable", json={})

    response = await admin_client.get("/api/v1/audit/export")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    header, rows = parse_csv(response)
    assert "Checksum" in header
    assert len(rows) == 1
    assert rows[0][header.index("Checksum")], "an export without checksums is unverifiable"
