#!/usr/bin/env bash
# Fulcrum Ops — one-time: move the analytics keeper's transaction log onto its
# named volume.
#
# The keeper image declares /datalog as a VOLUME, and the compose file used to
# mount nothing there, so Docker backed it with an ANONYMOUS volume. The compose
# file now mounts the named volume analytics-keeper-datalog at that path. A named
# volume starts empty: bring the stack up on a host that ran the earlier file
# WITHOUT running this first, and the keeper restarts from its last snapshot with
# no log to replay — silently rolled back by up to 100,000 transactions. The
# analytics store's replicated tables then disagree with their own coordination
# metadata and go read-only, and every agent's ingest fails until somebody runs
# SYSTEM RESTORE REPLICA table by table.
#
# So, on a host that is already running, BEFORE the first `docker compose up -d`
# with the new file:
#
#   cd /opt/fulcrum/deploy && sudo ./migrate-keeper-datalog.sh
#
# It stops the engine, the analytics store and the keeper (in that order — the
# writer first), copies the log across with the keeper's own image (nothing is
# pulled), proves the copy byte for byte, starts the three again on the new
# volume and confirms no replicated table came back read-only. Telemetry is
# unavailable for the few minutes that takes; the governance half of the console
# is not touched.
#
# If `docker compose up -d` was run FIRST: the keeper's start-up guard (see its
# entrypoint in docker-compose.yml) will have refused to start on the empty
# log, so nothing has been rolled back — but recreating the container detached
# the old anonymous volume, and it can no longer be found through the container.
# This script then looks for it among the orphaned volumes and carries on. Two
# candidates is a question for a person: re-run with SOURCE_VOLUME=<name>.
# Until it has succeeded, do NOT run `docker volume prune` — the only copy of
# the log is on a volume that looks unused.
#
# Safe to re-run: a fresh install, or a keeper already on the named volume, is
# reported and left alone. The old anonymous volume is never deleted — its name
# is printed so it can be removed once the stack has been healthy for a while.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")
PROJECT="${COMPOSE_PROJECT_NAME:-fulcrum-ops}"
SERVICE="analytics-keeper"
VOLUME_KEY="analytics-keeper-datalog"
TARGET="${PROJECT}_${VOLUME_KEY}"

say() { echo "[keeper-datalog] $*"; }
die() { echo "[keeper-datalog] error: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed"

cid="$(docker ps -aq \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --filter "label=com.docker.compose.service=$SERVICE" | head -n 1)"
if [ -z "$cid" ]; then
  say "no $SERVICE container in project $PROJECT: a fresh install has nothing to migrate"
  exit 0
fi

source_volume="$(docker inspect -f \
  '{{range .Mounts}}{{if eq .Destination "/datalog"}}{{.Name}}{{end}}{{end}}' "$cid")"
[ -n "$source_volume" ] || die "container $cid has no volume at /datalog; inspect it by hand"

image="$(docker inspect -f '{{.Config.Image}}' "$cid")"

# How many transaction-log files a volume holds (the keeper names them log.<zxid>).
logs_in() {
  docker run --rm --network none --user 0:0 --entrypoint sh \
    -v "$1":/v:ro "$image" -c 'ls /v/version-2/log.* 2>/dev/null | wc -l' | tr -d '[:space:]'
}

if [ "$source_volume" = "$TARGET" ]; then
  if [ "$(logs_in "$TARGET")" != "0" ]; then
    say "$SERVICE already keeps its transaction log on $TARGET: nothing to do"
    exit 0
  fi
  # The container is on the named volume and the volume is empty: `up -d` was
  # run with the new compose file BEFORE this script. The keeper's start-up
  # guard will have refused to run on the empty log, so nothing is lost yet —
  # but recreating the container detached the old anonymous volume, and it can
  # no longer be found through the container. Find it among the orphans.
  say "$TARGET is empty and the container already points at it: looking for the orphaned log"
  source_volume="${SOURCE_VOLUME:-}"
  if [ -z "$source_volume" ]; then
    candidates=()
    for volume in $(docker volume ls -q --filter dangling=true); do
      # Anonymous volumes are named by a 64-character hex id; skip the rest.
      case "$volume" in *[!0-9a-f]*) continue ;; esac
      [ "${#volume}" -eq 64 ] || continue
      [ "$(logs_in "$volume")" != "0" ] && candidates+=("$volume")
    done
    case "${#candidates[@]}" in
      1) source_volume="${candidates[0]}" ;;
      0) die "no orphaned volume holds a keeper log. If this install has never stored anything,
       remove both keeper volumes and start again; otherwise the log is gone (was
       'docker volume prune' run?) and the replicated tables need SYSTEM RESTORE REPLICA." ;;
      *) printf '[keeper-datalog]   candidate: %s\n' "${candidates[@]}" >&2
         die "more than one orphaned volume holds a keeper log. Inspect them
       (docker volume inspect <name> shows CreatedAt) and re-run with SOURCE_VOLUME=<name>." ;;
    esac
  fi
fi

say "container  $cid ($image)"
say "from       $source_volume (anonymous)"
say "to         $TARGET"

# A target that already holds a log is either a finished migration whose
# containers were never recreated, or an interrupted one. Copying over it could
# replace a newer log with an older one, so that is a decision for a person.
if docker volume inspect "$TARGET" >/dev/null 2>&1; then
  existing="$(docker run --rm --network none --user 0:0 --entrypoint sh \
    -v "$TARGET":/to:ro "$image" -c 'find /to -type f | wc -l')"
  if [ "${existing//[[:space:]]/}" != "0" ]; then
    die "$TARGET already holds ${existing//[[:space:]]/} file(s). If an earlier run of this script was
       interrupted, remove it (docker volume rm $TARGET) and run this again; if
       the keeper has already started on it, do NOT — that log is now the live one."
  fi
else
  # Labelled the way compose labels its own volumes, so `up` adopts it instead
  # of warning that the volume "was not created by Docker Compose".
  docker volume create \
    --label "com.docker.compose.project=$PROJECT" \
    --label "com.docker.compose.volume=$VOLUME_KEY" \
    "$TARGET" >/dev/null
fi

say "stopping telemetry-engine, analytics-db, $SERVICE"
# From here until the new containers are up, a failure must not leave telemetry
# down: the old containers still exist, still point at the old volume, and
# `docker start` brings them back exactly as they were.
old_containers="$cid $("${COMPOSE[@]}" ps -aq analytics-db telemetry-engine | tr '\n' ' ')"
phase=stopping
restore_old() {
  status=$?
  if [ "$phase" = "stopping" ] && [ "$status" -ne 0 ]; then
    echo "[keeper-datalog] restarting the previous containers unchanged" >&2
    # shellcheck disable=SC2086
    docker start $old_containers >/dev/null 2>&1 || true
  fi
}
trap restore_old EXIT
"${COMPOSE[@]}" stop telemetry-engine
"${COMPOSE[@]}" stop analytics-db
"${COMPOSE[@]}" stop "$SERVICE"

say "copying and verifying"
# cp -a keeps the keeper user's ownership. The checksum of checksums is compared
# inside the same container so a short copy fails here, not at the next restart.
docker run --rm --network none --user 0:0 --entrypoint sh \
  -v "$source_volume":/from:ro -v "$TARGET":/to "$image" -c '
    set -e
    cp -a /from/. /to/
    sum() { (cd "$1" && find . -type f | sort | xargs -r sha256sum | sha256sum); }
    [ "$(sum /from)" = "$(sum /to)" ] || { echo "copy does not match its source" >&2; exit 1; }
    echo "  $(find /to -type f | wc -l) file(s), $(du -sh /to | cut -f1)"
  ' || die "the copy failed. The source volume is untouched and the previous containers
       are being restarted on it. Do NOT run 'docker compose up -d' with the new
       compose file until this script has succeeded."

phase=starting
say "starting $SERVICE, analytics-db, telemetry-engine on the named volume"
"${COMPOSE[@]}" up -d "$SERVICE" analytics-db telemetry-engine

say "waiting for analytics-db to report healthy"
db="$("${COMPOSE[@]}" ps -q analytics-db)"
status=unknown
for _ in $(seq 1 60); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$db" 2>/dev/null || echo unknown)"
  [ "$status" = "healthy" ] && break
  sleep 5
done
[ "$status" = "healthy" ] || die "analytics-db is '$status' after 5 minutes: docker compose logs analytics-db $SERVICE"

readonly_tables="$("${COMPOSE[@]}" exec -T analytics-db sh -c \
  'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "SELECT count() FROM system.replicas WHERE is_readonly"')"
readonly_tables="${readonly_tables//[[:space:]]/}"
if [ "$readonly_tables" != "0" ]; then
  die "$readonly_tables replicated table(s) are read-only. The keeper did not come back with the
       state the store expects. The old log is still on volume $source_volume."
fi

say "done: 0 read-only replicas. The old anonymous volume is kept as a fallback:"
say "  $source_volume"
say "remove it once the stack has been healthy for a day:  docker volume rm $source_volume"
