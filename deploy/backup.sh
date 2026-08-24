#!/usr/bin/env bash
# Nightly backup of the governance state — the irreplaceable database.
#
# Dumps the app-db Postgres through the running container, keeps a short local
# window, and mirrors to S3 when a bucket is configured. Telemetry (ClickHouse,
# MySQL) is deliberately not covered here: it is regenerable and two orders of
# magnitude larger; this script protects the part that cannot be re-earned —
# agents, policies, approvals, secrets, licences, the audit chain.
#
# The dump is unreadable without FULCRUM_OPS_ENCRYPTION_KEY (Fernet columns).
# The key is escrowed OFF this host in SSM Parameter Store:
#   /fulcrum-ops/prod/APP_ENCRYPTION_KEY   (us-east-1)
# A backup of this database plus that parameter is a full recovery; either one
# alone is not.
#
# Install:  cp backup.sh /opt/fulcrum/deploy/ && chmod +x
#   echo '17 2 * * * root /opt/fulcrum/deploy/backup.sh >> /var/log/fulcrum-backup.log 2>&1' > /etc/cron.d/fulcrum-backup
# Restore (proven 2026-08-24, see docs):
#   gunzip -c app-db-<stamp>.sql.gz | docker compose exec -T app-db psql -U fulcrum -d <fresh-db>
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/fulcrum/deploy}"
BACKUP_DIR="${BACKUP_DIR:-/opt/fulcrum/backups}"
KEEP_LOCAL="${KEEP_LOCAL:-7}"
S3_PREFIX="${S3_PREFIX:-s3://fulcrum-ops-demo-deploy-155954279114/backup/app-db}"   # empty skips upload

cd "$COMPOSE_DIR"
mkdir -p "$BACKUP_DIR"

DB_USER=$(grep -oP '^APP_DB_USER=\K.*' .env 2>/dev/null || echo fulcrum)
DB_NAME=$(grep -oP '^APP_DB_NAME=\K.*' .env 2>/dev/null || echo fulcrum_ops)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/app-db-$STAMP.sql.gz"

docker compose exec -T app-db pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner | gzip > "$OUT"

# A dump that gzips to almost nothing is a failed dump, not a small database.
BYTES=$(stat -c%s "$OUT")
if [ "$BYTES" -lt 10240 ]; then
    echo "backup suspiciously small ($BYTES bytes): $OUT" >&2
    exit 1
fi
echo "backup written: $OUT ($BYTES bytes)"

if [ -n "$S3_PREFIX" ]; then
    if command -v aws >/dev/null 2>&1; then
        aws s3 cp "$OUT" "$S3_PREFIX/app-db-$STAMP.sql.gz" --only-show-errors
        echo "mirrored to $S3_PREFIX/app-db-$STAMP.sql.gz"
    else
        echo "S3_PREFIX set but no aws CLI on this host; local copy only" >&2
    fi
fi

# Rotate: keep the newest $KEEP_LOCAL local dumps.
ls -1t "$BACKUP_DIR"/app-db-*.sql.gz 2>/dev/null | tail -n +$((KEEP_LOCAL + 1)) | xargs -r rm --
