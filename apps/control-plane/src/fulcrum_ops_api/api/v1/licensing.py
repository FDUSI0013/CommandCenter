"""Licensing & Entitlements routes — one screen, six tabs, one router.

The tabs map onto the collections underneath them: Plans & Tiers is
``/licensing/plans``, Tenants & Licenses is ``/licensing/tenants``, Seats &
Assignments hangs off a licence at ``/licensing/tenants/{id}/seats``, Usage &
Limits is ``/licensing/tenants/{id}/entitlements``, and Billing & Renewals is
``/licensing/invoices`` with its per-invoice PDF. The Audit Log tab is served by
``/audit`` filtered on this screen and needs nothing here.

Two things are worth knowing before reading further.

``GET /licensing/entitlement-check`` is the same enforcement the ingest path
runs, exposed so a client can ask before it acts instead of discovering the
limit as a 402 mid-write. It is a read: it never records usage.

``GET /licensing/invoices`` is the one endpoint in this module that shapes its
own query. ``services.licensing`` renders invoices (CSV and PDF) but does not
page them, and rather than reach into a private helper the list is built here
from the same joins the service uses — always anchored on
``TenantLicense.tenant_workspace_id == principal.workspace_id``, so an invoice
belonging to another tenant is unreachable, not merely hidden.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute

from ...core.errors import NotFound
from ...models.identity import Workspace
from ...models.licensing import (
    Invoice,
    InvoiceStatus,
    LicensePlan,
    LicenseStatus,
    PlanStatus,
    PlanTier,
    TenantLicense,
)
from ...schemas.licensing import (
    EntitlementRead,
    ExportDataset,
    InvoiceRead,
    LicensePlanCreate,
    LicensePlanRead,
    LicensePlanUpdate,
    LicenseRevokeRequest,
    LicenseSuspendRequest,
    LicensingSummary,
    SeatAssignmentCreate,
    SeatAssignmentRead,
    SeatPurchaseRequest,
    SeatState,
    TenantEntitlementsRead,
    TenantLicenseCreate,
    TenantLicenseRead,
    TenantLicenseUpdate,
)
from ...services import licensing as service
from ..common import (
    ActionResult,
    ListParams,
    Page,
    apply_filters,
    apply_search,
    apply_sort,
    list_params,
    paginate,
)
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/licensing", tags=["Licensing"])

ListQuery = Annotated[ListParams, Depends(list_params)]

#: Sort keys accepted by the Billing & Renewals grid.
INVOICE_SORTS: dict[str, InstrumentedAttribute] = {
    "invoice_ref": Invoice.invoice_ref,
    "invoice": Invoice.invoice_ref,
    "tenant": Workspace.name,
    "plan": LicensePlan.name,
    "amount": Invoice.total_usd,
    "total_usd": Invoice.total_usd,
    "subtotal_usd": Invoice.subtotal_usd,
    "overage_usd": Invoice.overage_usd,
    "period": Invoice.period_start,
    "period_start": Invoice.period_start,
    "period_end": Invoice.period_end,
    "status": Invoice.status,
    "issued_at": Invoice.issued_at,
    "due_at": Invoice.due_at,
    "paid_at": Invoice.paid_at,
}


def _safe_filename(filename: str) -> str:
    """Strip anything that could break out of the Content-Disposition header."""
    return "".join(char for char in filename if char.isprintable() and char not in '"\\;\r\n')


def _attachment(filename: str) -> str:
    return f'attachment; filename="{_safe_filename(filename)}"'


# --------------------------------------------------------------------------- #
# KPI row, export and the enforcement check
# --------------------------------------------------------------------------- #


@router.get("/summary", response_model=LicensingSummary, summary="Licensing KPI summary")
async def get_summary(principal: CurrentPrincipal, session: Db) -> LicensingSummary:
    """The six KPI cards, plus the billing tallies beneath them.

    Plans, Active Tenant Licenses, Seats Assigned, Expiring in 30 Days,
    Suspended / Revoked and Overage Alerts — every one a SQL aggregate, with
    overage derived by comparing metered usage in the rolling window against the
    allowance each plan includes for that meter.
    """
    return await service.summary(session, principal)


@router.get(
    "/export",
    summary="Export a licensing dataset as CSV",
    response_class=StreamingResponse,
)
async def export_licensing(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    dataset: Annotated[
        ExportDataset, Query(description="Which tab to export")
    ] = "plans",
    license_status: Annotated[
        # A plain string on purpose: each tab has its own status vocabulary
        # (plan Draft/Retired, invoice Paid/Overdue, licence Active/Trial…) and
        # the service applies it per-dataset; an enum here 422s every tab but
        # the licences one.
        str | None,
        Query(alias="status", description="Status in the exported tab's own vocabulary"),
    ] = None,
    tier: Annotated[PlanTier | None, Query(description="Plan tier")] = None,
    billing_period: Annotated[str | None, Query(description="Monthly or Annual")] = None,
    plan_id: Annotated[str | None, Query(description="Restrict to one plan")] = None,
    license_id: Annotated[str | None, Query(description="Restrict to one licence")] = None,
    role: Annotated[str | None, Query(description="Seat role, for the seats dataset")] = None,
    state: Annotated[
        SeatState | None, Query(description="Seat state: active or released")
    ] = None,
    metric: Annotated[str | None, Query(description="Usage meter, for the usage dataset")] = None,
    auto_renew: Annotated[bool | None, Query(description="Only auto-renewing licences")] = None,
    expiring_in_days: Annotated[
        int | None, Query(ge=1, le=3650, description="Licences expiring within N days")
    ] = None,
) -> StreamingResponse:
    """The header's Export button, for whichever tab is open.

    ``dataset`` selects plans, tenants, seats, usage or invoices; the dropdown
    filters and the search box are replayed against it, so the file always
    matches the grid on screen. An empty result still yields a header row rather
    than a zero-byte download.
    """
    body, filename = await service.export_csv(
        session,
        principal,
        dataset=dataset,
        q=params.q,
        sort=params.sort,
        status=license_status,
        tier=tier.value if tier else None,
        billing_period=billing_period,
        plan_id=plan_id,
        license_id=license_id,
        role=role,
        state=state,
        metric=metric,
        auto_renew=auto_renew,
        expiring_in_days=expiring_in_days,
    )
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _attachment(filename)},
    )


@router.get(
    "/entitlement-check",
    response_model=ActionResult,
    summary="Check an entitlement before acting on it",
)
async def entitlement_check(
    principal: CurrentPrincipal,
    session: Db,
    key: Annotated[str, Query(min_length=1, max_length=120, description="Entitlement key")],
    amount: Annotated[int, Query(ge=0, description="Units this call would consume")] = 1,
    current_usage: Annotated[int, Query(ge=0, description="Units already consumed")] = 0,
    enforce: Annotated[
        bool, Query(description="Apply enforcement and answer 402 when the cap is breached")
    ] = True,
) -> ActionResult:
    """The enforcement the ingest path runs, exposed as a pre-flight check.

    With ``enforce`` on (the default) this behaves exactly as ingest does: a
    breached hard limit answers 402 and a suspended licence answers 402, while a
    soft limit is allowed through with the tenant warned on Usage & Limits. Turn
    it off to resolve the value without the gate — useful for feature flags,
    where the client wants to hide a control rather than be refused at it.

    A workspace holding no licence is not blocked: absence of a licence is a
    data gap, not a breached limit. ``entitled`` is false in that case and
    ``value`` is null.
    """
    if enforce:
        value = await service.enforce_entitlement(
            session,
            principal.workspace_id,
            key,
            amount=amount,
            current_usage=current_usage,
        )
    else:
        value = await service.check_entitlement(session, principal.workspace_id, key)

    entitled = value is not None and value is not False
    return ActionResult(
        ok=entitled,
        message=(
            f"'{key}' is granted on this workspace's plan."
            if entitled
            else f"'{key}' is not granted on this workspace's plan."
        ),
        data={
            "key": key,
            "value": value,
            "entitled": entitled,
            "enforced": enforce,
            "amount": amount,
            "current_usage": current_usage,
        },
    )


# --------------------------------------------------------------------------- #
# Billing & Renewals
# --------------------------------------------------------------------------- #


@router.get("/invoices", response_model=Page[InvoiceRead], summary="List invoices")
async def list_invoices(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    invoice_status: Annotated[
        InvoiceStatus | None, Query(alias="status", description="Draft, Issued, Paid, …")
    ] = None,
    license_id: Annotated[str | None, Query(description="Restrict to one licence")] = None,
) -> Page[InvoiceRead]:
    """One page of the Billing & Renewals grid, newest period first.

    Scoping is the join itself: every invoice is reached through its licence and
    the licence must belong to the calling workspace, so another tenant's
    billing history is not addressable here at all. Free-text search covers the
    invoice reference, the tenant name and the plan name; each row carries the
    tenant and plan labels the grid renders.
    """
    stmt = (
        select(Invoice)
        .join(TenantLicense, TenantLicense.id == Invoice.license_id)
        .join(LicensePlan, LicensePlan.id == TenantLicense.plan_id)
        .join(Workspace, Workspace.id == TenantLicense.tenant_workspace_id)
        .where(TenantLicense.tenant_workspace_id == principal.workspace_id)
    )
    stmt = apply_search(
        stmt, params, [Invoice.invoice_ref, Workspace.name, LicensePlan.name]
    )
    stmt = apply_filters(
        stmt,
        {
            Invoice.status: invoice_status.value if invoice_status else None,
            Invoice.license_id: license_id,
        },
    )
    stmt = apply_sort(stmt, params, INVOICE_SORTS, Invoice.period_start)
    # Ties on the sort column would otherwise shuffle between pages.
    stmt = stmt.order_by(Invoice.invoice_ref.desc())
    rows, total = await paginate(session, stmt, params)

    labels: dict[str, tuple[str | None, str | None]] = {}
    if rows:
        label_stmt = (
            select(TenantLicense.id, Workspace.name, LicensePlan.name)
            .join(LicensePlan, LicensePlan.id == TenantLicense.plan_id)
            .join(Workspace, Workspace.id == TenantLicense.tenant_workspace_id)
            .where(TenantLicense.id.in_({row.license_id for row in rows}))
        )
        labels = {
            license_key: (tenant_name, plan_name)
            for license_key, tenant_name, plan_name in (
                await session.execute(label_stmt)
            ).all()
        }

    items = []
    for row in rows:
        tenant_name, plan_name = labels.get(row.license_id, (None, None))
        items.append(
            InvoiceRead.model_validate(row).model_copy(
                update={"tenant_name": tenant_name, "plan_name": plan_name}
            )
        )
    return Page.build(items, total, params.page, params.page_size)


@router.get(
    "/invoices/{invoice_id}/pdf",
    summary="Download an invoice as PDF",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def download_invoice_pdf(
    principal: CurrentPrincipal, session: Db, invoice_id: str
) -> Response:
    """Render one invoice as a PDF and return the bytes — the grid's PDF button.

    The document is assembled in-process from the invoice's frozen line items,
    so producing a bill never depends on a third-party service or a rendering
    toolchain. ``invoice_id`` accepts either the primary key or the human
    reference the grid shows, e.g. ``INV-2026-0712``.
    """
    pdf, filename = await service.invoice_pdf(session, principal, invoice_id)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _attachment(filename),
            "Content-Length": str(len(pdf)),
        },
    )


# --------------------------------------------------------------------------- #
# Plans & Tiers
# --------------------------------------------------------------------------- #


@router.get("/plans", response_model=Page[LicensePlanRead], summary="List plans")
async def list_plans(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    tier: Annotated[PlanTier | None, Query(description="Enterprise, Pro, Standard, …")] = None,
    plan_status: Annotated[
        PlanStatus | None, Query(alias="status", description="Active, Draft or Retired")
    ] = None,
    billing_period: Annotated[str | None, Query(description="Monthly or Annual")] = None,
) -> Page[LicensePlanRead]:
    """One page of the plan catalogue, ordered by name by default.

    The catalogue is platform-level, but the four counters on each row — licence
    count, seats purchased, seats assigned and utilisation — are aggregated over
    the calling workspace's own licences, which is what the Seats Used and
    Utilization columns show.
    """
    items, total = await service.list_plans(
        session,
        principal,
        params,
        tier=tier.value if tier else None,
        status=plan_status.value if plan_status else None,
        billing_period=billing_period,
    )
    return Page.build(items, total, params.page, params.page_size)


@router.post(
    "/plans",
    response_model=LicensePlanRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a plan",
)
async def create_plan(
    principal: CurrentPrincipal,
    session: Db,
    payload: LicensePlanCreate,
    request: Request,
) -> LicensePlanRead:
    """Publish a plan — the "Create License Plan" dialog.

    The plan code is the stable identifier and is unique across the catalogue,
    so a duplicate answers 409. Plans start Draft unless a status is supplied.
    Requires the owner role: this changes what can be sold.
    """
    return await service.create_plan(session, principal, payload, request)


@router.get("/plans/{plan_id}", response_model=LicensePlanRead, summary="Get a plan")
async def get_plan(
    principal: CurrentPrincipal, session: Db, plan_id: str
) -> LicensePlanRead:
    """One plan from the catalogue, with this workspace's take-up alongside it."""
    return await service.get_plan(session, principal, plan_id)


@router.patch("/plans/{plan_id}", response_model=LicensePlanRead, summary="Update a plan")
async def update_plan(
    principal: CurrentPrincipal,
    session: Db,
    plan_id: str,
    payload: LicensePlanUpdate,
    request: Request,
) -> LicensePlanRead:
    """Amend a plan — the "Edit Plan" action.

    Withdrawing a plan from sale is a status change to Retired, not a delete, so
    licences that reference it keep resolving. An edit to ``features`` or to an
    ``included_*`` allowance is carried through to the entitlements of every
    current licence sold on the plan, in the same transaction; revoked and
    expired licences keep what they ended with. Requires the owner role.
    """
    return await service.update_plan(session, principal, plan_id, payload, request)


@router.delete(
    "/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a plan"
)
async def delete_plan(
    principal: CurrentPrincipal, session: Db, plan_id: str, request: Request
) -> None:
    """Remove a plan that was never sold.

    A plan any licence references answers 409 with the count; retire it instead.
    Requires the owner role.
    """
    await service.delete_plan(session, principal, plan_id, request)


# --------------------------------------------------------------------------- #
# Tenants & Licenses
# --------------------------------------------------------------------------- #


@router.get("/tenants", response_model=Page[TenantLicenseRead], summary="List tenant licenses")
async def list_licenses(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQuery,
    license_status: Annotated[
        LicenseStatus | None, Query(alias="status", description="Active, Trial, Suspended, …")
    ] = None,
    plan_id: Annotated[str | None, Query(description="Restrict to one plan")] = None,
    tier: Annotated[PlanTier | None, Query(description="Plan tier")] = None,
    auto_renew: Annotated[bool | None, Query(description="Only auto-renewing licences")] = None,
    expiring_in_days: Annotated[
        int | None, Query(ge=1, le=3650, description="Expiring within N days")
    ] = None,
) -> Page[TenantLicenseRead]:
    """One page of the Tenants & Licenses grid.

    Each row resolves its plan's name, tier and allowances, and counts down to
    renewal in ``days_until_expiry``, so the grid renders Token Capacity,
    Workflow Capacity and Renewal without a request per row.
    """
    items, total = await service.list_licenses(
        session,
        principal,
        params,
        status=license_status.value if license_status else None,
        plan_id=plan_id,
        tier=tier.value if tier else None,
        auto_renew=auto_renew,
        expiring_in_days=expiring_in_days,
    )
    return Page.build(items, total, params.page, params.page_size)


@router.post(
    "/tenants",
    response_model=TenantLicenseRead,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a tenant license",
)
async def create_license(
    principal: CurrentPrincipal,
    session: Db,
    payload: TenantLicenseCreate,
    request: Request,
) -> TenantLicenseRead:
    """Sell a plan to the calling workspace.

    A workspace holds at most one current licence — entitlement resolution has
    to be unambiguous — so a second one answers 409 until the existing licence
    is revoked or has expired. Retired plans cannot be sold, and an
    ``expires_at`` that is not later than the start of the term (``starts_at``,
    or now when it is omitted) answers 422. Requires the owner role.
    """
    return await service.create_license(session, principal, payload, request)


@router.get(
    "/tenants/{license_id}", response_model=TenantLicenseRead, summary="Get a tenant license"
)
async def get_license(
    principal: CurrentPrincipal, session: Db, license_id: str
) -> TenantLicenseRead:
    """One licence. A licence held by another workspace answers 404, not 403."""
    return await service.get_license(session, principal, license_id)


@router.patch(
    "/tenants/{license_id}",
    response_model=TenantLicenseRead,
    summary="Update a tenant license",
)
async def update_license(
    principal: CurrentPrincipal,
    session: Db,
    license_id: str,
    payload: TenantLicenseUpdate,
    request: Request,
) -> TenantLicenseRead:
    """Amend a licence, including the plan change behind "Upgrade Plan".

    Purchased seats cannot drop below the number currently assigned — release
    seats first — and a revoked or expired licence can no longer be amended.
    Suspension, reactivation and revocation have their own endpoints because
    they carry state the platform owns; sending ``status`` for a suspended
    licence answers 412 rather than lifting the suspension unchecked. The term
    is validated only when ``starts_at`` or ``expires_at`` is in the body, so
    this is also how a lapsed or mis-entered term is corrected: send a later
    ``expires_at``. Sending ``plan_id`` re-resolves the licence's entitlements
    from that plan -- on a move to another plan, and equally when it names the
    plan already held, which is how a licence still carrying an earlier plan's
    limits is repaired. Requires the owner role.
    """
    return await service.update_license(session, principal, license_id, payload, request)


@router.delete(
    "/tenants/{license_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tenant license",
)
async def delete_license(
    principal: CurrentPrincipal, session: Db, license_id: str, request: Request
) -> None:
    """Delete a licence that was never billed.

    Once an invoice exists the licence answers 409: suspend it instead, so the
    billing history stays intact. Deleting takes its seats, entitlements and
    usage records with it. Requires the owner role.
    """
    await service.delete_license(session, principal, license_id, request)


@router.post(
    "/tenants/{license_id}/suspend",
    response_model=ActionResult,
    summary="Suspend a tenant license",
)
async def suspend_license(
    principal: CurrentPrincipal,
    session: Db,
    license_id: str,
    payload: LicenseSuspendRequest,
    request: Request,
) -> ActionResult:
    """Suspend a licence — the "Suspend Plan" action.

    New seat assignment is blocked from this moment on, and entitlement
    enforcement starts refusing — which includes agent telemetry: every ingest
    batch for the workspace answers 402 until the licence is reactivated, and
    the SDKs drop a 402 rather than retry it. The response says so
    (``data.ingest_refused``). Seats already held are only released when
    ``release_seats`` is set, because taking a team's access away is a separate,
    deliberate choice. Requires the owner role.
    """
    return await service.suspend_license(session, principal, license_id, payload, request)


@router.post(
    "/tenants/{license_id}/revoke",
    response_model=ActionResult,
    summary="Revoke a tenant license",
)
async def revoke_license(
    principal: CurrentPrincipal,
    session: Db,
    license_id: str,
    payload: LicenseRevokeRequest,
    request: Request,
) -> ActionResult:
    """Revoke a licence for good — the way out the 409 on a second licence names.

    Terminal, unlike suspension: the row stays as history, can no longer be
    amended or reactivated, and stops being the workspace's current licence, so
    a new one can be issued. Every seat still held is released. An already
    revoked licence answers 409 and an expired one 412. The optional ``reason``
    is kept on the audit event. Requires the owner role.
    """
    return await service.revoke_license(session, principal, license_id, payload, request)


@router.post(
    "/tenants/{license_id}/reactivate",
    response_model=ActionResult,
    summary="Reactivate a tenant license",
)
async def reactivate_license(
    principal: CurrentPrincipal, session: Db, license_id: str, request: Request
) -> ActionResult:
    """Lift a suspension and return the licence to service.

    A licence whose term has already ended answers 412 — extend ``expires_at``
    first — and one whose renewal is inside the 30-day horizon comes back as
    Expiring Soon rather than Active, so the KPI stays honest. Requires the
    owner role.
    """
    return await service.reactivate_license(session, principal, license_id, request)


@router.get(
    "/tenants/{license_id}/entitlements",
    response_model=TenantEntitlementsRead,
    summary="Resolved entitlements and metered usage",
)
async def get_entitlements(
    principal: CurrentPrincipal, session: Db, license_id: str
) -> TenantEntitlementsRead:
    """Everything the Usage & Limits tab renders, in one call.

    The plan's feature bullets, the enforceable entitlement set, and usage per
    meter over the rolling 30-day window with each meter's allowance, remaining
    balance, utilisation and over-limit flag. ``over_limit_metrics`` is what the
    Overage Alerts panel lists.
    """
    return await service.license_entitlements(session, principal, license_id)


# --------------------------------------------------------------------------- #
# Seats & Assignments
# --------------------------------------------------------------------------- #


@router.get(
    "/tenants/{license_id}/seats",
    response_model=Page[SeatAssignmentRead],
    summary="List seat assignments",
)
async def list_seats(
    principal: CurrentPrincipal,
    session: Db,
    license_id: str,
    params: ListQuery,
    state: Annotated[
        SeatState | None, Query(description="active or released; omit for both")
    ] = None,
    role: Annotated[str | None, Query(description="Seat role, e.g. operator")] = None,
) -> Page[SeatAssignmentRead]:
    """One page of the seats issued against a licence, released ones included.

    Released seats are kept rather than deleted: who held a seat and when is
    licence history. Free-text search covers the holder's name and email, and
    each row carries the name, email and initials the table renders.
    """
    items, total = await service.list_seats(
        session, principal, license_id, params, state=state, role=role
    )
    return Page.build(items, total, params.page, params.page_size)


@router.post(
    "/tenants/{license_id}/seats",
    response_model=SeatAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Assign or reassign a seat",
)
async def assign_seat(
    principal: CurrentPrincipal,
    session: Db,
    license_id: str,
    payload: SeatAssignmentCreate,
    request: Request,
) -> SeatAssignmentRead:
    """Give a user a seat, or hand one over — the "Reassign" button.

    Sending ``replaces_user_id`` releases that user's seat and issues the new
    one in the same transaction, so a fully allocated licence can be reshuffled
    without buying a spare. Assignments serialise on the licence row and the
    seat count is re-read under that lock, so two concurrent assignments cannot
    both slip past the purchased ceiling (the loser answers 402) nor give one
    user two seats (the loser answers 409). A user who holds a duplicate seat
    from before that lock existed has all of them released by a reassignment.
    A suspended, revoked or expired licence refuses new assignment with 412.
    Requires the admin role.
    """
    return await service.assign_seat(session, principal, license_id, payload, request)


@router.post(
    "/tenants/{license_id}/seats/purchase",
    response_model=ActionResult,
    summary="Purchase additional seats",
)
async def purchase_seats(
    principal: CurrentPrincipal,
    session: Db,
    license_id: str,
    payload: SeatPurchaseRequest,
    request: Request,
) -> ActionResult:
    """Add seats to a licence — the "Purchase Seats" button.

    This changes what the tenant is billed, so it requires the owner role and is
    audited with the before and after pool sizes and the purchase order
    reference. The response carries the new pool, what is assigned against it
    and the resulting utilisation, which is what the Seat Pools panel redraws.
    """
    return await service.purchase_seats(session, principal, license_id, payload, request)


@router.get(
    "/tenants/{license_id}/entitlements/{key}",
    response_model=EntitlementRead,
    summary="Get one entitlement",
)
async def get_entitlement(
    principal: CurrentPrincipal, session: Db, license_id: str, key: str
) -> EntitlementRead:
    """One entitlement of a licence, by key — the Feature Entitlements matrix.

    Resolved from the same set the Usage & Limits tab renders, so a client can
    read a single flag without pulling the whole document. An unknown key
    answers 404.
    """
    resolved = await service.license_entitlements(session, principal, license_id)
    for entitlement in resolved.entitlements:
        if entitlement.key == key:
            return entitlement
    raise NotFound(f"This license grants no entitlement named '{key}'.")
