"""Stand the control plane up locally on REAL sockets, against the engine double,
seed a workspace, print an admin API key, and serve until killed.

  python local_stack.py <control-plane dir> <api port> <engine port>

Everything between the HTTP socket and the engine adapter is the production code
path: uvicorn, the middleware stack, real dependencies, a real EngineClient over
real TCP. Only the engine itself is the double, and the database is SQLite.
"""
import asyncio
import os
import pathlib
import sys
import tempfile

cp = pathlib.Path(sys.argv[1]).resolve()
api_port, engine_port = int(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, str(cp / "src"))
sys.path.insert(0, str(cp / "tests"))

work = pathlib.Path(tempfile.mkdtemp(prefix="fulcrum-local-"))
os.environ.update(
    FULCRUM_OPS_ENVIRONMENT="local",
    FULCRUM_OPS_DATABASE_URL=f"sqlite+aiosqlite:///{(work / 'cp.db').as_posix()}",
    FULCRUM_OPS_ENGINE_BASE_URL=f"http://127.0.0.1:{engine_port}",
    FULCRUM_OPS_ENGINE_METRIC_LIBRARY="enginelib",
    FULCRUM_OPS_EXPORT_SPOOL_DIR=str(work / "exports"),
    FULCRUM_OPS_SCHEDULER_ENABLED="false",
    FULCRUM_OPS_STATIC_DIR=str((cp / ".." / "web").resolve()),
)

import uvicorn  # noqa: E402
from engine_double import EngineDouble  # noqa: E402
from factories import Factory  # noqa: E402

from fulcrum_ops_api.db import session as db_session  # noqa: E402
from fulcrum_ops_api.db.base import Base  # noqa: E402
from fulcrum_ops_api.main import create_app  # noqa: E402
from fulcrum_ops_api.models.identity import Role  # noqa: E402


async def seed(double: EngineDouble) -> str:
    engine = db_session.get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = Factory(db_session.get_sessionmaker())
    workspace = await factory.workspace(name="Local Smoke", slug="local-smoke")
    await factory.user_in(workspace, role=Role.OWNER, email="owner@local.test") if hasattr(factory, "user_in") else None
    agent = await factory.provisioned_agent(workspace, double, name="Support Bot")
    for n in range(3):
        double.add_trace(
            project_name=agent.engine_project_name,
            name=f"answer question {n}",
            input={"question": "Where is my order?"},
            output={"answer": "It ships tomorrow."},
        )
    # One run with NO spans at all, the shape the UW bridge produced.
    double.add_trace(project_name=agent.engine_project_name, name="underwriting_insight",
                     input={"email_body": "please quote"}, output={"decision": "refer"})
    for make in ("knowledge_source", "quota", "memory_store", "policy", "guardrail", "environment", "configuration"):
        try:
            await getattr(factory, make)(workspace)
        except Exception as exc:  # noqa: BLE001 - seeding is best effort
            print(f"(seed: {make} skipped: {type(exc).__name__}: {exc})", flush=True)
    token, _ = await factory.api_key(workspace, name="smoke", scopes=["ingest", "read", "admin"])
    return token


async def main() -> None:
    double = EngineDouble()
    token = await seed(double)
    (work / "key.txt").write_text(token)
    print(f"KEYFILE {work / 'key.txt'}", flush=True)
    servers = [
        uvicorn.Server(uvicorn.Config(double, host="127.0.0.1", port=engine_port, log_level="error")),
        uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=api_port, log_level="warning")),
    ]
    print(f"READY http://127.0.0.1:{api_port}", flush=True)
    await asyncio.gather(*(s.serve() for s in servers))


asyncio.run(main())
