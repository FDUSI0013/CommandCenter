"""Contract check: does the console's API client match the server's OpenAPI document?

The console calls the control plane through one file — ``apps/web/js/api.js`` — so the
set of paths that file constructs is exactly the set of paths the server must serve.
This script extracts both and reports the difference in each direction:

* **missing** — the console calls it, the server does not serve it. A broken screen.
* **unused** — the server serves it, the console never calls it. Not an error (the SDK
  and CI use endpoints the console does not), but worth reading once per release.

Run it after any change to a router or to ``api.js``::

    .venv/Scripts/python.exe scripts/check_contract.py

Exit status is 1 when anything is missing, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve()
CONTROL_PLANE = HERE.parent.parent
REPO = CONTROL_PLANE.parent.parent
API_JS = REPO / "apps" / "web" / "js" / "api.js"

# `/agents/${encodeURIComponent(id)}/run` and `/agents` — the two shapes api.js uses.
TEMPLATE = re.compile(r"[`'\"](/(?:[A-Za-z0-9_\-./]|\$\{[^}]*\})+)[`'\"]")
INTERPOLATION = re.compile(r"\$\{[^}]*\}")


def normalise(path: str) -> str:
    """Reduce a JS template or an OpenAPI path to one comparable form."""
    path = INTERPOLATION.sub("{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


def console_paths(source: str) -> set[str]:
    found: set[str] = set()
    for match in TEMPLATE.finditer(source):
        raw = match.group(1)
        # api.js also holds literals that are not endpoints (the base path, a
        # download filename). Endpoints always start with a domain segment.
        if raw.startswith("//") or raw.count("/") == 0:
            continue
        # The client's own base path is not an endpoint it calls.
        if raw.rstrip("/") in {"/api/v1", "/api"}:
            continue
        found.add(normalise(raw))
    return found


def server_paths(prefix: str) -> set[str]:
    sys.path.insert(0, str(CONTROL_PLANE / "src"))
    from fulcrum_ops_api.main import app  # noqa: PLC0415 — import after sys.path setup

    out: set[str] = set()
    for path in app.openapi()["paths"]:
        if path.startswith(prefix):
            out.add(normalise(path[len(prefix) :]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="/api/v1")
    parser.add_argument("--show-unused", action="store_true")
    args = parser.parse_args()

    console = console_paths(API_JS.read_text(encoding="utf-8"))
    server = server_paths(args.prefix)

    missing = sorted(console - server)
    unused = sorted(server - console)

    print(f"console calls {len(console)} paths · server serves {len(server)} paths\n")

    if missing:
        print(f"MISSING — called by the console, not served ({len(missing)}):")
        for path in missing:
            print("  ", path)
    else:
        print("MISSING — none. Every path the console calls is served.")

    if args.show_unused and unused:
        print(f"\nunused by the console ({len(unused)}) — SDK/CI surface, or dead:")
        for path in unused:
            print("  ", path)

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
