"""Policy Center endpoints.

The Policy Center is where a governance owner writes the rules the enforcement
path applies before an agent acts, and where they see what those rules did. The
handlers here stay thin: they parse the query the table sent, hand it to
``services.policies``, and shape the answer. Tenancy, authority and the audit
trail all live in the service.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from ...models.governance import (
    Policy,
    PolicyCategory,
    PolicyEnforcement,
    PolicyStatus,
    RiskLevel,
    ViolationSeverity,
)
from ...schemas.policies import (
    PolicyActionResponse,
    PolicyBindingRead,
    PolicyCloneRequest,
    PolicyCreate,
    PolicyDeactivateRequest,
    PolicyImportRequest,
    PolicyImportResult,
    PolicyRead,
    PolicySummary,
    PolicyUpdate,
    PolicyViolationRead,
    enforced_mode,
)
from ...services import policies as service
from ..common import ListParams, Page, list_params, to_csv
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/policies", tags=["Policies"])

#: Column order of the CSV export, matching the Policy Center table left to right.
EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("id", "Policy ID"),
    ("name", "Policy Name"),
    ("description", "Description"),
    ("category", "Category"),
    ("scope", "Scope"),
    ("scope_label", "Scope Target"),
    ("risk_level", "Risk Level"),
    ("status", "Status"),
    ("enforcement", "Enforcement"),
    ("version", "Version"),
    ("applies_agents", "Agents In Scope"),
    ("applies_connectors", "Connectors In Scope"),
    ("applies_envs", "Environments In Scope"),
    ("violations_30d", "Violations (30d)"),
    ("blocked_30d", "Blocked (30d)"),
    ("requests_30d", "Requests (30d)"),
    ("approved_pct", "Approved %"),
    ("last_triggered_at", "Last Triggered"),
    ("updated_at", "Last Modified"),
    ("updated_by", "Modified By"),
]


@dataclasses.dataclass(frozen=True)
class PolicyFilters:
    """The Policy Center's four dropdowns, plus the two an SDK caller may add."""

    status: tuple[str, ...] = ()
    category: tuple[str, ...] = ()
    scope: str | None = None
    risk: tuple[str, ...] = ()
    enforcement: tuple[str, ...] = ()
    owner_user_id: str | None = None


def policy_filters(
    status_filter: Annotated[
        list[PolicyStatus] | None,
        Query(alias="status", description="Status chips; repeat for more than one."),
    ] = None,
    category: Annotated[
        list[PolicyCategory] | None, Query(description="Category tabs and dropdown.")
    ] = None,
    scope: Annotated[
        str | None,
        Query(description="Scope class ('Agent') or rendered label ('Finance Agents')."),
    ] = None,
    risk: Annotated[list[RiskLevel] | None, Query(description="Risk level chips.")] = None,
    enforcement: Annotated[
        list[PolicyEnforcement] | None, Query(description="Enforcement mode.")
    ] = None,
    owner_user_id: Annotated[str | None, Query(description="Policy owner.")] = None,
) -> PolicyFilters:
    """Read the table's dropdown filters off the query string."""
    return PolicyFilters(
        status=tuple(item.value for item in status_filter or ()),
        category=tuple(item.value for item in category or ()),
        scope=scope,
        risk=tuple(item.value for item in risk or ()),
        enforcement=tuple(item.value for item in enforcement or ()),
        owner_user_id=owner_user_id,
    )


Filters = Annotated[PolicyFilters, Depends(policy_filters)]
Params = Annotated[ListParams, Depends(list_params)]


def _csv_row(policy: Policy) -> dict[str, Any]:
    row: dict[str, Any] = {key: getattr(policy, key, None) for key, _ in EXPORT_COLUMNS}
    # The file says what the table says: the mode the rule body enforces.
    row["enforcement"] = enforced_mode(policy.rules, policy.enforcement)
    for key in ("last_triggered_at", "updated_at"):
        value = row.get(key)
        if isinstance(value, dt.datetime):
            row[key] = value.isoformat()
    return row


def _action_response(
    policy: Policy, message: str, *, agents_affected: int = 0, bound_agents: int = 0
) -> PolicyActionResponse:
    return PolicyActionResponse(
        policy=PolicyRead.model_validate(policy),
        message=message,
        agents_affected=agents_affected,
        bound_agents=bound_agents,
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[PolicyRead], summary="List policies")
async def list_policies(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
) -> Page[PolicyRead]:
    """Page through the workspace's policies.

    Supports free-text search over name, description, category, scope label and
    version, sorting by any table column, and the Status, Category, Scope and
    Risk Level dropdowns.
    """
    rows, total = await service.list_policies(
        session,
        principal,
        params,
        status=filters.status,
        category=filters.category,
        scope=filters.scope,
        risk=filters.risk,
        enforcement=filters.enforcement,
        owner_user_id=filters.owner_user_id,
    )
    return Page[PolicyRead].build(rows, total, params.page, params.page_size)


@router.get("/summary", response_model=PolicySummary, summary="Policy KPI summary")
async def policy_summary(
    principal: CurrentPrincipal,
    session: Db,
    window_days: Annotated[int, Query(ge=1, le=365, description="Rolling window.")] = 30,
) -> PolicySummary:
    """The KPI cards above the table: totals by status and the windowed violation numbers."""
    return await service.summary(session, principal, window_days=window_days)


@router.get(
    "/violations",
    response_model=Page[PolicyViolationRead],
    summary="List policy violations",
)
async def list_violations(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    policy_id: Annotated[str | None, Query(description="Restrict to one policy.")] = None,
    agent_id: Annotated[str | None, Query(description="Restrict to one agent.")] = None,
    severity: Annotated[
        list[ViolationSeverity] | None, Query(description="Severity chips.")
    ] = None,
    action: Annotated[
        list[PolicyEnforcement] | None,
        Query(description="Enforcement that actually fired."),
    ] = None,
    resolved: Annotated[
        bool | None, Query(description="True for closed violations, false for open ones.")
    ] = None,
    window_days: Annotated[
        int | None, Query(ge=1, le=365, description="Only breaches inside this window.")
    ] = None,
) -> Page[PolicyViolationRead]:
    """Every breach recorded by the enforcement path, newest first.

    Each row carries the policy and agent names so the feed renders without a
    second round trip. Pass ``policy_id`` for the inspector's Violations tab.
    """
    rows, total = await service.list_violations(
        session,
        principal,
        params,
        policy_id=policy_id,
        agent_id=agent_id,
        severity=[item.value for item in severity] if severity else None,
        action=[item.value for item in action] if action else None,
        resolved=resolved,
        window_days=window_days,
    )
    items = [
        PolicyViolationRead.from_row(violation, policy_name=policy_name, agent_name=agent_name)
        for violation, policy_name, agent_name in rows
    ]
    return Page[PolicyViolationRead].build(items, total, params.page, params.page_size)


@router.get("/export", summary="Export policies as CSV")
async def export_policies(
    principal: CurrentPrincipal,
    session: Db,
    params: Params,
    filters: Filters,
) -> StreamingResponse:
    """Download the filtered policy list as CSV.

    Honours exactly the search, sort and filters the table is showing, so the
    file matches what the operator sees on screen.
    """
    rows = await service.export_policies(
        session,
        principal,
        params,
        status=filters.status,
        category=filters.category,
        scope=filters.scope,
        risk=filters.risk,
        enforcement=filters.enforcement,
        owner_user_id=filters.owner_user_id,
    )
    body = to_csv([_csv_row(policy) for policy in rows], EXPORT_COLUMNS)
    filename = f"policies-{dt.datetime.now(dt.UTC):%Y%m%d}.csv"
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "",
    response_model=PolicyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a policy",
)
async def create_policy(
    principal: CurrentPrincipal,
    session: Db,
    body: PolicyCreate,
    request: Request,
) -> PolicyRead:
    """Create a policy and, unless told otherwise, start enforcing it.

    The rule body is validated against the condition/action/severity schema and
    its signals against the vocabulary the enforcement path resolves. If it is
    omitted, an opening condition is derived from the category, risk level and
    enforcement mode supplied here, and the policy is saved ``Inactive`` whatever
    ``status`` asked for — read ``status`` from the response. A derived rule is a
    draft; it enforces nothing until somebody has reviewed and activated it.
    """
    policy = await service.create_policy(session, principal, body, request=request)
    return PolicyRead.model_validate(policy)


@router.post(
    "/import", response_model=PolicyImportResult, summary="Import policy definitions"
)
async def import_policies(
    principal: CurrentPrincipal,
    session: Db,
    body: PolicyImportRequest,
    request: Request,
) -> PolicyImportResult:
    """Bulk-load policy definitions from an uploaded file.

    Each definition is validated in its own right; collisions and bad scope
    targets are returned in ``issues`` rather than failing the batch, unless
    ``on_conflict`` is ``fail``, which aborts and imports nothing.
    """
    return await service.import_policies(session, principal, body, request=request)


# ---------------------------------------------------------------------------
# Single policy
# ---------------------------------------------------------------------------


@router.get("/{policy_id}", response_model=PolicyRead, summary="Get a policy")
async def get_policy(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
) -> PolicyRead:
    """One policy, including its full rule body and reach counters."""
    policy = await service.get_policy(session, principal, policy_id)
    return PolicyRead.model_validate(policy)


@router.patch("/{policy_id}", response_model=PolicyRead, summary="Update a policy")
async def update_policy(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
    body: PolicyUpdate,
    request: Request,
) -> PolicyRead:
    """Apply a partial update.

    Send ``expected_updated_at`` to make the write conditional: if another
    editor saved in the meantime the request is rejected with 409 rather than
    overwriting their change. A new rule body bumps the policy version.

    ``enforcement`` and ``rules.action.mode`` are one setting. Change either and
    the other follows, so the badge and the enforcement path cannot disagree;
    change both to different values and the request is refused with 422.
    """
    policy = await service.update_policy(session, principal, policy_id, body, request=request)
    return PolicyRead.model_validate(policy)


@router.delete(
    "/{policy_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a policy"
)
async def delete_policy(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
    request: Request,
) -> Response:
    """Delete a policy that is no longer enforced.

    Deleting cascades to the policy's bindings and violation history, so an
    active policy must be deactivated first.
    """
    await service.delete_policy(session, principal, policy_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{policy_id}/activate",
    response_model=PolicyActionResponse,
    summary="Activate a policy",
)
async def activate_policy(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
    request: Request,
) -> PolicyActionResponse:
    """Start enforcing a policy and report how many agents it now covers.

    A policy with no conditions cannot be activated: there would be nothing for
    the enforcement path to evaluate.
    """
    policy, affected, bound = await service.activate_policy(
        session, principal, policy_id, request=request
    )
    return _action_response(
        policy,
        f"{policy.name} is now enforced across {affected} agent(s).",
        agents_affected=affected,
        bound_agents=bound,
    )


@router.post(
    "/{policy_id}/deactivate",
    response_model=PolicyActionResponse,
    summary="Deactivate a policy",
)
async def deactivate_policy(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
    request: Request,
    body: PolicyDeactivateRequest | None = None,
) -> PolicyActionResponse:
    """Stop enforcing a policy.

    Policies bound to agents deactivate like any other; the response reports how
    many agents lose enforcement so the console can say so in its confirmation.
    Any ``reason`` supplied is written into the audit trail.
    """
    policy, affected, bound = await service.deactivate_policy(
        session,
        principal,
        policy_id,
        reason=body.reason if body else None,
        request=request,
    )
    return _action_response(
        policy,
        f"{policy.name} is no longer enforced; {affected} agent(s) affected.",
        agents_affected=affected,
        bound_agents=bound,
    )


@router.post(
    "/{policy_id}/clone", response_model=PolicyActionResponse, summary="Clone a policy"
)
async def clone_policy(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
    request: Request,
    body: PolicyCloneRequest | None = None,
) -> PolicyActionResponse:
    """Copy a policy as an inactive draft named ``<name> (Copy)``.

    The draft carries the rule body and scope but none of the original's
    history, and enforces nothing until it is activated.
    """
    policy = await service.clone_policy(
        session,
        principal,
        policy_id,
        name=body.name if body else None,
        request=request,
    )
    return _action_response(policy, f"{policy.name} created as an inactive draft.")


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


@router.get(
    "/{policy_id}/bindings",
    response_model=list[PolicyBindingRead],
    summary="List a policy's agent bindings",
)
async def list_bindings(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
) -> list[PolicyBindingRead]:
    """The agents explicitly bound to a policy, as opposed to matched by its scope."""
    rows = await service.list_bindings(session, principal, policy_id)
    return [
        PolicyBindingRead.model_validate(binding).model_copy(update={"agent_name": name})
        for binding, name in rows
    ]


@router.post(
    "/{policy_id}/bindings/{agent_id}",
    response_model=PolicyActionResponse,
    summary="Bind a policy to an agent",
)
async def bind_agent(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
    agent_id: str,
    request: Request,
) -> PolicyActionResponse:
    """Attach a policy to one agent regardless of the policy's scope.

    Idempotent: binding an agent that is already bound answers 200 with the same
    shape and changes nothing. An Active policy governs the agent from its next
    trace.
    """
    policy, affected, bound, created = await service.bind_agent(
        session, principal, policy_id, agent_id, request=request
    )
    return _action_response(
        policy,
        f"{policy.name} is now bound to this agent."
        if created
        else f"{policy.name} was already bound to this agent.",
        agents_affected=affected,
        bound_agents=bound,
    )


@router.delete(
    "/{policy_id}/bindings/{agent_id}",
    response_model=PolicyActionResponse,
    summary="Unbind a policy from an agent",
)
async def unbind_agent(
    principal: CurrentPrincipal,
    session: Db,
    policy_id: str,
    agent_id: str,
    request: Request,
) -> PolicyActionResponse:
    """Remove an explicit binding; idempotent like its counterpart.

    The policy keeps applying to the agent if its scope matches on its own.
    """
    policy, affected, bound, removed = await service.unbind_agent(
        session, principal, policy_id, agent_id, request=request
    )
    return _action_response(
        policy,
        f"{policy.name} is no longer bound to this agent."
        if removed
        else f"{policy.name} was not bound to this agent.",
        agents_affected=affected,
        bound_agents=bound,
    )
