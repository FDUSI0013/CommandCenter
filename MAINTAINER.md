# FD AI Command Center — maintainer start here

You have been given the keys to a live product. Read this page before you change
anything; it is short on purpose. `HANDOFF.md` is the long engineering brief and
`deploy/README.md` is the operations detail — this page is the map to both.

## What it is

A governance console for AI agents. Teams register an agent, connect it with the
SDK or over OpenTelemetry, and the platform records every run and enforces
policy, guardrails, entitlement and quota **at ingest**, before telemetry is
stored. Twenty-five screens over one FastAPI server and a vanilla-JS console
with no build step.

**The rule that outranks every preference in this codebase:** nothing on a
screen is simulated. If a number is rendered, a service computed it. If a
service cannot answer, the screen says so, and a dash means *not measured* — it
is never rendered as zero. A change that invents, interpolates or zero-fills a
number is wrong even when the screen looks better for it.

## Getting on the server

```
ssh -i idris-fdacc-ed25519 ubuntu@54.162.124.30
```

Key-only; there is no password. `chmod 600` the key file first or OpenSSH will
refuse it. `ubuntu` has passwordless `sudo`. The host is AWS EC2
`i-02c888d6ca6f07a6b` in `us-east-1`, and it is also reachable without SSH
through AWS Systems Manager (`aws ssm start-session --target i-02c888d6ca6f07a6b`)
if you are given an AWS account.

| Thing | Where |
|---|---|
| Live console | https://controlplane.fdprod.net |
| Compose stack | `/opt/fulcrum/deploy/` on the host |
| Served console files | `/srv/fulcrum-ops/` |
| Owner password | `/opt/fulcrum/owner-password.txt` on the host |
| Owner account | `ops@fulcrumops.com` |
| Source | this repository |

Ten containers; all ten should read `healthy`:

```
sudo docker compose -f /opt/fulcrum/deploy/docker-compose.yml ps
```

## Working on it locally

The server is Python 3.12 with its virtualenv already built at
`apps/control-plane/.venv`. The console is plain JavaScript — edit and reload,
there is nothing to compile.

Everything below must pass before a deploy. There is no CI, so these are the
only gate there is:

```
apps/control-plane/.venv/Scripts/python.exe -m pytest        # ~1300 tests, ~30 min
apps/control-plane/.venv/Scripts/python.exe -m ruff check src
apps/control-plane/scripts/check_contract.py                 # console <-> API agreement
scripts/check-branding.sh                                    # no vendor strings may ship
scripts/check-config.sh                                      # compose, Caddyfile, shell, deploy XML
```

`check-branding.sh` exists because the telemetry engine underneath is a
white-labelled Apache-2.0 project and its name must not appear anywhere a user
or a customer can see. Treat a failure there as a release blocker, not a lint
warning.

## Deploying

`deploy/ship.sh` is the path: it stages a tarball to S3, runs the build on the
host over SSM, migrates the database, swaps the image, and publishes the console
files. It takes a database snapshot first and can roll itself back — it reads
the pre-deploy revision from the database rather than from the container,
because a container that fails to start cannot be asked what revision it is on.

Read `deploy/README.md` before your first one. A host last deployed before
2026-09-18 needs two one-time steps under "Upgrades" in that file *before* its
next `docker compose up -d`, or the analytics store comes back read-only.

## Names that look wrong and are not

The product was renamed to **FD AI Command Center** on 2026-09-22. The older
name survives on purpose in technical identifiers that running agents and
customer integrations depend on: the pip package `fulcrum-ops`, the Python
modules `fulcrum_ops` and `fulcrum_ops_api`, `FULCRUM_OPS_*` environment
variables, `X-Fulcrum-*` headers, the `fulcrum-ops-api` CLI, cookie and
localStorage keys, the host name `controlplane.fdprod.net`, the
`apps/control-plane` directory, and image and volume names. Renaming any of them
as a side effect of other work will break live agents. The owner decides when
they change.

## What is open

`HANDOFF.md` ends with the current list. The two live items:

1. The prompt-injection guardrail is wired but inert — it needs the scanner's
   privately distributed model (an access token plus two settings on the
   `safety-scanner` service).
2. The underwriting bridge agent sends runs without model attribution. Its SDK
   was upgraded to 1.0.1 on the host; steps 2 and 3 of the patch
   (`observability.py`, and calling `record_llm_response`) still have to be
   applied in that agent's own repository.

## If something is down

```
sudo docker compose -f /opt/fulcrum/deploy/docker-compose.yml logs --tail=200 control-plane
sudo /opt/fulcrum/deploy/autoheal.sh          # what the host runs on a timer
sudo /opt/fulcrum/deploy/backup.sh            # on-host snapshot + S3
```

Backups go to S3 under `backups/`. Restores are exercised — `deploy/README.md`
has the procedure and the last date it was run.
