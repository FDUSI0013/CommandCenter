# FD AI Command Center

An AI agent governance console. Teams register the agents they run, connect them with
an SDK or over OpenTelemetry, and then govern them: what they may call, what they
cost, what they were asked, what they answered, and what has to be approved
before they act.

The console is twenty-five screens over one API. Nothing in it is simulated —
if a number is on screen, a service computed it, and if a service cannot answer,
the screen says so instead of showing a zero.

## What is here

```
apps/control-plane   the server: FastAPI — identity, tenancy, governance, the public API
apps/web             the console: vanilla JS, no build step, served from the API or the edge
sdks/python          the Python SDK (package fulcrum-ops) — decorators, provider wrappers, batching reporter
sdks/typescript      the TypeScript SDK (@fulcrum-ops/sdk) — the same surface for Node and the browser
engine/              build tooling for the private telemetry engine (source is fetched, never committed)
deploy/              compose stack, edge proxy config, image mirroring, egress verification
docs/                the operator manual (user-guide.html is the master), the analysis this was
                     built from, and the console wiring contract
scripts/             repository gates, including the branding check
```

## How it fits together

```
   an agent ──SDK or OTLP──▶ ┌──────────────────┐
                             │    the server    │  the only public surface
   the console ─────────────▶│  (FastAPI)       │  identity · tenancy · governance
                             └────────┬─────────┘
                                      │ private network, no published port
                             ┌────────▼─────────┐
                             │ telemetry engine │  traces · spans · scores · prompts
                             └──────────────────┘  datasets · experiments · costs
```

Two halves, deliberately separated:

**Governance is ours.** Agents, connections, connectors, policies, approvals, the
audit chain, secrets, quotas, budgets, deployments, licences — Postgres tables and
services we own, with role checks and an append-only, hash-chained audit trail.

**Telemetry is the engine's.** Runs, traces, spans, feedback scores, prompts,
datasets, experiments, guardrails and costs live in a private engine reached only
through one adapter. It runs with its own authentication disabled because it is
not addressable from outside; the server in front of it is the sole
identity authority, and one workspace maps to one project namespace inside it.

When the engine is unreachable, telemetry endpoints fail closed with
`telemetry_unavailable` and the governance half keeps working.

## Running it locally

```bash
cd apps/control-plane
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"
./.venv/Scripts/python.exe -m alembic upgrade head
FULCRUM_OPS_BOOTSTRAP_PASSWORD='choose-one' \
  ./.venv/Scripts/python.exe -m fulcrum_ops_api.cli bootstrap \
  --workspace "Your Company" --email you@example.com --name "Your Name"
./.venv/Scripts/python.exe -m uvicorn fulcrum_ops_api.main:app --port 8080
```

<http://127.0.0.1:8080> serves the console, `/api/v1` the API, `/api/docs` the
contract. Telemetry screens need the engine (see `deploy/`); everything else works
against Postgres or SQLite alone.

## Connecting an agent

Issue a key in the console under Workspace settings, then:

```python
from fulcrum_ops import FulcrumOps, trace

client = FulcrumOps(api_key="fo_live_…")

@trace
def answer(question: str) -> str:
    ...
```

Already instrumented with OpenTelemetry? Point the exporter at the same host and
report to `/v1/traces` — no SDK required. Either way the run passes the same
policy evaluation, guardrail checks, entitlement gate and quota accounting.

## Checks

```bash
scripts/check-branding.sh                              # no vendor strings ship
scripts/check-config.sh                                # deploy config parses: XML, compose, Caddyfile, shell
apps/control-plane/.venv/Scripts/python.exe -m pytest  # the suite, against an engine double
apps/control-plane/.venv/Scripts/python.exe -m ruff check apps/control-plane/src
apps/control-plane/.venv/Scripts/python.exe apps/control-plane/scripts/check_contract.py
deploy/verify-no-egress.sh                             # on a deployed host
```

## Deploying

`deploy/README.md` has the runbook: host sizing, first deployment, the edge
proxy, upgrades (including two one-time steps for a host deployed before
2026-09-18), the host's cron jobs, backups and what to look at when something is
wrong.

## Licensing

The telemetry engine is an Apache-2.0 component redistributed under this product's
name, which that licence permits. Attribution is preserved in
[THIRD_PARTY_LICENCES.md](THIRD_PARTY_LICENCES.md) — repository documentation that
is never bundled into the console, the SDKs or any customer-facing artefact.
`scripts/check-branding.sh` enforces the boundary.
