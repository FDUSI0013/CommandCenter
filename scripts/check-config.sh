#!/usr/bin/env bash
# Fulcrum Ops — validate deployment configuration before it reaches a host.
#
# A malformed XML file does not fail loudly at deploy time: the analytics store
# starts, refuses to merge the config, exits, and restarts — forever — while
# compose reports it as merely "unhealthy". The loop costs minutes to diagnose
# on a remote host and seconds to prevent here.
#
# Checks:
#   - every XML file under deploy/ is well-formed (XML comments may not contain
#     "--", which is easy to write in a decorative rule and invalid);
#   - the compose file parses and names no image without a tag;
#   - every shell script under deploy/ and engine/ parses.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

py() { command -v python3 >/dev/null 2>&1 && python3 "$@" || python "$@"; }

echo "== XML =="
while IFS= read -r -d '' file; do
  if py -c "import sys,xml.dom.minidom as m; m.parse(sys.argv[1])" "$file" 2>/dev/null; then
    echo "  ok   ${file#"$ROOT"/}"
  else
    echo "  FAIL ${file#"$ROOT"/}"
    py -c "import sys,xml.dom.minidom as m; m.parse(sys.argv[1])" "$file" 2>&1 | tail -1 | sed 's/^/       /'
    fail=1
  fi
done < <(find "$ROOT/deploy" -name '*.xml' -print0 2>/dev/null)

echo
echo "== compose =="
if py -c "
import sys, re, pathlib
text = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')
untagged = [l.strip() for l in text.splitlines()
            if re.match(r'^\s*image:\s*\S+$', l) and ':' not in l.split('image:', 1)[1].strip().rsplit('/', 1)[-1]]
if untagged:
    print('images without an explicit tag:'); [print('  ', u) for u in untagged]; sys.exit(1)
print('  ok   every image is tagged')
" "$ROOT/deploy/docker-compose.yml"; then :; else fail=1; fi

echo
echo "== shell =="
while IFS= read -r -d '' file; do
  if bash -n "$file" 2>/dev/null; then
    echo "  ok   ${file#"$ROOT"/}"
  else
    echo "  FAIL ${file#"$ROOT"/}"; bash -n "$file" 2>&1 | sed 's/^/       /'; fail=1
  fi
done < <(find "$ROOT/deploy" "$ROOT/engine" "$ROOT/scripts" -name '*.sh' -print0 2>/dev/null)

echo
if [ "$fail" -eq 0 ]; then echo "CONFIG CHECK PASSED"; else echo "CONFIG CHECK FAILED" >&2; fi
exit "$fail"
