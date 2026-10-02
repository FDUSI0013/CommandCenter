#!/usr/bin/env bash
# FD AI Command Center — place the telemetry engine's upstream source where the build
# can reach it.
#
# The source is not committed: it is a large third-party tree that we neither
# author nor modify in place, and keeping it out of the repository is what makes
# "no vendor string in our code" a checkable property rather than a hope. It is
# fetched here, patched by engine/patch-vendor.sh, and built by engine/build.sh.
#
# Usage:
#   engine/fetch-vendor.sh <archive.zip|archive.tar.gz|directory>
#
# The archive is whatever release of the engine this product is pinned to; the
# expected digest lives in engine/vendor.lock and is verified before unpacking,
# so a build cannot silently pick up a different upstream than the one that was
# reviewed.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$HERE/vendor"
LOCK="$HERE/vendor.lock"
SOURCE="${1:-}"

if [ -z "$SOURCE" ]; then
  echo "usage: engine/fetch-vendor.sh <archive.zip|archive.tar.gz|directory>" >&2
  exit 2
fi
[ -e "$SOURCE" ] || { echo "error: $SOURCE does not exist" >&2; exit 1; }

# ------------------------------------------------------------------ digest
if [ -f "$SOURCE" ]; then
  actual="$(sha256sum "$SOURCE" | awk '{print $1}')"
  if [ -f "$LOCK" ]; then
    expected="$(awk '/^sha256[[:space:]]/ {print $2}' "$LOCK")"
    if [ -n "$expected" ] && [ "$expected" != "$actual" ]; then
      echo "error: archive digest does not match engine/vendor.lock" >&2
      echo "  expected $expected" >&2
      echo "  actual   $actual" >&2
      echo "Update the lock deliberately if this is an intended upgrade." >&2
      exit 1
    fi
  else
    printf 'sha256 %s\nfile   %s\n' "$actual" "$(basename "$SOURCE")" > "$LOCK"
    echo "recorded digest in engine/vendor.lock: $actual"
  fi
fi

# ------------------------------------------------------------------ unpack
rm -rf "$VENDOR"
mkdir -p "$VENDOR"

case "$SOURCE" in
  *.zip)
    command -v unzip >/dev/null || { echo "error: unzip is not installed" >&2; exit 1; }
    unzip -q "$SOURCE" -d "$VENDOR"
    ;;
  *.tar.gz|*.tgz)
    tar -xzf "$SOURCE" -C "$VENDOR"
    ;;
  *)
    [ -d "$SOURCE" ] || { echo "error: $SOURCE is neither an archive nor a directory" >&2; exit 1; }
    cp -r "$SOURCE"/. "$VENDOR"/
    ;;
esac

# Archives usually carry one top-level folder; flatten it so every later step
# can assume engine/vendor/apps exists.
if [ ! -d "$VENDOR/apps" ]; then
  inner="$(find "$VENDOR" -mindepth 1 -maxdepth 1 -type d | head -1)"
  if [ -n "$inner" ] && [ -d "$inner/apps" ]; then
    mv "$inner"/* "$inner"/.[!.]* "$VENDOR"/ 2>/dev/null || true
    rmdir "$inner" 2>/dev/null || true
  fi
fi

[ -d "$VENDOR/apps" ] || { echo "error: unpacked tree has no apps/ directory" >&2; exit 1; }

# The licence we are obliged to carry forward is copied out of the tree and into
# the repository, because the tree itself is not committed.
for candidate in LICENSE LICENSE.txt LICENCE COPYING; do
  if [ -f "$VENDOR/$candidate" ]; then
    cp "$VENDOR/$candidate" "$HERE/../THIRD_PARTY_LICENCE.txt"
    echo "licence copied to THIRD_PARTY_LICENCE.txt"
    break
  fi
done

echo "vendored source ready at engine/vendor"
echo "next: engine/patch-vendor.sh && engine/build.sh"
