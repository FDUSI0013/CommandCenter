"""Version 1 of the public API.

Every console screen and both SDKs talk to this tree and nothing else. Routers
are mounted in the order the console's navigation presents them, which keeps
``/api/docs`` readable as a product tour rather than an alphabetical dump.

Adding a domain is one import and one ``include_router`` line; the prefix and
tags belong to the domain module so a router can be mounted anywhere without
editing it.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    agents,
    alerts,
    approvals,
    audit,
    auth,
    configurations,
    connections,
    connectors,
    deployments,
    evaluations,
    exports,
    feedback,
    guardrails,
    ingest,
    knowledge,
    licensing,
    llm_usage,
    memory,
    metrics,
    policies,
    prompts,
    quota,
    runs,
    secrets,
    testing,
    workspaces,
)

router = APIRouter()

# -- identity ---------------------------------------------------------------
router.include_router(auth.router)
router.include_router(workspaces.router)

# -- platform ---------------------------------------------------------------
router.include_router(runs.router)
router.include_router(metrics.router)
router.include_router(llm_usage.router)

# -- agent governance -------------------------------------------------------
router.include_router(connections.router)
router.include_router(agents.router)
router.include_router(connectors.router)
router.include_router(policies.router)
router.include_router(approvals.router)
router.include_router(audit.router)

# -- configuration ----------------------------------------------------------
router.include_router(configurations.router)
router.include_router(prompts.router)
router.include_router(knowledge.router)
router.include_router(secrets.router)

# -- operations -------------------------------------------------------------
router.include_router(quota.router)
router.include_router(memory.router)
router.include_router(deployments.router)

# -- quality ----------------------------------------------------------------
router.include_router(evaluations.router)
router.include_router(guardrails.router)
router.include_router(testing.router)
router.include_router(feedback.router)

# -- system -----------------------------------------------------------------
router.include_router(alerts.router)
router.include_router(exports.router)
router.include_router(licensing.router)

# -- machine surface: how a customer's agent reports in --------------------
router.include_router(ingest.router)

__all__ = ["router"]
