#!/usr/bin/env bash
# Fulcrum Ops — restart containers that are running but no longer answering.
#
# Compose's `restart: unless-stopped` reacts to a process that EXITS. A process
# that is still there and has stopped answering — a wedged JVM, a server whose
# workers are all stuck — is only ever marked "unhealthy", and stays that way
# until a person notices. This is the missing half: once a minute, from the
# host's cron, restart what Docker reports unhealthy.
#
# It runs on the host on purpose. The usual answer is a third-party image with
# the Docker socket mounted into it — which hands root on this host to a
# container we did not build. A dozen lines of shell next to the daemon need
# neither.
#
# What it will and will not touch:
#   - only containers of this compose project that carry the label
#     fulcrum.autoheal=true (the four stateless services; see docker-compose.yml
#     for why the datastores are left alone);
#   - only containers Docker calls "unhealthy" — never "starting", so a slow
#     start inside its start_period is not interrupted;
#   - each container at most once per COOLDOWN_SECONDS. A restart that does not
#     cure it is not repeated every minute: that would turn one broken service
#     into a restart storm and bury the evidence. It is logged, and left for a
#     person.
#
# Every action is one line on stdout; cron appends it to
# /var/log/fulcrum-autoheal.log (rotated by deploy/logrotate.d/fulcrum-ops).
#
# Install (as root; deploy/README.md, "Host jobs"):
#   install -m 0644 /opt/fulcrum/deploy/cron.d/fulcrum-ops /etc/cron.d/fulcrum-ops
#
# Try it without restarting anything:  DRY_RUN=1 deploy/autoheal.sh

set -uo pipefail

# cron runs with PATH=/usr/bin:/bin; say where docker may live rather than hope.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin

PROJECT="${COMPOSE_PROJECT_NAME:-fulcrum-ops}"
LABEL="${AUTOHEAL_LABEL:-fulcrum.autoheal=true}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-600}"
STOP_TIMEOUT="${STOP_TIMEOUT:-25}"
STATE_DIR="${STATE_DIR:-/var/lib/fulcrum-autoheal}"
DRY_RUN="${DRY_RUN:-0}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) autoheal: $*"; }

if ! command -v docker >/dev/null 2>&1; then
  log "ERROR docker not found on PATH ($PATH)"
  exit 1
fi
mkdir -p "$STATE_DIR" || { log "ERROR cannot create $STATE_DIR"; exit 1; }

# One run at a time: a restart can outlast the minute between two cron ticks,
# and the second run must not restart the same container again underneath it.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$STATE_DIR/.lock"
  flock -n 9 || exit 0
fi

if ! unhealthy="$(docker ps \
      --filter "label=com.docker.compose.project=$PROJECT" \
      --filter "label=$LABEL" \
      --filter "health=unhealthy" \
      --format '{{.Names}}' 2>&1)"; then
  log "ERROR docker ps failed: $unhealthy"
  exit 1
fi

# Silence when there is nothing to do: a line a minute saying so would be the
# only thing in the log, and the restarts that matter would be lost in it.
[ -n "$unhealthy" ] || exit 0

now="$(date +%s)"
status=0
while IFS= read -r name; do
  [ -n "$name" ] || continue
  stamp="$STATE_DIR/$name.last-restart"
  last=0
  [ -f "$stamp" ] && last="$(cat "$stamp" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac

  waited=$((now - last))
  if [ "$waited" -lt "$COOLDOWN_SECONDS" ]; then
    log "SKIP $name is unhealthy again ${waited}s after its last restart (cool-down ${COOLDOWN_SECONDS}s): a restart did not cure it — docker logs $name"
    continue
  fi

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY-RUN would restart $name"
    continue
  fi

  # Stamp before restarting: if the restart itself hangs or fails, the cool-down
  # still applies and the next tick does not pile a second one on top.
  echo "$now" > "$stamp"
  if out="$(docker restart -t "$STOP_TIMEOUT" "$name" 2>&1)"; then
    log "RESTARTED $name (was unhealthy)"
  else
    log "ERROR restarting $name failed: $out"
    status=1
  fi
done <<< "$unhealthy"

exit "$status"
