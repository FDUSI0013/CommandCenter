"""Secrets & Credentials routes.

Eleven endpoints back one screen: the vault table with its four dropdowns, the
KPI cards and donut above it, the metadata export beside them, the inspector's
reveal / rotate / disable / enable buttons, and the per-credential access
history behind "View Audit Log".

Handlers here only parse, delegate and shape. Workspace scoping, the admin
gate on every mutation, encryption, the access-log rows and the audit writes
all live in ``services.secrets`` — including on the paths that refuse, because
a refusal is evidence and the service is where evidence is written.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from ...models.governance import RiskLevel, SecretAccessAction, SecretStatus, SecretType
from ...schemas.secrets import (
    AccessLogFilters,
    RotationState,
    SecretAccessLogRead,
    SecretCreate,
    SecretDisableRequest,
    SecretFilters,
    SecretRead,
    SecretRevealRequest,
    SecretRevealResult,
    SecretRotateRequest,
    SecretRotateResult,
    SecretsSummary,
    SecretUpdate,
)
from ...services import secrets as service
from ..common import ListParams, Page, list_params
from ..deps import CurrentPrincipal, Db

router = APIRouter(prefix="/secrets", tags=["Secrets"])


def secret_filters(
    type_: Annotated[
        list[SecretType] | None,
        Query(alias="type", description="Credential type, e.g. 'API Key'. Repeatable."),
    ] = None,
    secret_type: Annotated[
        list[SecretType] | None, Query(description="Alias of `type`.")
    ] = None,
    env: Annotated[
        list[str] | None,
        Query(description="Environment, e.g. 'Production'. Repeatable."),
    ] = None,
    environment: Annotated[list[str] | None, Query(description="Alias of `env`.")] = None,
    status: Annotated[
        list[SecretStatus] | None,
        Query(description="Vault status chip, e.g. 'Active'. Repeatable."),
    ] = None,
    vault: Annotated[
        list[str] | None,
        Query(description="Store, e.g. 'Azure Key Vault'. Repeatable."),
    ] = None,
    risk: Annotated[RiskLevel | None, Query(description="Low, Medium or High")] = None,
    owner_user_id: Annotated[str | None, Query(description="Owning user's id")] = None,
    privileged: Annotated[
        bool | None, Query(description="Only credentials that require step-up access")
    ] = None,
    rotation_state: Annotated[
        RotationState | None,
        Query(description="overdue, due_soon, scheduled or none"),
    ] = None,
    expiring_within_days: Annotated[
        int | None,
        Query(ge=1, le=365, description="Only credentials expiring inside this window"),
    ] = None,
) -> SecretFilters:
    """The vault table's dropdowns, shared by the list and export endpoints.

    ``type`` and ``env`` are the keys the console's filter bar sends; the
    column names are accepted as aliases so an SDK caller can use either.
    """
    return SecretFilters(
        secret_type=[*(type_ or []), *(secret_type or [])] or None,
        environment=[*(env or []), *(environment or [])] or None,
        status=status,
        vault=vault,
        risk=risk,
        owner_user_id=owner_user_id,
        privileged=privileged,
        rotation_state=rotation_state,
        expiring_within_days=expiring_within_days,
    )


def access_log_filters(
    action: Annotated[
        SecretAccessAction | None,
        Query(description="reveal, rotate, use, disable, enable, create, update or revoke"),
    ] = None,
    success: Annotated[
        bool | None, Query(description="Only successful, or only refused, attempts")
    ] = None,
) -> AccessLogFilters:
    """Filters over one credential's access history."""
    return AccessLogFilters(action=action, success=success)


SecretFilterQuery = Annotated[SecretFilters, Depends(secret_filters)]
AccessLogFilterQuery = Annotated[AccessLogFilters, Depends(access_log_filters)]
ListQueryParams = Annotated[ListParams, Depends(list_params)]


# ---------------------------------------------------------------------------
# Fixed paths first: they would otherwise be swallowed by /{secret_id}.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=SecretsSummary, summary="Secret KPI summary")
async def get_summary(principal: CurrentPrincipal, session: Db) -> SecretsSummary:
    """The KPI cards above the vault, plus the by-type breakdown for the donut.

    Totals, distinct vaults, credentials expiring inside 30 days, overdue
    rotations, the compliance score and the 30-day privileged-access count are
    all SQL aggregates, so the panel costs the same on a vault of ten
    credentials as on one of ten thousand.

    Expiring, overdue and the compliance figures are taken over the credentials
    in service (`in_service`): a disabled or revoked one cannot be rotated, so
    it is not scored. `privileged_access_30d` counts successful reveals and
    rotations of privileged credentials, not metadata edits or refusals.
    """
    return await service.summarise(session, principal)


@router.get("/export", summary="Export secret metadata as CSV")
async def export_secrets(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQueryParams,
    filters: SecretFilterQuery,
    request: Request,
) -> StreamingResponse:
    """Download the current view as CSV.

    Metadata only: names, vaults, owners, rotation and compliance state. No
    ciphertext, no plaintext, not even the masked hint leaves in a file. The
    export itself is written to the audit trail.
    """
    csv_text = await service.export_csv(session, principal, params, filters, request=request)
    filename = f"secrets-metadata-{dt.datetime.now(dt.UTC):%Y%m%d}.csv"
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get("", response_model=Page[SecretRead], summary="List secrets")
async def list_secrets(
    principal: CurrentPrincipal,
    session: Db,
    params: ListQueryParams,
    filters: SecretFilterQuery,
) -> Page[SecretRead]:
    """One page of the workspace's vault, newest first by default.

    Free-text search covers the name, type, vault, environment, external
    reference and the owner's name or email; `sort` accepts any column key the
    table shows. Rows carry the masked `display_hint` and never the value.
    """
    rows, total = await service.list_secrets(session, principal, params, filters)
    return Page.build(rows, total, params.page, params.page_size)


@router.post(
    "",
    response_model=SecretRead,
    status_code=status.HTTP_201_CREATED,
    summary="Store a secret",
)
async def create_secret(
    principal: CurrentPrincipal,
    session: Db,
    payload: SecretCreate,
    request: Request,
) -> SecretRead:
    """Store a credential in the vault.

    Supply `value` and it is encrypted before it touches a row, or supply
    `vault_reference` alone to register a credential this control plane only
    points at. The value is never returned by this or any other list or detail
    endpoint. Requires the admin role.
    """
    return await service.create_secret(session, principal, payload, request=request)


# ---------------------------------------------------------------------------
# Single secret
# ---------------------------------------------------------------------------


@router.get("/{secret_id}", response_model=SecretRead, summary="Get a secret")
async def get_secret(
    principal: CurrentPrincipal, session: Db, secret_id: str
) -> SecretRead:
    """One credential's metadata. A secret in another workspace answers 404, not 403."""
    return await service.get_secret(session, principal, secret_id)


@router.patch("/{secret_id}", response_model=SecretRead, summary="Update a secret")
async def update_secret(
    principal: CurrentPrincipal,
    session: Db,
    secret_id: str,
    payload: SecretUpdate,
    request: Request,
) -> SecretRead:
    """Partially update a credential's metadata.

    The material cannot be changed here — rotate it instead. Send
    `expected_updated_at` to make the write conditional: if the row moved since
    you read it the request is refused with 409. Requires the admin role.

    `status` is held to the same state machine as the verbs. Setting `Revoked`
    revokes the credential and is recorded as such; moving a revoked credential
    anywhere else is refused with 412, exactly as `/enable` refuses it; and
    `Disabled` is entered and left through `/disable` and `/enable` only (422
    here), because those record the reason. `owner_user_id` must name a member
    of this workspace, and a field that cannot be empty — name, type, vault,
    status, risk, privileged — answers 422 to an explicit null.
    """
    return await service.update_secret(
        session, principal, secret_id, payload, request=request
    )


@router.delete(
    "/{secret_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a secret",
)
async def delete_secret(
    principal: CurrentPrincipal, session: Db, secret_id: str, request: Request
) -> None:
    """Remove a credential and its access history.

    Refused with 412 while the credential is still in service: disable or
    revoke it first, so retirement is a deliberate two-step act. The audit
    trail survives the deletion. Requires the admin role.
    """
    await service.delete_secret(session, principal, secret_id, request=request)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


@router.post(
    "/{secret_id}/reveal",
    response_model=SecretRevealResult,
    summary="Reveal a secret value",
)
async def reveal_secret(
    principal: CurrentPrincipal,
    session: Db,
    secret_id: str,
    payload: SecretRevealRequest,
    request: Request,
) -> SecretRevealResult:
    """Return the plaintext once, against a written justification.

    Requires the admin role and a non-empty `justification`, which is stored
    against your name. Disabled and revoked credentials are refused with 412,
    as are entries the control plane only points at. Every outcome — allowed or
    refused — is written to the credential's access log and to the audit trail.
    """
    return await service.reveal_secret(
        session, principal, secret_id, payload, request=request
    )


@router.post(
    "/{secret_id}/rotate",
    response_model=SecretRotateResult,
    summary="Rotate a secret",
)
async def rotate_secret(
    principal: CurrentPrincipal,
    session: Db,
    secret_id: str,
    request: Request,
    payload: SecretRotateRequest | None = None,
) -> SecretRotateResult:
    """Replace the material and restart the rotation clock.

    Send `value` when the credential was reissued upstream, or omit it and a
    fresh one is generated — a generated value is returned exactly once, in
    this response, while a supplied one is never echoed back. `next_rotation_at`
    is recomputed from the rotation period. Requires the admin role.

    A credential the control plane only points at (`has_material` false) is
    never minted into: omit `value` there and the call records a rotation you
    performed in its own vault — `recorded_upstream` is true, no value comes
    back, and the row stays a reference.
    """
    return await service.rotate_secret(
        session, principal, secret_id, payload or SecretRotateRequest(), request=request
    )


@router.post(
    "/{secret_id}/disable",
    response_model=SecretRead,
    summary="Disable a secret",
)
async def disable_secret(
    principal: CurrentPrincipal,
    session: Db,
    secret_id: str,
    request: Request,
    payload: SecretDisableRequest | None = None,
) -> SecretRead:
    """Take a credential out of service; linked systems lose access immediately.

    An optional `reason` is recorded on the access-log row. Disabling an
    already disabled or revoked credential answers 409. Requires the admin role.
    """
    return await service.disable_secret(
        session, principal, secret_id, payload, request=request
    )


@router.post(
    "/{secret_id}/enable",
    response_model=SecretRead,
    summary="Enable a secret",
)
async def enable_secret(
    principal: CurrentPrincipal, session: Db, secret_id: str, request: Request
) -> SecretRead:
    """Return a disabled credential to service.

    A revoked credential is refused with 412: revocation is final, and the
    answer to needing it back is a replacement. Requires the admin role.
    """
    return await service.enable_secret(session, principal, secret_id, request=request)


@router.get(
    "/{secret_id}/access-log",
    response_model=Page[SecretAccessLogRead],
    summary="List a secret's access history",
)
async def list_access_log(
    principal: CurrentPrincipal,
    session: Db,
    secret_id: str,
    params: ListQueryParams,
    filters: AccessLogFilterQuery,
) -> Page[SecretAccessLogRead]:
    """Every reveal, rotation and administrative change to one credential.

    Newest first, refused attempts included — they are what the inspector's
    failed-access counter is reading. The rows are append-only: nothing in the
    API updates or deletes them.
    """
    rows, total = await service.list_access_log(
        session, principal, secret_id, params, filters
    )
    return Page.build(
        [SecretAccessLogRead.model_validate(row) for row in rows],
        total,
        params.page,
        params.page_size,
    )
