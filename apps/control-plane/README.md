# FD AI Command Center — server API

The only publicly reachable component of FD AI Command Center. It owns identity, tenancy,
governance state and the public API contract. The telemetry engine it reads and
writes sits on a private network and is never addressable from outside.

## Run it locally

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"     # POSIX: .venv/bin/python
./.venv/Scripts/python.exe -m alembic upgrade head
FULCRUM_OPS_BOOTSTRAP_PASSWORD='choose-one' \
  ./.venv/Scripts/python.exe -m fulcrum_ops_api.cli bootstrap \
  --workspace "Your Company" --email you@example.com --name "Your Name"
./.venv/Scripts/python.exe -m uvicorn fulcrum_ops_api.main:app --port 8080
```

`.env.local` holds development settings; every one of them is an environment
variable prefixed `FULCRUM_OPS_`. With `FULCRUM_OPS_STATIC_DIR` set, this process
also serves the console, so the whole product is on one origin at
<http://127.0.0.1:8080> — API at `/api/v1`, docs at `/api/docs`.

Telemetry-backed screens need the engine. Without it they fail closed with a
`telemetry_unavailable` error rather than showing a fabricated number; the
governance half of the product works entirely from Postgres.

## Layout

```
src/fulcrum_ops_api/
  api/          route tree (v1 per domain), request dependencies, list helpers
  core/         config, logging, error translation, crypto
  db/           declarative base, async engine and session lifecycle
  engine/       the private telemetry adapter — the only place it is spoken to
  models/       SQLAlchemy tables
  schemas/      Pydantic request/response models: the API contract
  services/     business logic, importable without FastAPI
  cli.py        bootstrap, user and key administration
migrations/     Alembic revisions
scripts/        contract check between the console's client and the OpenAPI doc
tests/          pytest suite, run against an in-process engine double
```

## Checks

```bash
./.venv/Scripts/python.exe -m ruff check src
./.venv/Scripts/python.exe -m pytest
./.venv/Scripts/python.exe scripts/check_contract.py
```
