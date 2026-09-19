"""Fixtures every test in this suite is built on.

Three things are wired up here and nothing else is:

* **A database per test.** SQLite on disk, schema created straight from
  ``Base.metadata`` rather than by replaying migrations, so a model change
  shows up as a failing test rather than as a migration that was never written.
  The application's own engine and sessionmaker globals are pointed at it, so
  request handling, background tasks and the test's own probes all read one
  database.
* **The telemetry engine, in process.** :class:`~engine_double.EngineDouble` is
  mounted behind ``httpx.ASGITransport`` and handed to a real
  :class:`EngineClient`, which is then published exactly the way application
  startup publishes it. No socket is opened, and every layer between the route
  and the wire is the production one.
* **Callers.** ``client`` is anonymous; ``as_role`` mints a session for a user
  holding any role; ``key_client`` authenticates with a real API key. Session
  tokens are signed by the application's own signer, so the whole
  authentication path runs even when a test did not go through ``/auth/login``.

Nothing here stubs a service, patches a method or overrides a FastAPI
dependency. A test that passes did so through the real request path.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fulcrum_ops_api.core.config import settings

# The console is a separate deliverable; serving it would mount StaticFiles at
# the root of the app under test. Cleared before the app is ever built.
settings.static_dir = None

# The metric library's package name is deployment configuration (the vendor's
# name never ships in this repository). The suite sets a stand-in so the
# guardrail mirror path runs; the double validates shape, not importability.
settings.engine_metric_library = "enginelib"


def _warm_password_backend() -> None:
    """Load passlib's argon2 backend once, outside any test.

    The first hash of the process makes passlib read ``argon2.__version__``,
    which argon2-cffi has deprecated. The warning is emitted exactly once, when
    the backend is selected — but ``filterwarnings = ["error::DeprecationWarning"]``
    turns it into a failure of whichever test happened to hash first. Paying for
    it here keeps that accident out of the results while leaving the underlying
    dependency problem visible in the notes rather than silently filtered
    everywhere.
    """
    import warnings

    from fulcrum_ops_api.core.security import hash_password

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        hash_password("warm the hashing backend")


_warm_password_backend()

from engine_double import EngineDouble  # noqa: E402
from factories import Factory  # noqa: E402
from fulcrum_ops_api.api.deps import Principal  # noqa: E402
from fulcrum_ops_api.core import security  # noqa: E402
from fulcrum_ops_api.db import session as db_session  # noqa: E402
from fulcrum_ops_api.db.base import Base  # noqa: E402
from fulcrum_ops_api.engine import EngineClient, set_engine_client  # noqa: E402
from fulcrum_ops_api.main import create_app  # noqa: E402
from fulcrum_ops_api.models import identity as identity_models  # noqa: E402
from fulcrum_ops_api.models.identity import Membership, Role, User, Workspace  # noqa: E402

#: Base URL the engine double is addressed by. It is never resolved — the ASGI
#: transport short-circuits the request — but httpx requires an absolute URL.
ENGINE_BASE_URL = "http://telemetry-engine.internal"

#: Base URL the application under test is addressed by, for the same reason.
APP_BASE_URL = "http://control-plane.test"


@pytest.fixture(autouse=True)
def _fresh_rate_windows():
    """The limiter's windows are process-global; tests must not share them."""
    from fulcrum_ops_api.core import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture(autouse=True)
def _no_run_screen_memory(monkeypatch):
    """The run screens' short-lived caches are off unless a test asks for them.

    A test writes telemetry and reads it back in the same breath; it has to see
    what it wrote, not what was remembered a second ago. The tests that are
    *about* the caches switch them back on for themselves.
    """
    from fulcrum_ops_api.core.config import settings
    from fulcrum_ops_api.services import telemetry_cache

    monkeypatch.setattr(settings, "runs_activity_cache_seconds", 0.0)
    monkeypatch.setattr(settings, "runs_scan_cache_seconds", 0.0)
    monkeypatch.setattr(settings, "runs_summary_cache_seconds", 0.0)
    monkeypatch.setattr(settings, "agent_stats_cache_seconds", 0.0)
    telemetry_cache.reset()
    yield
    telemetry_cache.reset()


@pytest.fixture(autouse=True)
def _no_metrics_memory(monkeypatch):
    """The metrics rollups' shared measurements are off for the same reason.

    With them on, a test that takes the store down would still be answered from
    the measurement made a moment earlier. ``test_audit_metrics`` switches the
    memory back on for the tests that are about it.
    """
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "metrics_cache_seconds", 0.0)


@pytest.fixture(autouse=True)
def _no_configuration_usage_memory(monkeypatch):
    """The Configuration Center's Usage tab remembers nothing, for the same reason."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "configuration_usage_cache_seconds", 0.0)


@pytest.fixture(autouse=True)
def _no_audit_verify_memory(monkeypatch):
    """A chain replay is never served from memory: a test that tampers with a row
    and verifies again has to see the break, not the answer from before it."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "audit_verify_cache_seconds", 0.0)


@pytest.fixture(autouse=True)
def _no_knowledge_scan_memory(monkeypatch):
    """The knowledge screen's shared retrieval scan is off, for the same reason:
    a test that takes the store down must be refused, not answered from the scan
    made a moment earlier. ``test_audit_knowledge`` switches it back on."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "knowledge_scan_cache_seconds", 0.0)


@pytest.fixture(autouse=True)
def _no_prompt_stats_memory(monkeypatch):
    """Prompt Studio's per-agent run counters are read afresh, for the same reason:
    two tests name their agent alike, and the second must not be told the first's
    runs. ``test_audit_prompts`` switches the memory back on."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "prompt_stats_cache_seconds", 0.0)


@pytest.fixture(autouse=True)
def _no_memory_counts_memory(monkeypatch):
    """Memory & State counts its threads afresh, for the same reason: a test that
    takes the store down must be refused, not told the count from a moment ago.
    ``test_audit_memory`` switches the memory back on."""
    from fulcrum_ops_api.core.config import settings

    monkeypatch.setattr(settings, "memory_counts_cache_seconds", 0.0)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine(tmp_path) -> AsyncIterator[Any]:
    """One SQLite database per test, with the schema built from the models."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'control-plane.db').as_posix()}"
    engine = db_session.create_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    # Point the application's globals at this database. Handlers resolve their
    # session through get_session(), and background work through
    # get_sessionmaker(); both read these.
    db_session._engine = engine
    db_session._sessionmaker = None
    try:
        yield engine
    finally:
        db_session._engine = None
        db_session._sessionmaker = None
        await engine.dispose()


@pytest.fixture
def sessionmaker(db_engine) -> async_sessionmaker[AsyncSession]:
    return db_session.get_sessionmaker()


class DatabaseProbe:
    """Read and write the database directly, outside the request path.

    Every call opens its own short-lived session. That is deliberate: a session
    held open across an HTTP call keeps a read transaction, and would answer
    with the snapshot from before the request committed.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    def session(self) -> AsyncSession:
        return self._factory()

    async def scalars(self, statement) -> list[Any]:
        async with self._factory() as session:
            return list((await session.execute(statement)).scalars().all())

    async def scalar(self, statement) -> Any:
        async with self._factory() as session:
            return (await session.execute(statement)).scalars().first()

    async def rows(self, statement) -> list[Any]:
        async with self._factory() as session:
            return list((await session.execute(statement)).all())

    async def get(self, model: type, primary_key: str) -> Any:
        async with self._factory() as session:
            return await session.get(model, primary_key)

    async def count(self, model: type, *where) -> int:
        statement = select(func.count()).select_from(model)
        if where:
            statement = statement.where(*where)
        async with self._factory() as session:
            return int((await session.execute(statement)).scalar_one())

    async def execute(self, statement) -> Any:
        """Run a statement and commit it — used to tamper with rows on purpose."""
        async with self._factory() as session:
            result = await session.execute(statement)
            await session.commit()
            return result


@pytest.fixture
def db(sessionmaker) -> DatabaseProbe:
    return DatabaseProbe(sessionmaker)


@pytest.fixture
def factory(sessionmaker) -> Factory:
    """Build rows without going through HTTP."""
    return Factory(sessionmaker)


# ---------------------------------------------------------------------------
# Telemetry engine
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> EngineDouble:
    """The in-process telemetry engine this test's application talks to."""
    return EngineDouble()


@pytest.fixture
async def engine_client(engine: EngineDouble) -> AsyncIterator[EngineClient]:
    """A real adapter pointed at the double, published process-wide.

    Retries are off by default so a deliberate outage fails in the time a test
    is willing to wait; ``engine_client_with_retries`` covers the retry path
    itself.
    """
    client = EngineClient(
        base_url=ENGINE_BASE_URL,
        workspace="test",
        api_key=None,
        retries=0,
        transport=httpx.ASGITransport(app=engine),
    )
    set_engine_client(client)
    try:
        yield client
    finally:
        set_engine_client(None)
        await client.aclose()


@pytest.fixture
async def engine_client_with_retries(engine: EngineDouble) -> AsyncIterator[EngineClient]:
    """The same adapter with its production retry count, for retry assertions."""
    client = EngineClient(
        base_url=ENGINE_BASE_URL,
        workspace="test",
        api_key=None,
        retries=2,
        transport=httpx.ASGITransport(app=engine),
    )
    set_engine_client(client)
    try:
        yield client
    finally:
        set_engine_client(None)
        await client.aclose()


# ---------------------------------------------------------------------------
# Application and callers
# ---------------------------------------------------------------------------


@pytest.fixture
def app(db_engine, engine_client):
    """The application under test, built the way ``main`` builds it.

    ``lifespan`` is not run: the ASGI transport does not send lifespan events,
    and startup's only jobs — opening a connection pool to the engine and
    probing it — are already done by the fixtures above.
    """
    return create_app()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    """An anonymous caller."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        yield http


@pytest.fixture
async def workspace(factory: Factory) -> Workspace:
    """The workspace almost every test acts inside."""
    return await factory.workspace(name="Northwind", slug="northwind")


@pytest.fixture
async def other_workspace(factory: Factory) -> Workspace:
    """A second tenant. It must never see the first's rows."""
    return await factory.workspace(name="Contoso", slug="contoso")


@pytest.fixture
async def owner(factory: Factory, workspace: Workspace) -> User:
    return await factory.user(
        workspace, email="owner@northwind.test", full_name="Ada Owner", role=Role.OWNER
    )


@pytest.fixture
async def admin(factory: Factory, workspace: Workspace) -> User:
    return await factory.user(
        workspace, email="admin@northwind.test", full_name="Ben Admin", role=Role.ADMIN
    )


@pytest.fixture
async def operator(factory: Factory, workspace: Workspace) -> User:
    return await factory.user(
        workspace, email="operator@northwind.test", full_name="Cal Operator",
        role=Role.OPERATOR,
    )


@pytest.fixture
async def approver(factory: Factory, workspace: Workspace) -> User:
    return await factory.user(
        workspace, email="approver@northwind.test", full_name="Dee Approver",
        role=Role.APPROVER,
    )


@pytest.fixture
async def member(factory: Factory, workspace: Workspace) -> User:
    return await factory.user(
        workspace, email="member@northwind.test", full_name="Eli Member", role=Role.MEMBER
    )


@pytest.fixture
async def viewer(factory: Factory, workspace: Workspace) -> User:
    return await factory.user(
        workspace, email="viewer@northwind.test", full_name="Fay Viewer", role=Role.VIEWER
    )


def session_token(user: User, workspace: Workspace, role: Role) -> str:
    """Sign a session the application will accept, without paying for a login."""
    return security.issue_session_token(
        user_id=user.id, workspace_id=workspace.id, role=role.value
    )


def authorise(http: httpx.AsyncClient, user: User, workspace: Workspace, role: Role) -> None:
    http.headers["Authorization"] = f"Bearer {session_token(user, workspace, role)}"


@pytest.fixture
async def as_role(
    app, factory: Factory, workspace: Workspace
) -> AsyncIterator[Callable[..., Any]]:
    """Open a client signed in as a user holding ``role``.

    Usage: ``async with as_role(Role.ADMIN) as http: ...``. A user is created
    for the role on first use, so a test names the permission it needs rather
    than the person who happens to hold it.
    """
    opened: list[httpx.AsyncClient] = []

    class _Opener:
        def __init__(self, role: Role, target: Workspace | None = None) -> None:
            self._role = role
            self._workspace = target or workspace

        async def __aenter__(self) -> httpx.AsyncClient:
            user = await factory.user(
                self._workspace,
                email=f"{self._role.value}-{len(opened)}@{self._workspace.slug}.test",
                full_name=f"{self._role.value.title()} Person",
                role=self._role,
            )
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=APP_BASE_URL
            )
            authorise(http, user, self._workspace, self._role)
            opened.append(http)
            return http

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def opener(role: Role, target: Workspace | None = None) -> _Opener:
        return _Opener(role, target)

    try:
        yield opener
    finally:
        for http in opened:
            await http.aclose()


@pytest.fixture
async def admin_client(app, admin: User, workspace: Workspace) -> AsyncIterator[httpx.AsyncClient]:
    """The caller most governance tests use: an admin of the main workspace."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        authorise(http, admin, workspace, Role.ADMIN)
        yield http


@pytest.fixture
async def owner_client(app, owner: User, workspace: Workspace) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        authorise(http, owner, workspace, Role.OWNER)
        yield http


@pytest.fixture
async def viewer_client(
    app, viewer: User, workspace: Workspace
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        authorise(http, viewer, workspace, Role.VIEWER)
        yield http


@pytest.fixture
async def ingest_key(factory: Factory, workspace: Workspace) -> tuple[str, Any]:
    """An API key with the ingest scope, plus the row it was minted from."""
    return await factory.api_key(workspace, name="Fleet key", scopes=["ingest", "read"])


@pytest.fixture
async def ingest_client(app, ingest_key) -> AsyncIterator[httpx.AsyncClient]:
    token, _row = ingest_key
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        yield http


@pytest.fixture
async def registrar_client(
    app, factory: Factory, workspace: Workspace
) -> AsyncIterator[httpx.AsyncClient]:
    """An ingest key that may also register an agent it has never seen."""
    token, _row = await factory.api_key(
        workspace, name="Bootstrap key", scopes=["ingest", "read", "agents:write"]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=APP_BASE_URL) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        yield http


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def error_code(response: httpx.Response) -> str | None:
    """The stable machine-readable code out of the API's error envelope."""
    try:
        return response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def principal_for(workspace: Workspace, user: User, role: Role) -> Principal:
    """The principal a service call acts as when a test bypasses HTTP."""
    return Principal(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        engine_workspace=workspace.engine_workspace,
        role=role,
        kind="user",
        user_id=user.id,
        email=user.email,
        display_name=user.full_name,
    )


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


__all__ = [
    "APP_BASE_URL",
    "ENGINE_BASE_URL",
    "DatabaseProbe",
    "authorise",
    "error_code",
    "principal_for",
    "session_token",
    "utcnow",
    "Membership",
    "Role",
    "User",
    "Workspace",
    "identity_models",
]
