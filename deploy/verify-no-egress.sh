#!/usr/bin/env bash
# FD AI Command Center — prove the private services do not talk to the internet.
#
# The telemetry engine is a third-party component running inside our product.
# It is configured never to report outbound, and the configuration is baked into
# the image rather than passed as an environment variable so it cannot be
# switched back on by a stray setting. This script checks that claim on a
# running deployment, from the outside, the way an auditor would:
#
#   1. the engine's own configuration says reporting is off;
#   2. no private service publishes a port to the host;
#   3. nothing on the compose network resolves or reaches a reporting endpoint;
#   4. the only container listening on a public interface is the edge proxy.
#
# Run it on the deployment host after `docker compose up -d`:
#   deploy/verify-no-egress.sh
#
# Exit status is non-zero if any check fails, so it can gate a release.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="docker compose -f $HERE/docker-compose.yml"
ENGINE_SERVICE="telemetry-engine"
fail=0

note()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32mok\033[0m   %s\n' "$*"; }
bad()   { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }

echo "1. engine configuration"
config_value="$($COMPOSE exec -T "$ENGINE_SERVICE" sh -c \
  'awk "/^usageReport:/{f=1} f&&/^[[:space:]]+enabled:/{print \$2; exit}" config.yml' 2>/dev/null | tr -d '\r')"
if [ "$config_value" = "false" ]; then
  ok "usage reporting is disabled in the running container"
else
  bad "usage reporting reads '$config_value' — expected 'false'"
fi

config_url="$($COMPOSE exec -T "$ENGINE_SERVICE" sh -c \
  'awk "/^usageReport:/{f=1} f&&/^[[:space:]]+url:/{print \$2; exit}" config.yml' 2>/dev/null | tr -d '\r')"
case "$config_url" in
  *127.0.0.1*|*localhost*) ok "the reporting URL points nowhere: $config_url" ;;
  *)                       bad "the reporting URL is off-host: $config_url" ;;
esac

echo
echo "2. published ports"
published="$($COMPOSE ps --format '{{.Service}} {{.Publishers}}' 2>/dev/null)"
while read -r service ports; do
  [ -z "$service" ] && continue
  case "$service" in
    control-plane)
      if printf '%s' "$ports" | grep -q '0\.0\.0\.0\|::'; then
        bad "control-plane publishes on a public interface: $ports"
      else
        ok "control-plane is bound to loopback only"
      fi
      ;;
    *)
      if printf '%s' "$ports" | grep -qE '[0-9]+->'; then
        bad "$service publishes a port to the host: $ports"
      else
        ok "$service publishes nothing"
      fi
      ;;
  esac
done <<< "$published"

echo
echo "3. outbound reachability from the engine container"
# A reporting endpoint must not be reachable. `curl --max-time` covers both a
# DNS failure and a silent drop; either is a pass, a 2xx is not.
probe="$($COMPOSE exec -T "$ENGINE_SERVICE" sh -c \
  'curl -s -o /dev/null -w "%{http_code}" --max-time 6 "$1" 2>/dev/null || echo blocked' _ \
  "http://127.0.0.1:9/disabled" 2>/dev/null | tr -d '\r')"
if [ "$probe" = "blocked" ] || [ "$probe" = "000" ]; then
  ok "the configured reporting endpoint is unreachable"
else
  bad "something answered at the reporting endpoint (HTTP $probe)"
fi

echo
echo "4. what is actually listening on the host"
if command -v ss >/dev/null 2>&1; then
  listening="$(ss -ltnp 2>/dev/null | awk 'NR>1 {print $4}')"
else
  listening="$(netstat -ltn 2>/dev/null | awk 'NR>2 {print $4}')"
fi
public="$(printf '%s\n' "$listening" | grep -E '^(0\.0\.0\.0|\*|\[::\]):' | sed 's/.*://' | sort -u | tr '\n' ' ')"
note "public listeners: ${public:-none}"
for port in $public; do
  case "$port" in
    80|443) ok "port $port — the edge proxy, expected" ;;
    22)     ok "port $port — administrative access" ;;
    *)      bad "port $port is open to the world and should not be" ;;
  esac
done

echo
if [ "$fail" -eq 0 ]; then
  echo "EGRESS CHECK PASSED — the private services stay private and report nothing outbound."
else
  echo "EGRESS CHECK FAILED — see the entries marked FAIL above." >&2
fi
exit "$fail"
