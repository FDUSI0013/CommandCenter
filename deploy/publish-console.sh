#!/usr/bin/env bash
# Fulcrum Ops — publish the console's static files to the edge's document root.
#
# The obvious `cp -r apps/web/. /srv/fulcrum-ops/` is wrong, and wrong in a way
# that hides: it overwrites what changed and *leaves behind what was deleted*.
# A file removed from the repository keeps being served, so a browser that still
# references it — or a stale cached page that does — gets a working response for
# something that no longer exists. That is how a console ends up running half of
# one build and half of another.
#
# This mirrors the directory instead: after it runs, the document root contains
# exactly what the repository contains, and nothing else.
#
# Usage:  deploy/publish-console.sh [source] [docroot]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$HERE/../apps/web}"
DOCROOT="${2:-/srv/fulcrum-ops}"

[ -f "$SOURCE/index.html" ] || { echo "error: $SOURCE has no index.html" >&2; exit 1; }

install -d "$DOCROOT"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude='.git' "$SOURCE"/ "$DOCROOT"/
else
  # No rsync on a minimal host: empty the root first, which gets the same
  # result. The console is a few hundred kilobytes, so the copy is cheap and
  # the window where the root is incomplete is milliseconds.
  find "$DOCROOT" -mindepth 1 -delete
  cp -r "$SOURCE"/. "$DOCROOT"/
fi

echo "published $(find "$DOCROOT" -type f | wc -l) files to $DOCROOT"

# Anything the repository no longer ships must no longer be served. This is the
# check that would have caught the old fixture lingering after it was deleted.
stale=0
while IFS= read -r -d '' served; do
  rel="${served#"$DOCROOT"/}"
  if [ ! -f "$SOURCE/$rel" ]; then
    echo "  STALE: $rel is served but is not in the source" >&2
    stale=1
  fi
done < <(find "$DOCROOT" -type f -print0)

if [ "$stale" -ne 0 ]; then
  echo "publish failed: the document root holds files the source does not" >&2
  exit 1
fi

echo "document root matches the source exactly"
