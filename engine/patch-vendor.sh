#!/usr/bin/env bash
# Fulcrum Ops — prepare the vendored telemetry engine source for our build.
#
# The engine is an Apache-2.0 upstream component we redistribute under our own
# product name. Two things have to be true before it is built:
#
#   1. it must not phone home — upstream ships anonymous usage reporting that is
#      enabled by default and posts to a third-party endpoint;
#   2. nothing an operator sees at runtime may name the upstream vendor — image
#      labels, start-up logs and the service banner all get our name instead.
#
# Everything here is done by matching *structure* (a YAML key, a label
# directive, an echo line) rather than by naming the upstream project, so this
# script contains no vendor string of its own. It is idempotent: running it
# twice changes nothing the second time, and it verifies its own work.
#
# Usage:  engine/patch-vendor.sh [vendor-dir]      (default: engine/vendor)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="${1:-$HERE/vendor}"
PRODUCT="Fulcrum Ops"
DISABLED_URL="http://127.0.0.1:9/disabled"

if [ ! -d "$VENDOR" ]; then
  echo "error: no vendored source at $VENDOR — run engine/fetch-vendor.sh first" >&2
  exit 1
fi

# The upstream tree may be nested one level inside the archive's own folder.
if [ ! -d "$VENDOR/apps" ] && [ -d "$(find "$VENDOR" -maxdepth 2 -type d -name apps | head -1)" ]; then
  VENDOR="$(dirname "$(find "$VENDOR" -maxdepth 2 -type d -name apps | head -1)")"
fi

APPS="$VENDOR/apps"
[ -d "$APPS" ] || { echo "error: $VENDOR does not look like the engine source (no apps/)" >&2; exit 1; }

# Discover the three components by shape rather than by name, so this keeps
# working if the upstream directory names change.
ENGINE_DIR="$(find "$APPS" -maxdepth 1 -type d -exec test -f '{}/config.yml' -a -f '{}/pom.xml' ';' -print | head -1)"
SANDBOX_DIR="$(find "$APPS" -maxdepth 1 -type d -name '*sandbox*' | head -1)"
SAFETY_DIR="$(find "$APPS" -maxdepth 1 -type d -name '*guardrails*' | head -1)"
METRICS_DIR="$(find "$APPS" -maxdepth 1 -type d -name '*python-backend*' | head -1)"

[ -n "$ENGINE_DIR" ] || { echo "error: could not locate the engine component under $APPS" >&2; exit 1; }

echo "vendored source : $VENDOR"
echo "engine          : ${ENGINE_DIR#"$VENDOR"/}"
echo "metric runner   : ${METRICS_DIR:-(not found)}"
echo "safety scanner  : ${SAFETY_DIR:-(not found)}"
echo "sandbox         : ${SANDBOX_DIR:-(not found)}"
echo

CONFIG="$ENGINE_DIR/config.yml"
[ -f "$CONFIG" ] || { echo "error: $CONFIG missing" >&2; exit 1; }

# ---------------------------------------------------------------- no egress
# Both blocks are keyed by their YAML parent, and the value is replaced with a
# literal so no environment variable can switch them back on later. awk rather
# than a YAML library so this runs on a bare build host with nothing installed.
awk -v disabled_url="$DISABLED_URL" '
  # A line with no leading whitespace that ends in ":" opens a new top-level block.
  /^[A-Za-z_][A-Za-z0-9_]*:[[:space:]]*$/ { block = substr($0, 1, index($0, ":") - 1) }

  (block == "usageReport" || block == "analytics") && /^[[:space:]]+enabled:/ {
    match($0, /^[[:space:]]+/)
    print substr($0, 1, RLENGTH) "enabled: false"
    changed[block "/enabled"] = 1
    next
  }
  block == "usageReport" && /^[[:space:]]+url:/ {
    match($0, /^[[:space:]]+/)
    print substr($0, 1, RLENGTH) "url: " disabled_url
    changed["usageReport/url"] = 1
    next
  }
  { print }

  END {
    if (!changed["usageReport/enabled"] || !changed["usageReport/url"]) {
      print "error: could not pin the reporting block" > "/dev/stderr"
      exit 1
    }
  }
' "$CONFIG" > "$CONFIG.patched" && mv "$CONFIG.patched" "$CONFIG"
echo "  config: outbound reporting disabled and pinned"

# ------------------------------------------------------------------ labels
# Replace every OCI label value with ours, matching the directive not the value.
find "$APPS" -maxdepth 2 -name 'Dockerfile*' -print0 | while IFS= read -r -d '' dockerfile; do
  if grep -qi '^LABEL org.opencontainers.image' "$dockerfile"; then
    sed -i \
      -e "s|^LABEL org.opencontainers.image.title=.*|LABEL org.opencontainers.image.title=\"${PRODUCT} Telemetry Engine\"|I" \
      -e "s|^LABEL org.opencontainers.image.description=.*|LABEL org.opencontainers.image.description=\"${PRODUCT} telemetry engine — private service, not externally addressable\"|I" \
      -e "s|^LABEL org.opencontainers.image.vendor=.*|LABEL org.opencontainers.image.vendor=\"${PRODUCT}\"|I" \
      "$dockerfile"
    echo "  labels: ${dockerfile#"$VENDOR"/}"
  fi
done

# -------------------------------------------------------------- start-up log
# The entrypoint echoes its own version banner. An operator tailing container
# logs is the one person who sees it, so it says our name.
find "$APPS" -maxdepth 2 -name 'entrypoint.sh' -print0 | while IFS= read -r -d '' entry; do
  # Drop the tracing-agent debug echoes first: they also match the version
  # pattern below, and one banner per start-up is the point.
  sed -i '/^echo "[A-Z_]*OTEL_SDK_ENABLED=/d;/^echo "OTEL_VERSION=/d' "$entry"
  if grep -q '^echo "[A-Z_]*VERSION=' "$entry"; then
    sed -i "s|^echo \"\([A-Z_]*\)VERSION=\(.*\)\"|echo \"${PRODUCT} telemetry engine, build \2\"|" "$entry"
    echo "  banner: ${entry#"$VENDOR"/}"
  fi
  # "Starting <upstream-name> service…" — the service has our name in our logs.
  sed -i -E "s|^(echo \"Starting )[A-Za-z0-9_.-]+( service)|\1the telemetry engine\2|" "$entry"
done

# ----------------------------------------------------------- config comments
# The reporting block documents its upstream default endpoint in a comment.
# The value is pinned above; the comment is scrubbed so a reader of the running
# container's config is not pointed at an endpoint this build never calls.
sed -i -E 's|^([[:space:]]*#[[:space:]]*Default:[[:space:]]*)https?://[A-Za-z0-9.-]+/notify/event/?[[:space:]]*$|\1(disabled in this build)|' "$CONFIG"

# ------------------------------------------------------------------ verify
echo
fail=0
if ! awk '
  /^[A-Za-z_][A-Za-z0-9_]*:[[:space:]]*$/ { block = substr($0, 1, index($0, ":") - 1) }
  block == "usageReport" && /^[[:space:]]+enabled:[[:space:]]*false[[:space:]]*$/ { ok = 1 }
  END { exit ok ? 0 : 1 }
' "$CONFIG"; then
  echo "VERIFY FAILED: usage reporting is not pinned off" >&2; fail=1
fi
if awk '
  /^[A-Za-z_][A-Za-z0-9_]*:[[:space:]]*$/ { block = substr($0, 1, index($0, ":") - 1) }
  block == "usageReport" && /^[[:space:]]+url:/ && $0 !~ /127\.0\.0\.1/ { bad = 1 }
  END { exit bad ? 0 : 1 }
' "$CONFIG"; then
  echo "VERIFY FAILED: the reporting URL still points off-host" >&2; fail=1
fi
if [ "$fail" -ne 0 ]; then exit 1; fi

echo "vendor source patched: no outbound reporting, our labels, our banner."
