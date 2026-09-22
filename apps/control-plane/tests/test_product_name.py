"""The product is FD AI Command Center, and the API documents it as such.

The API document is read by people -- /api/docs renders every title, summary
and description in it -- so it is held to the same rule as the console and the
operator manual: the old product names do not appear. Technical identifiers are
exempt on purpose (the fulcrum_ops package, FULCRUM_OPS_* variables, X-Fulcrum-*
headers): running agents depend on them. None of those contains either phrase
below, so the check needs no exceptions.
"""

from __future__ import annotations

import json

OLD_NAMES = ("fulcrum ops", "control plane")


async def test_the_api_document_names_the_product(client):
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200, response.text
    spec = response.json()

    assert spec["info"]["title"] == "FD AI Command Center API"


async def test_the_api_document_never_uses_the_old_names(client):
    response = await client.get("/api/openapi.json")
    text = json.dumps(response.json()).lower()

    found = {name: text.count(name) for name in OLD_NAMES if name in text}
    assert not found, f"old product names in the API document: {found}"
