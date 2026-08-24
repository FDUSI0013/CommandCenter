#!/usr/bin/env bash
# Fulcrum Ops — branding gate.
#
# The telemetry engine underneath this product is a white-labelled third-party
# component. Nothing a customer can see may name it: not the console, not the
# SDKs, not the API contract, not the docs, not an environment variable they set,
# not a log line, not a package name.
#
# This script fails the build if a forbidden string appears anywhere outside the
# two places it is allowed:
#
#   THIRD_PARTY_LICENCES.md   — the Apache-2.0 attribution we are obliged to keep
#   engine/vendor/            — the unmodified upstream source we build from
#
# Both are repository-only. Neither is shipped to a customer or rendered in a UI.
#
# Usage:  scripts/check-branding.sh [path]     (default: repo root)

set -uo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PATTERN='opik|comet'

ALLOWED=(
  ':!THIRD_PARTY_LICENCES.md'
  ':!engine/vendor/**'
  ':!scripts/check-branding.sh'
)

EXCLUDE_DIRS=(
  --glob '!**/node_modules/**'
  --glob '!**/.venv/**'
  --glob '!**/.git/**'
  --glob '!**/dist/**'
  --glob '!**/__pycache__/**'
  --glob '!**/.ruff_cache/**'
  --glob '!**/.mypy_cache/**'
  --glob '!**/.pytest_cache/**'
  --glob '!THIRD_PARTY_LICENCES.md'
  --glob '!engine/vendor/**'
  --glob '!scripts/check-branding.sh'
)

if command -v rg >/dev/null 2>&1; then
  hits=$(rg --no-heading --line-number --ignore-case "${EXCLUDE_DIRS[@]}" -e "$PATTERN" "$ROOT" 2>/dev/null)
else
  hits=$(grep -rniE "$PATTERN" "$ROOT" \
    --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.git \
    --exclude-dir=dist --exclude-dir=__pycache__ --exclude-dir=.ruff_cache \
    --exclude-dir=.mypy_cache --exclude-dir=.pytest_cache \
    --exclude=THIRD_PARTY_LICENCES.md --exclude=check-branding.sh \
2>/dev/null | grep -v '/engine/vendor/')
fi

if [ -n "$hits" ]; then
  echo "BRANDING CHECK FAILED — forbidden vendor strings found:"
  echo "$hits"
  echo
  echo "Every one of these must be removed or renamed before this can ship."
  exit 1
fi

echo "BRANDING CHECK PASSED — no vendor strings outside the licence file and vendored source."
