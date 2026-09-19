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

# Stamp the PUBLISHED index.html — not the one in the repository. The console
# has no build step, so its files keep fixed names (js/app.js, not
# js/app.<hash>.js) and a browser cannot tell a cached copy from the current one
# by URL. Appending ?v=<build> is what actually invalidates a cache; the edge
# then serves the stamped URLs with a year-long max-age and index.html itself
# with no-cache, so one revalidated page hands out the new stamps for everything
# else. The control plane does the same rewrite in main.py when it is the one
# serving the console, and the regex here is the same.
#
# The build id is a digest of what is being served, so it moves when an asset
# changes and only then. A stamp that moved on every publish would throw a warm
# cache away each deploy; one that did NOT move when a file did — a commit id
# published from a dirty tree — would pin a year-long cache to stale JavaScript.
# index.html is left out of the digest: it is the file being rewritten, and it
# is always revalidated anyway.
BUILD=""
if command -v sha256sum >/dev/null 2>&1; then
  BUILD="$(find "$DOCROOT" -type f ! -name index.html -print0 \
             | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)" || BUILD=""
fi
# No digest tool (or a sort without -z): fall back to a stamp that is at least
# never wrong, only pessimistic — every publish looks like new assets.
[ -n "$BUILD" ] || BUILD="$(date -u +%Y%m%d%H%M%S)"

# Written through a temporary file rather than `sed -i`, which is GNU-only and
# this also runs on a workstation. `[^"?]+` skips a URL that already carries a
# query, so publishing twice cannot stamp twice.
sed -E 's/((src|href)="(js|css)\/[^"?]+)"/\1?v='"$BUILD"'"/g' \
  "$DOCROOT/index.html" > "$DOCROOT/index.html.stamping"
mv "$DOCROOT/index.html.stamping" "$DOCROOT/index.html"
echo "stamped the assets index.html references with ?v=$BUILD"

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
