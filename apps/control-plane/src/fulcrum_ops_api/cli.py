"""Operator command line for the FD AI Command Center server.

Installed as ``fulcrum-ops-api``. Five commands cover everything that has to
happen outside a request: standing a new deployment up, running the server,
adding people, minting credentials, and generating the key that protects secret
material at rest.

Secrets are handled the way the API handles them. A password is never accepted
as a command-line argument — it would land in shell history and in the process
table — so it is read from the environment or prompted for, and a minted API key
is printed exactly once with a warning, because the database only ever holds its
hash.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Annotated

import typer
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from .core.config import settings
from .core.errors import AppError
from .core.security import generate_encryption_key
from .db import base as db_base
from .db import session as db_session
from .models import identity as identity_models
from .models.identity import Role
from .services import identity as service

app = typer.Typer(
    name="fulcrum-ops-api",
    help="Operate the FD AI Command Center server.",
    no_args_is_help=True,
    add_completion=False,
)

#: Slugs address a tenant in URLs, SDK configuration and the telemetry engine's
#: namespace, so the accepted shape is deliberately narrow.
SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

#: The table whose absence means the schema was never created.
SENTINEL_TABLE = identity_models.Workspace.__tablename__


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:63].rstrip("-")


async def _schema_present() -> bool:
    engine = db_session.get_engine()
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync: sa_inspect(sync).has_table(SENTINEL_TABLE))


async def _create_schema() -> None:
    engine = db_session.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_base.Base.metadata.create_all)


async def _ensure_schema(init_db: bool) -> None:
    if await _schema_present():
        return
    if not init_db:
        _fail(
            "the database has no schema. Run the Alembic migrations, or pass "
            "--init-db to create the tables directly (development only)."
        )
    await _create_schema()
    typer.secho("created database schema", fg=typer.colors.CYAN)


def _run(coro) -> None:  # noqa: ANN001 - a coroutine of any result type
    """Run one async command to completion and close the engine behind it."""

    async def _wrapped() -> None:
        try:
            await coro
        finally:
            await db_session.dispose()

    try:
        asyncio.run(_wrapped())
    except AppError as exc:
        _fail(exc.message)


def _session() -> AsyncSession:
    return db_session.get_sessionmaker()()


def _read_password(supplied: str | None, *, prompt: str) -> str:
    """Take the password from settings or prompt for it. Never from argv."""
    if supplied:
        return supplied
    if not sys.stdin.isatty():
        _fail(
            "no password available: set it in the environment "
            "(FULCRUM_OPS_BOOTSTRAP_PASSWORD) or run this on a terminal."
        )
    return typer.prompt(prompt, hide_input=True, confirmation_prompt=True)


def _print_key(token: str, *, name: str) -> None:
    typer.secho(f"\nAPI key '{name}':", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  {token}")
    typer.secho(
        "  Copy it now. The server stores only a hash and cannot show it again.",
        fg=typer.colors.YELLOW,
    )


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


@app.command()
def bootstrap(
    workspace_name: Annotated[
        str, typer.Option("--workspace", help="Display name of the first workspace")
    ] = "FD AI Command Center",
    slug: Annotated[
        str | None, typer.Option("--slug", help="URL slug; derived from the name when omitted")
    ] = None,
    email: Annotated[
        str | None, typer.Option("--email", help="Owner's email; defaults to the configured one")
    ] = None,
    full_name: Annotated[
        str, typer.Option("--name", help="Owner's display name")
    ] = "Platform Owner",
    key_name: Annotated[
        str, typer.Option("--key-name", help="Name of the admin API key that is minted")
    ] = "Bootstrap Admin Key",
    init_db: Annotated[
        bool, typer.Option("--init-db", help="Create the tables directly (development only)")
    ] = False,
) -> None:
    """Stand up the first workspace, its owner and an admin API key.

    Idempotent in the sense that it refuses rather than duplicates: a slug or an
    email that already exists stops the command, so running it twice cannot
    silently mint a second set of credentials.
    """
    target_slug = slug or _slugify(workspace_name)
    if not SLUG_PATTERN.match(target_slug):
        _fail(f"'{target_slug}' is not a valid slug (lower-case letters, digits and hyphens)")

    owner_email = (email or settings.bootstrap_email or "").strip().lower()
    if not owner_email:
        _fail("no owner email: pass --email or set FULCRUM_OPS_BOOTSTRAP_EMAIL")

    secret = _read_password(settings.bootstrap_password, prompt="Owner password")

    async def _bootstrap() -> None:
        await _ensure_schema(init_db)
        async with _session() as session:
            workspace = await service.provision_workspace(
                session, name=workspace_name, slug=target_slug
            )
            user, _membership = await service.provision_user(
                session,
                workspace=workspace,
                email=owner_email,
                full_name=full_name,
                password=secret,
                role=Role.OWNER,
                job_title="Platform Owner",
            )
            key, minted = await service.provision_api_key(
                session,
                workspace=workspace,
                name=key_name,
                scopes=["ingest", "read", "admin"],
                created_by_user_id=user.id,
            )
            await session.commit()

            typer.secho(
                f"workspace '{workspace.name}' ({workspace.slug}) created",
                fg=typer.colors.GREEN,
            )
            typer.echo(f"  owner:   {user.email} ({Role.OWNER.value})")
            typer.echo(f"  key id:  {key.id}")
            _print_key(minted.token, name=key.name)

    _run(_bootstrap())


# ---------------------------------------------------------------------------
# create-user
# ---------------------------------------------------------------------------


@app.command("create-user")
def create_user(
    email: Annotated[str, typer.Option("--email", help="Email address to sign in with")],
    full_name: Annotated[str, typer.Option("--name", help="Display name")],
    workspace: Annotated[
        str, typer.Option("--workspace", help="Workspace slug to add them to")
    ],
    role: Annotated[
        str, typer.Option("--role", help="Role in that workspace")
    ] = Role.MEMBER.value,
    job_title: Annotated[str | None, typer.Option("--title", help="Job title")] = None,
    team: Annotated[str | None, typer.Option("--team", help="Team name")] = None,
    no_password: Annotated[
        bool,
        typer.Option(
            "--no-password",
            help="Create the account without a password; it cannot sign in until one is set",
        ),
    ] = False,
) -> None:
    """Add a person to a workspace, creating their account if it is new.

    An address the platform already knows is joined to the workspace rather
    than duplicated, and keeps the password it already has.
    """
    normalised = email.strip().lower()
    try:
        target_role = Role(role.strip().lower())
    except ValueError:
        _fail(f"'{role}' is not a role. Valid roles: {', '.join(r.value for r in Role)}")
        return

    secret = None if no_password else _read_password(None, prompt=f"Password for {normalised}")

    async def _create() -> None:
        from sqlalchemy import select

        async with _session() as session:
            found = (
                await session.execute(
                    select(identity_models.Workspace).where(
                        identity_models.Workspace.slug == workspace
                    )
                )
            ).scalar_one_or_none()
            if found is None:
                _fail(f"no workspace with slug '{workspace}'")
                return

            user, membership = await service.provision_user(
                session,
                workspace=found,
                email=normalised,
                full_name=full_name,
                password=secret,
                role=target_role,
                job_title=job_title,
                team=team,
            )
            await session.commit()
            typer.secho(
                f"{user.email} added to {found.slug} as {membership.role}",
                fg=typer.colors.GREEN,
            )
            if secret is None:
                typer.secho(
                    "  no password set — the account cannot sign in yet",
                    fg=typer.colors.YELLOW,
                )

    _run(_create())


# ---------------------------------------------------------------------------
# issue-key
# ---------------------------------------------------------------------------


@app.command("issue-key")
def issue_key(
    workspace: Annotated[str, typer.Option("--workspace", help="Workspace slug the key serves")],
    name: Annotated[str, typer.Option("--name", help="What the key is for")],
    scopes: Annotated[
        str, typer.Option("--scopes", help="Comma-separated: ingest, read, admin")
    ] = "ingest,read",
    environment: Annotated[
        str | None, typer.Option("--environment", help="Environment label, e.g. Production")
    ] = None,
    expires_in_days: Annotated[
        int | None, typer.Option("--expires-in-days", min=1, max=730, help="Expiry, in days")
    ] = None,
) -> None:
    """Mint an API key for a workspace and print it once.

    Use this to connect a deployed agent or a CI job. The plaintext exists only
    in this output: the row stores a SHA-256 digest of the secret.
    """
    requested = [s.strip().lower() for s in scopes.split(",") if s.strip()]
    unknown = sorted(set(requested) - {"ingest", "read", "admin"})
    if not requested or unknown:
        _fail(f"invalid scopes {', '.join(unknown) or '(none given)'}; use ingest, read or admin")

    async def _issue() -> None:
        from sqlalchemy import select

        async with _session() as session:
            found = (
                await session.execute(
                    select(identity_models.Workspace).where(
                        identity_models.Workspace.slug == workspace
                    )
                )
            ).scalar_one_or_none()
            if found is None:
                _fail(f"no workspace with slug '{workspace}'")
                return

            key, minted = await service.provision_api_key(
                session,
                workspace=found,
                name=name,
                scopes=requested,
                environment=environment,
                expires_in_days=expires_in_days,
            )
            await session.commit()
            typer.echo(f"key id:  {key.id}")
            typer.echo(f"scopes:  {', '.join(requested)}")
            expiry = f"{key.expires_at:%Y-%m-%d}" if key.expires_at else "never"
            typer.echo(f"expires: {expiry}")
            _print_key(minted.token, name=key.name)

    _run(_issue())


# ---------------------------------------------------------------------------
# generate-encryption-key
# ---------------------------------------------------------------------------


@app.command("generate-encryption-key")
def generate_key() -> None:
    """Print a fresh Fernet key for FULCRUM_OPS_ENCRYPTION_KEY.

    Rotating this key makes previously encrypted secret material unreadable, so
    generate it once per environment and store it in the platform's own vault.
    """
    typer.echo(generate_encryption_key())


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="Port to bind")] = 8080,
    reload: Annotated[bool, typer.Option("--reload", help="Reload on source changes")] = False,
    workers: Annotated[
        int, typer.Option("--workers", min=1, help="Worker processes; ignored with --reload")
    ] = 1,
) -> None:
    """Run the API with uvicorn.

    Behind a TLS terminator such as Caddy, bind the loopback interface and let
    the proxy own the public socket.
    """
    import uvicorn

    uvicorn.run(
        "fulcrum_ops_api.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=None if reload else workers,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def main() -> None:
    """Entry point declared by ``[project.scripts]``."""
    app()


if __name__ == "__main__":
    main()
