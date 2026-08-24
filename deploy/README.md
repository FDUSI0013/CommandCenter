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

| Service | Cap |
|---|---:|
| analytics DB (ClickHouse) | 5.0 GB |
| telemetry engine (JVM) | 4.0 GB |
| safety scanner | 2.5 GB |
| state DB (MySQL) | 1.2 GB |
| control plane | 1.0 GB |
| cache (Redis) | 0.9 GB |
| metric runner | 0.9 GB |
| app DB (Postgres) | 0.8 GB |
| blob store (MinIO) | 0.6 GB |
| analytics keeper | 0.6 GB |
| **total** | **17.5 GB** |

Add the operating system, Docker and Caddy and the floor is about **19 GB**.

| Instance | vCPU | RAM | ~$/month (us-east-1) | Verdict |
|---|---:|---:|---:|---|
| **r6i.xlarge** | 4 | 32 GB | ~$184 | **Recommended.** This workload is memory-bound, not CPU-bound. |
| t3.2xlarge | 8 | 32 GB | ~$243 | More CPU than needed; burstable credits are a liability under sustained ingest. |
| t3.xlarge | 4 | 16 GB | ~$121 | Only with the caps lowered (ClickHouse 3 GB, safety scanner 1.5 GB) and no headroom. |
| t3.micro | 2 | 1 GB | ~$8 | What the demo runs on today. Cannot run this stack. |

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
```

Then remove `FULCRUM_OPS_BOOTSTRAP_*` from `.env` and restart the control plane.

## The edge

`Caddyfile` goes to `/etc/caddy/Caddyfile` on the host. It serves the console's
static files, proxies `/api/*` and `/health` to the control plane, and proxies
`/v1/traces` for agents that report over OpenTelemetry. It sets HSTS, a strict
CSP, `X-Frame-Options: DENY` and disables buffering on the streaming routes —
without `flush_interval -1` the live run stream would arrive in chunks minutes
apart.

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

## Backups

Three things hold state worth keeping:

| What | Where | How |
|---|---|---|
| Governance state | `app-db` (Postgres) | `pg_dump` — this is the irreplaceable one: agents, policies, approvals, secrets, licences, the audit chain |
| Telemetry | `analytics-db` (ClickHouse) + `state-db` (MySQL) | `clickhouse-backup` / `mysqldump`; large, and regenerable if agents still hold their history |
| Attachments | `blob-store` (MinIO) | `mc mirror` to S3 |

The secrets vault is encrypted with `APP_ENCRYPTION_KEY`. **A backup of the
database without that key is unreadable.** Store the key somewhere the database
backup is not.

## When something is wrong

```bash
docker compose logs -f control-plane          # our API
docker compose logs -f telemetry-engine       # the private engine
curl -s localhost:8080/health | jq            # database + telemetry reachability
docker stats --no-stream                      # who is eating the memory
```

`/health` returning `"telemetry": false` means the engine is unreachable: the
governance half of the console keeps working and every telemetry screen fails
closed with a clear message rather than showing a fabricated zero.
