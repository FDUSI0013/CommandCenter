#!/usr/bin/env bash
# FD AI Command Center — build the three private engine images.
#
#   fulcrum-ops/telemetry-engine   the telemetry service (Java)
#   fulcrum-ops/metric-runner      runs user-defined metric code in a sandbox
#   fulcrum-ops/safety-scanner     the guardrail inference service
#
# None of these is publicly addressable in a deployment: they sit on the compose
# network behind the control plane. They are built here rather than pulled so
# the de-branding patch in engine/patch-vendor.sh is part of the artefact, not
# something applied at run time that an operator could forget.
#
# Usage:
#   engine/build.sh                       build locally, tag :dev
#   ENGINE_TAG=1.0.0 engine/build.sh      build and tag a release
#   REGISTRY=…dkr.ecr.… PUSH=1 engine/build.sh    build, tag and push
#
# Requires Docker with buildx (any recent Docker Engine). Nothing else.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$HERE/vendor"
TAG="${ENGINE_TAG:-dev}"
REGISTRY="${REGISTRY:-}"
PUSH="${PUSH:-0}"
PLATFORM="${PLATFORM:-linux/amd64}"

prefix() { if [ -n "$REGISTRY" ]; then echo "$REGISTRY/fulcrum-ops"; else echo "fulcrum-ops"; fi; }

command -v docker >/dev/null || { echo "error: docker is not installed" >&2; exit 1; }
[ -d "$VENDOR/apps" ] || { echo "error: run engine/fetch-vendor.sh first" >&2; exit 1; }

# The patch is idempotent, so running it here means a build is always made from
# patched source even if someone skips the step by hand.
bash "$HERE/patch-vendor.sh" "$VENDOR"

APPS="$VENDOR/apps"
ENGINE_DIR="$(find "$APPS" -maxdepth 1 -type d -exec test -f '{}/config.yml' -a -f '{}/pom.xml' ';' -print | head -1)"
METRICS_DIR="$(find "$APPS" -maxdepth 1 -type d -name '*python-backend*' | head -1)"
SAFETY_DIR="$(find "$APPS" -maxdepth 1 -type d -name '*guardrails*' | head -1)"

VERSION="$(cat "$VENDOR/version.txt" 2>/dev/null || echo "0.0.0")"

build() {
  local dir="$1" name="$2" dockerfile="${3:-Dockerfile}" extra="${4:-}"
  [ -n "$dir" ] && [ -d "$dir" ] || { echo "skip: $name (source not found)"; return 0; }
  local image; image="$(prefix)/$name:$TAG"
  echo
  echo "=== $name → $image ==============================================="
  # shellcheck disable=SC2086 — extra is a deliberate word-split of build args
  docker build \
    --platform "$PLATFORM" \
    --file "$dir/$dockerfile" \
    --tag "$image" \
    --label "org.opencontainers.image.version=$TAG" \
    --label "org.opencontainers.image.vendor=FD AI Command Center" \
    $extra \
    "$dir"
  if [ "$PUSH" = "1" ]; then
    [ -n "$REGISTRY" ] || { echo "error: PUSH=1 needs REGISTRY" >&2; exit 1; }
    docker push "$image"
  fi
}

# The engine's Dockerfile takes its version through a build argument whose name
# is upstream's. Read it out of the Dockerfile rather than hard-coding it, so
# this script names nothing it does not own and keeps working if it is renamed.
VERSION_ARG="$(grep -hoE '^ARG[[:space:]]+[A-Za-z_][A-Za-z0-9_]*_VERSION' "$ENGINE_DIR/Dockerfile" \
                 | head -1 | awk '{print $2}')"
ENGINE_BUILD_ARGS=""
[ -n "$VERSION_ARG" ] && ENGINE_BUILD_ARGS="--build-arg $VERSION_ARG=$VERSION"

build "$ENGINE_DIR"  "telemetry-engine" "Dockerfile" "$ENGINE_BUILD_ARGS"
build "$METRICS_DIR" "metric-runner"    "Dockerfile"
# The guardrail service ships a GPU image and a CPU one; a single-host
# deployment has no GPU, so the CPU variant is what we build when it exists.
if [ -n "$SAFETY_DIR" ] && [ -f "$SAFETY_DIR/Dockerfile.cpu" ]; then
  build "$SAFETY_DIR" "safety-scanner" "Dockerfile.cpu"
else
  build "$SAFETY_DIR" "safety-scanner" "Dockerfile"
fi

echo
echo "built at tag $TAG:"
docker images --filter "reference=$(prefix)/*:$TAG" --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'

# ---------------------------------------------------------------- egress check
# Prove the built engine cannot report home: the image's own configuration must
# have reporting pinned off. Cheap, and it fails the build rather than a review.
echo
echo "verifying the built image does not report outbound…"
if docker run --rm --entrypoint sh "$(prefix)/telemetry-engine:$TAG" -c \
     'awk "/^usageReport:/{f=1} f&&/^[[:space:]]+enabled:/{print \$2; exit}" config.yml' \
     2>/dev/null | grep -qx 'false'; then
  echo "  ok — outbound reporting is disabled in the image"
else
  echo "  FAILED — the image does not have reporting pinned off" >&2
  exit 1
fi
