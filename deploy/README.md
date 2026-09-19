# Deploying Fulcrum Ops

One host runs everything. The edge proxy terminates TLS and is the only thing
listening publicly; the control plane sits behind it on loopback; the telemetry
engine and its four datastores are on a private compose network with no
published ports at all.

```
              :443
  internet ──────────▶ Caddy ──▶ 127.0.0.1:8080  control plane ──┐
                        │                                        │  private network
                        └─ /  console static files               ▼
                                                          telemetry engine
                                                   ├── state DB (MySQL)
                                                   ├── analytics DB (ClickHouse + keeper)
                                                   ├── cache / queues (Redis)
                                                   └── blob store (MinIO)
                                            control plane ── app DB (Postgres)
```

## Sizing the host

The compose file caps every service, and those caps add up:

| Service | Memory cap | CPU cap | CPU weight |
|---|---:|---:|---:|
| analytics DB (ClickHouse) | 5.0 GB | none | 1024 |
| telemetry engine (JVM) | 4.0 GB | none | 1024 |
| control plane | 3.0 GB | none | **2048** |
| safety scanner | 3.0 GB | 2.0 cores | 512 |
| metric runner | 2.5 GB | 1.5 cores | 256 |
| app DB (Postgres) | 1.5 GB | none | **2048** |
| state DB (MySQL) | 1.2 GB | none | 1024 |
| cache (Redis) | 0.9 GB | none | 1024 |
| analytics keeper | 0.75 GB | none | 1024 |
| blob store (MinIO) | 0.6 GB | none | 1024 |
| **total** | **22.5 GB** | | |

Add the operating system, Docker and Caddy and the floor is about **24 GB**.

Three of those numbers were learned the hard way, and the compose file says why
next to each: the **metric runner** pre-forks four ~310 MB executors (it used to
get 900 MB for five, and the kernel killed and re-forked the fifth ~450 times an
hour at a full core); the **control plane** runs four workers at ~240 MB each
(they sat at 870 MB of a 1 GB cap); the **keeper**'s JVM is told its heap
(`-Xmx512m`) because the image's default of 1000 MB is more than its container
is allowed.

The CPU weight (`cpu_shares`) only matters when the cores are contended, and
then it decides who is served first: the API and its database — what a person at
the console and an agent at ingest are waiting on — before model inference in
the scanner and user-supplied code in the runner, both of which are also capped
outright. A weight costs nothing while cores are idle.

| Instance | vCPU | RAM | ~$/month (us-east-1) | Verdict |
|---|---:|---:|---:|---|
| **r6i.xlarge** | 4 | 32 GB | ~$184 | **Recommended**, and what production runs on. |
| t3.2xlarge | 8 | 32 GB | ~$243 | More CPU; burstable credits are a liability under sustained ingest. |
| t3.xlarge | 4 | 16 GB | ~$121 | Does not fit: the caps alone are 22.5 GB. |

Storage: **100 GB gp3** minimum — ClickHouse parts, MySQL, MinIO attachments and
the images themselves. Grow it before it reaches 80%; ClickHouse behaves badly
on a full disk.

## First deployment

```bash
# 1. Build the private images from vendored source (on the host, or anywhere
#    with Docker, then push).
engine/fetch-vendor.sh ~/engine-source.zip
engine/build.sh                       # patches, builds, verifies no outbound reporting
AWS_REGION=us-east-1 ENGINE_TAG=1.0.0 deploy/mirror-images.sh

# 2. Configure.
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env                   # every value; nothing has a usable default

# 3. Start.
cd deploy && docker compose up -d
docker compose ps                     # wait for every service to report healthy

# 4. Create the first workspace and owner.
docker compose exec control-plane fulcrum-ops-api bootstrap \
  --workspace "Your Company" --email you@example.com --name "Your Name"
#    Copy the API key it prints. It is shown once.

# 5. Prove the private services are private.
deploy/verify-no-egress.sh

# 6. Install the host jobs (see "Host jobs" below). host-bootstrap.sh does this
#    itself when the repository is already at /opt/fulcrum.
sudo install -m 0644 -o root -g root deploy/cron.d/fulcrum-ops      /etc/cron.d/fulcrum-ops
sudo install -m 0644 -o root -g root deploy/logrotate.d/fulcrum-ops /etc/logrotate.d/fulcrum-ops
```

Then remove `FULCRUM_OPS_BOOTSTRAP_*` from `.env` and restart the control plane.

`ENGINE_TAG` has no default: compose refuses to start until `.env` names the
engine build to run. `deploy/mirror-images.sh` ends by printing the two lines to
put there (`IMAGE_PREFIX=…/fulcrum-ops` and `ENGINE_TAG=…`), and warns when the
registry would let a later push replace the image behind a tag (a MUTABLE
repository); `ENFORCE_IMMUTABLE=1` locks them.

## The edge

`Caddyfile` goes to `/etc/caddy/Caddyfile` on the host. It serves the console's
static files, proxies `/api/*` and `/health` to the control plane, and proxies
`/v1/traces` for agents that report over OpenTelemetry. It sets HSTS, a strict
CSP, `X-Frame-Options: DENY` and disables buffering on the streaming routes —
without `flush_interval -1` the live run stream would arrive in chunks minutes
apart.

Three settings on every proxy block are there because of how this stack fails,
and the file explains each where it is set:

- **A 30-second retry window** (`lb_try_duration`). The control plane is one
  container; while it restarts nothing listens on 8080. A request that arrives
  in that gap is held and re-dialled instead of being answered 502 — so a deploy
  no longer shows the console "cannot reach the control plane" or refuses an
  agent's ingest batch. Only a failed dial is retried for a POST, so nothing is
  delivered twice.
- **`response_header_timeout 150s`**, replacing a 24-hour read timeout on every
  API route. It is longer than any wait our own clients have (a Prompt Studio
  run is allowed 135 s), and it means a request into a wedged worker ends in a
  504 rather than being held for a day. The two event streams keep a
  `read_timeout` of their own — it is per read, and they heartbeat every 15 s.
- **`keepalive 60s`**, paired with `--timeout-keep-alive 75` on the control
  plane. The side that reuses idle connections has to give them up first, or a
  POST is eventually sent down a socket the server has just closed.

A 10 MB `request_body` ceiling sits just above the API's own 8 MiB ingest limit,
for every route that has no limit of its own.

The console's files are served `no-cache` — revalidate before reuse — because
they have fixed names and a browser could otherwise run a previous deploy's
JavaScript against this one's HTML. The exception is a URL carrying a build
stamp: `publish-console.sh` rewrites the published `index.html` so each asset it
references becomes `js/app.js?v=<digest>`, and the edge serves those with a
year-long `immutable` max-age. Only `index.html` is then revalidated on a
reload, and it is what hands out the new stamps after a deploy. Republishing
without redeploying the Caddyfile is safe in either order: an unstamped URL
still revalidates, and a stamped one is only ever produced by a publish.

Check an edited file before it goes live — a typo is otherwise found by the
reload failing on the production host:

```bash
FULCRUM_SITE_ADDRESSES=localhost caddy adapt --config deploy/Caddyfile >/dev/null && echo ok
```

(`scripts/check-config.sh` runs the same check wherever caddy is installed.)

Certificates are issued automatically. The DNS record must already point at the
host before the first start, or issuance fails and Caddy will retry with
backoff.

## Upgrades

```bash
cd deploy
docker compose pull                  # or: engine/build.sh && deploy/mirror-images.sh
docker compose up -d control-plane   # migrations run in the container's CMD
```

The control plane runs `alembic upgrade head` before it starts serving, so a
restart never serves against an older schema. Roll back by deploying the
previous `APP_TAG`; roll the schema back only with a deliberate down-revision.

The control-plane image is built on the host, and `pyproject.toml` gives lower
bounds only — so a rebuild installs whatever is newest that day unless
`deploy/constraints.txt` pins it. That file ships without pins and says how to
record them from the running, verified image (`pip freeze` inside the
container). Do that once after a deploy you are satisfied with, and commit it.
The build copies `deploy/constraints.txt`, so ship `deploy/` together with
`apps/control-plane` — without it the build stops at that `COPY`.

The control plane waits for its database to be healthy and for the engine merely
to have *started*: `up -d control-plane` works while the engine is unhealthy,
which is exactly when a control-plane fix is most likely to be needed.

### One-time steps for a host deployed before 2026-09-18

Two changes in this release do not take effect safely by themselves. Do them in
this order, **before** the first plain `docker compose up -d` with the new files.

**1. Move the keeper's transaction log onto its named volume — or lose it.**
The keeper image declares `/datalog` as a volume and the compose file used to
mount nothing there, so the log — the only durable record of everything since the
last snapshot — has been living on an *anonymous* volume. The compose file now
mounts a named one at that path, and a named volume starts empty. Bring the stack
up without migrating and the keeper restarts from its last snapshot, silently
rolled back; the analytics store's replicated tables then disagree with their own
metadata and go read-only, and ingest fails for every agent until somebody runs
`SYSTEM RESTORE REPLICA` table by table.

```bash
cd /opt/fulcrum/deploy
sudo bash migrate-keeper-datalog.sh
```

It stops the engine, the analytics store and the keeper, copies the log with the
keeper's own image, verifies the copy byte for byte, starts the three on the new
volume and checks that no replicated table came back read-only. If the copy
fails it restarts the old containers unchanged. Telemetry is unavailable for a
few minutes; the governance half of the console is not touched. It is safe to
re-run, and does nothing on a fresh install.

If `up -d` was run first anyway: the keeper now refuses to start when it finds a
snapshot and no transaction log (`docker compose logs analytics-keeper` says
`REFUSING TO START`), so nothing has been rolled back. Run the script then — it
finds the detached anonymous volume among the orphans and recovers from there.
**Do not run `docker volume prune` until it has succeeded**: the only copy of the
log is on a volume that looks unused.

**2. Reclaim the analytics store's diagnostic logs.** The configuration now keeps
one system log (`query_log`, for a week) and removes the rest, but removing a log
only stops it being written — the 16 GB already on disk stays there.

```bash
docker compose restart analytics-db                 # a bind-mounted config file is re-read at start
bash prune-analytics-system-logs.sh                 # lists the leftover tables and their sizes
bash prune-analytics-system-logs.sh --drop          # drops them
```

Then bring everything else up: `docker compose up -d`, and install the host jobs
below.

## Host jobs

Compose restarts a container whose process **exits**. It does nothing about one
that is still running and has stopped answering — that is only ever reported as
`unhealthy`. `deploy/autoheal.sh` is the other half: run from the host's cron
once a minute, it restarts what Docker calls unhealthy.

- It runs **on the host**, deliberately. The usual alternative is a third-party
  image with the Docker socket mounted into it, which is root on this host for a
  container we did not build.
- It touches only containers labelled `fulcrum.autoheal=true`: the control
  plane, the engine, the metric runner and the safety scanner. The datastores
  are left out — one that is unhealthy is usually recovering, and restarting it
  mid-recovery makes things worse.
- It restarts a container **at most once every ten minutes**. If the restart did
  not cure it, it says so in the log and leaves it for a person, rather than
  restarting it every minute and burying the evidence.
- For that to be safe, the control plane's container healthcheck is a *liveness*
  probe: it passes on any answer from `/health`, including the 503 that means
  "the engine is down". Restarting the API cannot mend the engine; it would only
  take login and the governance screens down with it, in a loop. `/health`
  itself is unchanged and still tells a person or a monitor the whole truth.

`deploy/cron.d/fulcrum-ops` is the source of truth for the schedule:

```
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
* * * * *   root  bash /opt/fulcrum/deploy/autoheal.sh >> /var/log/fulcrum-autoheal.log 2>&1
17 2 * * *  root  bash /opt/fulcrum/deploy/backup.sh >> /var/log/fulcrum-backup.log 2>&1
41 3 * * 0  root  docker builder prune -f --filter until=168h >> /var/log/fulcrum-housekeeping.log 2>&1
```

Install it, and the rotation for those three logs, as root:

```bash
install -m 0644 -o root -g root /opt/fulcrum/deploy/cron.d/fulcrum-ops      /etc/cron.d/fulcrum-ops
install -m 0644 -o root -g root /opt/fulcrum/deploy/logrotate.d/fulcrum-ops /etc/logrotate.d/fulcrum-ops
rm -f /etc/cron.d/fulcrum-backup     # the earlier backup-only file; left in place the dump runs twice
DRY_RUN=1 bash /opt/fulcrum/deploy/autoheal.sh      # says what it would restart, restarts nothing
```

The `PATH` line matters: cron's own is `/usr/bin:/bin`, the AWS CLI lives in
`/usr/local/bin`, and that is how a backup can "succeed" every night without a
copy ever leaving the host. The weekly `builder prune` is there because the
control plane is built on the host and nothing else ever removes the build cache
(22 GB of it by September 2026).

## Backups

Three things hold state worth keeping:

| What | Where | How |
|---|---|---|
| Governance state | `app-db` (Postgres) | `deploy/backup.sh`, nightly — this is the irreplaceable one: agents, policies, approvals, secrets, licences, the audit chain |
| Engine registry | `state-db` (MySQL) | `deploy/backup.sh`, same run — a few megabytes: the project ids every agent row points at, plus prompts, datasets and rules. Restore `app-db` without it and every agent points at a project that no longer exists |
| Telemetry | `analytics-db` (ClickHouse) | `clickhouse-backup`; large, and regenerable if agents still hold their history. Not covered by `backup.sh` |
| Attachments | `blob-store` (MinIO) | `mc mirror` to S3 |

The secrets vault is encrypted with `APP_ENCRYPTION_KEY`. **A backup of the
database without that key is unreadable.** The key is escrowed off the host, in
SSM Parameter Store (`/fulcrum-ops/prod/APP_ENCRYPTION_KEY`, us-east-1); the
dumps are mirrored to S3. Either alone is not a recovery.

`backup.sh` is built so that a backup that did not happen cannot look like one
that did. A dump is written under a `.partial` name and renamed only once the
archive verifies and ends with the dump tool's own completion line, so rotation
never counts a truncated file among the seven it keeps. Every failure — most
importantly "S3 is configured but this host has no `aws` CLI", which used to
print one line and exit 0 — exits non-zero and leaves
`/opt/fulcrum/backups/BACKUP_FAILED` saying what went wrong; only a fully
successful run removes it and touches `last-success`. Both are there to be
alarmed on:

```bash
test ! -e /opt/fulcrum/backups/BACKUP_FAILED \
  && find /opt/fulcrum/backups/last-success -mmin -1560 | grep -q . \
  || echo "no good backup in the last 26 hours"
```

`S3_PREFIX=` (empty) declares local-only backups and is the only way to run
without the CLI. Restore, as proven on 2026-08-24:

```bash
gunzip -c app-db-<stamp>.sql.gz | docker compose exec -T app-db psql -U fulcrum -d <fresh-db>
```

## When something is wrong

```bash
docker compose logs -f control-plane          # our API
docker compose logs -f telemetry-engine       # the private engine
curl -s localhost:8080/health | jq            # database + telemetry reachability
docker stats --no-stream                      # who is eating the memory
docker compose ps                             # anything "unhealthy"?
tail -n 50 /var/log/fulcrum-autoheal.log      # what was restarted, when — and what a restart did not cure
cat /opt/fulcrum/backups/BACKUP_FAILED        # present only if the last backup failed
```

A service whose process is killed *inside* its container — the metric runner's
executors, a control-plane worker — is replaced by its own supervisor, and
Docker's restart count never moves. `docker stats` sitting at a container's
memory limit, and `dmesg | grep -i 'killed process'`, are how that shows up.

`/health` returning `"telemetry": false` means the engine is unreachable: the
governance half of the console keeps working and every telemetry screen fails
closed with a clear message rather than showing a fabricated zero.
