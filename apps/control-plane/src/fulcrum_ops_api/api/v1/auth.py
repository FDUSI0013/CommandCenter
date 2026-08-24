"""Session routes: the front door for the console and for every SDK.

Eleven endpoints cover the whole signed-in lifecycle — sign in, probe the
session, switch workspace, edit your own profile and notification preferences,
rotate your password, sign out.

The session token is delivered as an httpOnly cookie rather than a body field
the browser has to store: script on the page cannot read it, so an injected
script cannot exfiltrate it. The same token is also accepted as a bearer header
for non-browser callers, which is what makes one route serve both.

Handlers here only parse, delegate and set cookies. Credential checking,
lockout, audit writes and the envelope itself all live in ``services.identity``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from ...core.config import settings
from ...core.errors import AppError
from ...schemas.identity import (
    ChangePasswordRequest,
    LoginRequest,
    PreferencesUpdate,
    ProfileUpdate,
    SessionEnvelope,
    SwitchWorkspaceRequest,
    UserPreferences,
    UserProfile,
    WorkspaceOption,
)
from ...services import identity as service
from ..common import ActionResult
from ..deps import (
    API_KEY_HEADER,
    SESSION_COOKIE,
    WORKSPACE_HEADER,
    CurrentPrincipal,
    Db,
    Principal,
    get_principal,
)

router = APIRouter(prefix="/auth", tags=["Identity"])


# ---------------------------------------------------------------------------
# Cookie handling
# ---------------------------------------------------------------------------


def _set_session_cookie(response: Response, token: str) -> None:
    """Install the session cookie.

    ``httponly`` keeps it away from page script, ``samesite=lax`` stops another
    origin's form post from riding it, and ``secure`` is dropped only on local
    development, where there is no TLS to attach it to.
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=not settings.is_local,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        secure=not settings.is_local,
        samesite="lax",
        path="/",
    )


async def _optional_principal(request: Request, session: Db) -> Principal | None:
    """Resolve the caller if there is one, without failing when there is not.

    Sign-out has to work from an expired session — otherwise the one action a
    stuck client needs is the one it cannot perform.
    """
    try:
        return await get_principal(
            request,
            session,
            authorization=request.headers.get("authorization"),
            x_fulcrum_api_key=request.headers.get(API_KEY_HEADER),
            x_fulcrum_workspace=request.headers.get(WORKSPACE_HEADER),
        )
    except AppError:
        return None


# ---------------------------------------------------------------------------
# Sign in / sign out
# ---------------------------------------------------------------------------


@router.post("/login", response_model=SessionEnvelope, summary="Sign in")
async def login(
    session: Db, payload: LoginRequest, response: Response, request: Request
) -> SessionEnvelope:
    """Exchange an email and password for a session.

    Every failure — unknown address, wrong password, deactivated account, no
    workspace — answers with the same message and the same code, so this route
    cannot be used to discover who has an account. Repeated failures lock the
    account for a cooling-off period; a correct password on a locked account is
    told so, because the caller already holds the secret.
    """
    token, _expires_at, envelope = await service.login(
        session, email=payload.email, password=payload.password, request=request
    )
    _set_session_cookie(response, token)
    return envelope


@router.post("/logout", response_model=ActionResult, summary="Sign out")
async def logout(session: Db, response: Response, request: Request) -> ActionResult:
    """Close the session on this device and clear the cookie.

    Succeeds even when the presented session has already expired: the cookie is
    removed either way, and the audit row is written when there was still a
    principal to attribute it to.
    """
    principal = await _optional_principal(request, session)
    await service.logout(session, principal, request=request)
    _clear_session_cookie(response)
    return ActionResult(message="Signed out.")


@router.get("/session", response_model=SessionEnvelope, summary="Current session")
async def get_session(principal: CurrentPrincipal, session: Db) -> SessionEnvelope:
    """Who is calling, in which workspace, with what role and entitlements.

    Answers 401 when the caller is anonymous, which is how the console decides
    whether to render the shell or the sign-in form. An API-key caller gets the
    same envelope with ``user`` null and ``api_key`` populated, so an SDK can
    verify its own credential.
    """
    return await service.current_session(session, principal)


@router.get(
    "/me",
    response_model=SessionEnvelope,
    summary="Current session (alias)",
)
async def get_me(principal: CurrentPrincipal, session: Db) -> SessionEnvelope:
    """Alias of `GET /auth/session`, kept because the console client asks here."""
    return await service.current_session(session, principal)


@router.get(
    "/workspaces",
    response_model=list[WorkspaceOption],
    summary="Workspaces you can switch to",
)
async def list_workspaces(principal: CurrentPrincipal, session: Db) -> list[WorkspaceOption]:
    """The workspace switcher's options, with the current one flagged."""
    return await service.list_my_workspaces(session, principal)


@router.post(
    "/switch-workspace",
    response_model=SessionEnvelope,
    summary="Switch workspace",
)
async def switch_workspace(
    principal: CurrentPrincipal,
    session: Db,
    payload: SwitchWorkspaceRequest,
    response: Response,
    request: Request,
) -> SessionEnvelope:
    """Re-issue the session against another workspace you belong to.

    A slug or an id is accepted. A workspace you are not a member of answers
    404 rather than 403 — the switcher must not double as a tenant directory.
    The role in the new envelope is the role you hold *there*, which may differ.
    """
    token, _expires_at, envelope = await service.switch_workspace(
        session, principal, payload.workspace, request=request
    )
    _set_session_cookie(response, token)
    return envelope


@router.post(
    "/workspace",
    response_model=SessionEnvelope,
    summary="Switch workspace (alias)",
)
async def switch_workspace_alias(
    principal: CurrentPrincipal,
    session: Db,
    payload: SwitchWorkspaceRequest,
    response: Response,
    request: Request,
) -> SessionEnvelope:
    """Alias of `POST /auth/switch-workspace`, kept for the console client."""
    token, _expires_at, envelope = await service.switch_workspace(
        session, principal, payload.workspace, request=request
    )
    _set_session_cookie(response, token)
    return envelope


@router.post(
    "/change-password",
    response_model=ActionResult,
    summary="Change your password",
)
async def change_password(
    principal: CurrentPrincipal,
    session: Db,
    payload: ChangePasswordRequest,
    response: Response,
    request: Request,
) -> ActionResult:
    """Rotate your own password and re-issue the session cookie.

    The current password is always required, so a stolen cookie cannot be
    turned into permanent account control. API-key callers are refused: a key
    is not a person and has no password.
    """
    token, expires_at = await service.change_password(
        session,
        principal,
        current_password=payload.current_password,
        new_password=payload.new_password,
        request=request,
    )
    _set_session_cookie(response, token)
    return ActionResult(
        message="Password changed. This device stays signed in.",
        entity_id=principal.user_id,
        data={"session_expires_at": expires_at.isoformat()},
    )


# ---------------------------------------------------------------------------
# Profile & Preferences / Notification Settings
# ---------------------------------------------------------------------------


@router.get("/profile", response_model=UserProfile, summary="Your profile")
async def get_profile(principal: CurrentPrincipal, session: Db) -> UserProfile:
    """Your own record and preference document, as the account menu opens it."""
    return await service.get_profile(session, principal)


@router.patch("/profile", response_model=UserProfile, summary="Update your profile")
async def update_profile(
    principal: CurrentPrincipal, session: Db, payload: ProfileUpdate, request: Request
) -> UserProfile:
    """Edit your display name, job title, team or avatar initials.

    Email and role are deliberately not self-service: one identifies the
    account, the other is granted by an admin.
    """
    return await service.update_profile(session, principal, payload, request=request)


@router.get(
    "/preferences",
    response_model=UserPreferences,
    summary="Your notification and console preferences",
)
async def get_preferences(principal: CurrentPrincipal, session: Db) -> UserPreferences:
    """What the Notification Settings and Preferences panels render."""
    return await service.get_preferences(session, principal)


@router.patch(
    "/preferences",
    response_model=UserPreferences,
    summary="Update your preferences",
)
async def update_preferences(
    principal: CurrentPrincipal, session: Db, payload: PreferencesUpdate, request: Request
) -> UserPreferences:
    """Merge a preference change one section at a time.

    Sending only `notifications` leaves the console preferences untouched, which
    is what two independent panels writing the same document require.
    Preferences are held per person *per workspace*, so being paged for
    production in one tenant does not page you for experiments in another.
    """
    return await service.update_preferences(session, principal, payload, request=request)


@router.get(
    "/status",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cheap authentication probe",
)
async def session_status(principal: CurrentPrincipal) -> None:
    """204 while the session is valid, 401 once it is not.

    Used by the console's idle timer, which needs the answer without paying for
    the workspace list and entitlement lookup that the full envelope costs.
    """
    return None
