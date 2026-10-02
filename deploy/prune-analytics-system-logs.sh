#!/usr/bin/env bash
# FD AI Command Center — one-time: reclaim the disk the analytics store's own diagnostic
# logs were left holding.
#
# deploy/clickhouse/config.d/fulcrum.xml now removes every system log except
# query_log. Removing a log from the configuration stops it being WRITTEN; it
# does not drop the table that is already on disk. On the production host that
# was 16 GB of profiler and metric rows beside 43 MB of actual traces — 12 GB of
# it in processors_profile_log alone. Changing query_log's TTL also makes the
# server set the old table aside as query_log_0 (then _1, ...), and those are
# never cleaned up either.
#
# Run it on the host AFTER the new configuration is live:
#
#   cd /opt/fulcrum/deploy
#   docker compose restart analytics-db            # a bind-mounted file is re-read at start
#   bash prune-analytics-system-logs.sh            # lists what it would drop, and the sizes
#   bash prune-analytics-system-logs.sh --drop     # drops them
#
# It only ever touches MergeTree tables in the `system` database whose name ends
# in _log or _log_<n>, and never query_log (the one log we keep: it is how a slow
# screen is traced to the query behind it) or crash_log (a few rows, and the only
# record of a server crash). No product data lives in `system`. A log that is
# still configured and gets dropped anyway is simply recreated, empty, by the
# server at its next flush.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")
DROP=0
[ "${1:-}" = "--drop" ] && DROP=1

# The statement travels as an argument ($1 inside the container), so nothing in
# it has to survive two layers of shell quoting; the credentials are the
# container's own and never appear on this host's command line.
#
# </dev/null: `exec` attaches stdin even with -T, and inside the loop below it
# would swallow the rest of the table list after the first DROP.
ch() {
  "${COMPOSE[@]}" exec -T analytics-db sh -c \
    'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "$1"' sh "$1" </dev/null
}

LEFTOVERS="database = 'system' AND engine LIKE '%MergeTree%'
  AND match(name, '_log(_[0-9]+)?\$') AND name NOT IN ('query_log', 'crash_log')"

echo "system log tables no longer worth their disk:"
ch "SELECT name, formatReadableSize(ifNull(total_bytes, 0)) FROM system.tables
    WHERE $LEFTOVERS ORDER BY ifNull(total_bytes, 0) DESC FORMAT TSV" | sed 's/^/  /'

tables="$(ch "SELECT name FROM system.tables WHERE $LEFTOVERS ORDER BY name FORMAT TSV")"
if [ -z "$tables" ]; then
  echo "  (none)"
  exit 0
fi

if [ "$DROP" -ne 1 ]; then
  echo
  echo "nothing dropped. Re-run with --drop to reclaim the space."
  exit 0
fi

echo
while IFS= read -r table; do
  table="${table//$'\r'/}"
  case "$table" in
    ''|*[!a-z0-9_]*) echo "  skip: unexpected table name '$table'" >&2; continue ;;
  esac
  echo "  dropping system.$table"
  ch "DROP TABLE IF EXISTS system.\`$table\` SYNC"
done <<< "$tables"

echo
echo "done. What is left in the system database:"
ch "SELECT name, formatReadableSize(ifNull(total_bytes, 0)) FROM system.tables
    WHERE database = 'system' AND engine LIKE '%MergeTree%' ORDER BY name FORMAT TSV" | sed 's/^/  /'
