#!/usr/bin/env bash
# Nightly backup of the governance state — the irreplaceable database.
#
# Dumps the app-db Postgres through the running container, keeps a short local
# window, and mirrors to S3 when a bucket is configured. This script protects the
# part that cannot be re-earned — agents, policies, approvals, secrets, licences,
# the audit chain.
#
# It also takes a small dump of the engine's registry (state-db, MySQL). The
# telemetry itself (ClickHouse) is deliberately not covered: it is two orders of
# magnitude larger and regenerable. The registry is neither — it is a few
# megabytes, and it holds the project ids that every agent row in app-db points
# at, plus the prompts, datasets and rules. Restore app-db alone after losing the
# volume and every agent comes back pointing at a project that no longer exists.
#
# The dump is unreadable without FULCRUM_OPS_ENCRYPTION_KEY (Fernet columns).
# The key is escrowed OFF this host in SSM Parameter Store:
#   /fulcrum-ops/prod/APP_ENCRYPTION_KEY   (us-east-1)
# A backup of this database plus that parameter is a full recovery; either one
# alone is not.
#
# A BACKUP THAT DID NOT HAPPEN MUST NOT LOOK LIKE ONE THAT DID. Every failure —
# a dump that stopped half way, an archive that does not verify, an upload that
# did not land, and above all "S3 is configured but this host has no aws CLI",
# which used to print one line and exit 0 every night — now:
#   - exits non-zero;
#   - leaves $BACKUP_DIR/BACKUP_FAILED saying what went wrong and when (removed
#     by the next fully successful run);
#   - leaves $BACKUP_DIR/last-success untouched, so its age is the age of the
#     last backup that can actually be restored from. Alarm on either:
#       test ! -e /opt/fulcrum/backups/BACKUP_FAILED \
#         && find /opt/fulcrum/backups/last-success -mmin -1560 | grep -q .
# A dump is written under a .partial name and only renamed once it has verified,
# so rotation can never count a truncated file as one of the dumps worth keeping.
#
# Install (as root; deploy/README.md, "Host jobs") — cron and log rotation:
#   install -m 0644 /opt/fulcrum/deploy/cron.d/fulcrum-ops      /etc/cron.d/fulcrum-ops
#   install -m 0644 /opt/fulcrum/deploy/logrotate.d/fulcrum-ops /etc/logrotate.d/fulcrum-ops
# Restore (proven 2026-08-24, see docs):
#   gunzip -c app-db-<stamp>.sql.gz | docker compose exec -T app-db psql -U fulcrum -d <fresh-db>
#   gunzip -c state-db-<stamp>.sql.gz | docker compose exec -T state-db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot'
set -euo pipefail

# cron's PATH is /usr/bin:/bin. The AWS CLI v2 installs to /usr/local/bin and the
# snap to /snap/bin, so under cron `command -v aws` failed on a host where it
# works perfectly well in a shell.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin

COMPOSE_DIR="${COMPOSE_DIR:-/opt/fulcrum/deploy}"
BACKUP_DIR="${BACKUP_DIR:-/opt/fulcrum/backups}"
KEEP_LOCAL="${KEEP_LOCAL:-7}"
BACKUP_STATE_DB="${BACKUP_STATE_DB:-1}"   # 0 skips the engine registry dump
# ${VAR-default}, not ${VAR:-default}: S3_PREFIX="" is the documented way to say
# "no upload", and ":-" would quietly turn that back into the default bucket.
S3_PREFIX="${S3_PREFIX-s3://fulcrum-ops-demo-deploy-155954279114/backup/app-db}"   # empty skips upload

cd "$COMPOSE_DIR"
mkdir -p "$BACKUP_DIR"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
MARKER="$BACKUP_DIR/BACKUP_FAILED"
PARTIALS=()

fail() {
    echo "BACKUP FAILED: $*" >&2
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" > "$MARKER"
    exit 1
}

# Anything that ends this script early without going through fail() — set -e on a
# command nobody expected to fail, a signal — still leaves the marker behind.
on_exit() {
    status=$?
    [ "${#PARTIALS[@]}" -eq 0 ] || rm -f -- "${PARTIALS[@]}"
    if [ "$status" -ne 0 ] && [ ! -s "$MARKER" ]; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) exited $status before finishing (see /var/log/fulcrum-backup.log)" > "$MARKER"
    fi
}
trap on_exit EXIT

# Two copies of this at once — an old /etc/cron.d/fulcrum-backup left beside the
# new file — would rotate each other's dumps away.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$BACKUP_DIR/.lock"
    if ! flock -n 9; then
        # Not a failed backup: the run that holds the lock reports its own outcome.
        echo "another backup is still running; this one steps aside" >&2
        trap - EXIT
        exit 1
    fi
fi
rm -f "$MARKER"

DB_USER=$(grep -oP '^APP_DB_USER=\K.*' .env 2>/dev/null || echo fulcrum)
DB_NAME=$(grep -oP '^APP_DB_NAME=\K.*' .env 2>/dev/null || echo fulcrum_ops)

# verified_dump <final path> <the dump's own completion line> <command...>
#
# Both dump tools write a trailer as their very last act, so an archive that
# ends with it is a dump that ran to the end. `gzip -t` alone would pass a
# stream that was cut off cleanly between two statements.
verified_dump() {
    local out="$1" trailer="$2"; shift 2
    local tmp="$out.partial"
    PARTIALS+=("$tmp")
    "$@" | gzip > "$tmp" || fail "$(basename "$out"): the dump command failed"
    gzip -t "$tmp" 2>/dev/null || fail "$(basename "$out"): the archive does not verify"
    # grep -c, not -q: -q stops reading at the first match, and under pipefail
    # the SIGPIPE that gives `tail` would be reported as a failed check.
    [ "$(gunzip -c "$tmp" | tail -n 8 | grep -c -- "$trailer")" -ge 1 ] \
        || fail "$(basename "$out"): the dump is incomplete (no '$trailer' at its end)"
    # A dump that gzips to almost nothing is a failed dump, not a small database.
    local bytes
    bytes=$(stat -c%s "$tmp")
    [ "$bytes" -ge 10240 ] || fail "$(basename "$out"): suspiciously small ($bytes bytes)"
    mv -- "$tmp" "$out"
    echo "backup written: $out ($bytes bytes)"
}

APP_OUT="$BACKUP_DIR/app-db-$STAMP.sql.gz"
verified_dump "$APP_OUT" "PostgreSQL database dump complete" \
    docker compose exec -T app-db pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner
FILES=("$APP_OUT")

# From here on a problem is remembered rather than fatal: the governance dump is
# already safe on disk and must still be uploaded and rotated. The run fails at
# the end all the same.
problems=()

if [ "$BACKUP_STATE_DB" = "1" ]; then
    STATE_OUT="$BACKUP_DIR/state-db-$STAMP.sql.gz"
    # The password is read from the container's own environment and passed as
    # MYSQL_PWD, so it appears neither in this host's process list nor in the log.
    # Run in a subshell so that fail() inside it ends this dump, not the script.
    PARTIALS+=("$STATE_OUT.partial")
    if (verified_dump "$STATE_OUT" "Dump completed" \
            docker compose exec -T state-db sh -c \
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --no-tablespaces --routines --databases "$MYSQL_DATABASE"'); then
        FILES+=("$STATE_OUT")
    else
        problems+=("the engine registry (state-db) dump failed")
    fi
fi

if [ -n "$S3_PREFIX" ]; then
    if ! command -v aws >/dev/null 2>&1; then
        problems+=("S3_PREFIX is set but there is no aws CLI on PATH ($PATH): NOTHING LEFT THIS HOST. Install it (deploy/host-bootstrap.sh does), or set S3_PREFIX= to declare local-only backups")
    else
        for file in "${FILES[@]}"; do
            name=$(basename "$file")
            # Upload, then ask the bucket: `cp` exiting 0 is a claim, the listing is the evidence.
            # (`aws s3 ls` exits 1 when nothing matches.)
            if aws s3 cp "$file" "$S3_PREFIX/$name" --only-show-errors \
                && aws s3 ls "$S3_PREFIX/$name" >/dev/null; then
                echo "mirrored to $S3_PREFIX/$name"
            else
                problems+=("$name did not reach $S3_PREFIX")
            fi
        done
    fi
fi

# Rotate: keep the newest $KEEP_LOCAL local dumps of each kind. Only verified
# dumps carry these names, so a bad night can no longer push a good one out.
for kind in app-db state-db; do
    # `|| true`: no dumps of a kind (BACKUP_STATE_DB=0) makes ls fail, and under
    # pipefail that would otherwise end a successful backup as a failed one.
    ls -1t "$BACKUP_DIR"/"$kind"-*.sql.gz 2>/dev/null | tail -n +$((KEEP_LOCAL + 1)) | xargs -r rm -- || true
done

if [ "${#problems[@]}" -gt 0 ]; then
    fail "$(IFS=';'; echo "${problems[*]}")"
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/last-success"
echo "backup complete: $STAMP"
