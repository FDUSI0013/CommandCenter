"""Call every read endpoint a deployment publishes, and say how each one did.

    python scripts/verify_live.py https://controlplane.example.com
    FULCRUM_OPS_API_KEY=fo_live_...   (or --key-file PATH --key-name NAME)

"Is everything working?" deserves an answer with evidence in it. This reads the
deployment's own OpenAPI document, so the list cannot drift from what is served,
and issues one GET per operation: collection reads as they are, item reads with
an id taken from the collection next to them. It writes nothing.

Each answer is one of:

    ok       2xx
    gated    401/403 - the key's role does not reach it; not a defect
    empty    404 on an item read we had no real id for
    params   the operation REQUIRES a query parameter (compare needs two ids, a
             diff needs two versions); the sweep will not invent one
    SLOW     2xx, but over the --slow threshold (default 3 s)
    FAIL     5xx, a timeout, or a 4xx that is none of the above

Exit status is the number of FAILs, so it can gate a release.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any

import httpx

ITEM = re.compile(r"\{(\w+)\}")
#: Reads that stream or block by design; probing them says nothing useful.
SKIP = ("/stream", "/download", "/export")


def load_key(args: argparse.Namespace) -> str:
    if os.environ.get("FULCRUM_OPS_API_KEY"):
        return os.environ["FULCRUM_OPS_API_KEY"]
    if args.key_file:
        with open(args.key_file, encoding="utf-8") as handle:
            for line in handle:
                name, _, value = line.strip().partition("=")
                if name == args.key_name and value:
                    return value.strip().strip('"')
    sys.exit("no API key: set FULCRUM_OPS_API_KEY, or pass --key-file and --key-name")


def first_id(body: Any) -> str | None:
    rows = body.get("items") if isinstance(body, dict) else body
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        value = rows[0].get("id")
        return str(value) if value else None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("--key-file")
    parser.add_argument("--key-name", default="FULCRUM_OPS_API_KEY")
    parser.add_argument("--ca-bundle", help="PEM bundle, for a TLS-inspecting corporate proxy")
    parser.add_argument("--slow", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=35.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    client = httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {load_key(args)}"},
        timeout=args.timeout,
        verify=args.ca_bundle or True,
    )

    health = client.get("/health")
    print(f"/health -> {health.status_code} {health.text[:300]}\n")

    spec = client.get("/api/openapi.json").json()
    reads = sorted(path for path, ops in spec["paths"].items() if "get" in ops)

    def needs_query(path: str) -> bool:
        declared = spec["paths"][path]["get"].get("parameters", [])
        return any(p.get("in") == "query" and p.get("required") for p in declared)

    collections = [p for p in reads if not ITEM.search(p) and not p.endswith(SKIP)]
    items = [p for p in reads if ITEM.search(p) and not p.endswith(SKIP)]

    rows: list[tuple[str, str, int | str, float]] = []
    ids: dict[str, str] = {}

    def probe(path: str, label: str) -> None:
        started = time.perf_counter()
        try:
            response = client.get(path, params={"page_size": 5})
            status: int | str = response.status_code
        except httpx.HTTPError as exc:
            response, status = None, type(exc).__name__
        seconds = time.perf_counter() - started
        if isinstance(status, int) and 200 <= status < 300:
            verdict = "SLOW" if seconds > args.slow else "ok"
            if response is not None and "json" in response.headers.get("content-type", ""):
                try:
                    found = first_id(response.json())
                except ValueError:  # a JSON content type on an empty body
                    found = None
                if found:
                    ids.setdefault(label, found)
        elif status in (401, 403):
            verdict = "gated"
        elif status == 404 and label != path:
            verdict = "empty"
        else:
            verdict = "FAIL"
        rows.append((verdict, label, status, seconds))

    for path in collections:
        if needs_query(path):
            rows.append(("params", path, "needs query", 0.0))
            continue
        probe(path, path)
    for template in items:
        if needs_query(template):
            rows.append(("params", template, "needs query", 0.0))
            continue
        parent = template.split("/{", 1)[0]
        known = ids.get(parent)
        if len(ITEM.findall(template)) != 1 or not known:
            rows.append(("empty", template, "no id", 0.0))
            continue
        probe(ITEM.sub(known, template), template)

    order = {"FAIL": 0, "SLOW": 1, "gated": 2, "params": 3, "empty": 4, "ok": 5}
    rows.sort(key=lambda row: (order[row[0]], -row[3]))
    for verdict, label, status, seconds in rows:
        if verdict in ("FAIL", "SLOW"):
            print(f"  {verdict:<5} {status!s:<14} {seconds:6.2f}s  {label}")
    tally = {name: sum(1 for row in rows if row[0] == name) for name in order}
    timed = sorted(row[3] for row in rows if row[0] in ("ok", "SLOW"))
    if timed:
        print(
            f"\nlatency over {len(timed)} successful reads: "
            f"p50 {timed[len(timed) // 2]:.2f}s  p95 {timed[int(len(timed) * 0.95)]:.2f}s  max {timed[-1]:.2f}s"
        )
    print("  ".join(f"{name}={count}" for name, count in tally.items()), f" of {len(rows)} read operations")
    return tally["FAIL"]


if __name__ == "__main__":
    sys.exit(main())
