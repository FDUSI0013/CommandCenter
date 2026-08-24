"""Model registry.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic autogenerate and the test bootstrap rely on. Enum classes are *not*
re-exported at package level — several modules legitimately define their own
``RiskLevel``/``PolicyStatus`` for their own vocabulary, so import those from
their owning module (``models.governance.PolicyStatus``) to keep the meaning
unambiguous.
"""

from __future__ import annotations

from ..db.base import Base
from . import governance, identity, licensing, operations, quality, registry
from .governance import (
    ApprovalComment,
    ApprovalRequest,
    AuditEvent,
    Policy,
    PolicyBinding,
    PolicyViolation,
    Secret,
    SecretAccessLog,
)
from .identity import ApiKey, Membership, Role, User, Workspace
from .licensing import (
    Entitlement,
    Invoice,
    LicensePlan,
    SeatAssignment,
    TenantLicense,
    UsageRecord,
)
from .operations import (
    Alert,
    AlertRule,
    Budget,
    CapacityRecord,
    Deployment,
    DeploymentStage,
    ExportJob,
    ExportSchedule,
    Quota,
)
from .quality import (
    BacklogItem,
    EvaluationRun,
    FeedbackIssue,
    FeedbackItem,
    GuardrailConfig,
    GuardrailEvent,
    Improvement,
    TestRun,
    TestSuite,
)
from .registry import (
    Agent,
    AgentConnector,
    Configuration,
    ConfigurationVersion,
    Connection,
    ConnectionActivity,
    Connector,
    Environment,
    KnowledgeSource,
    MemoryStore,
)

__all__ = [
    "Base",
    # modules, for namespaced enum access
    "governance",
    "identity",
    "licensing",
    "operations",
    "quality",
    "registry",
    # identity
    "ApiKey",
    "Membership",
    "Role",
    "User",
    "Workspace",
    # registry
    "Agent",
    "AgentConnector",
    "Configuration",
    "ConfigurationVersion",
    "Connection",
    "ConnectionActivity",
    "Connector",
    "Environment",
    "KnowledgeSource",
    "MemoryStore",
    # governance
    "ApprovalComment",
    "ApprovalRequest",
    "AuditEvent",
    "Policy",
    "PolicyBinding",
    "PolicyViolation",
    "Secret",
    "SecretAccessLog",
    # operations
    "Alert",
    "AlertRule",
    "Budget",
    "CapacityRecord",
    "Deployment",
    "DeploymentStage",
    "ExportJob",
    "ExportSchedule",
    "Quota",
    # quality
    "BacklogItem",
    "EvaluationRun",
    "FeedbackIssue",
    "FeedbackItem",
    "GuardrailConfig",
    "GuardrailEvent",
    "Improvement",
    "TestRun",
    "TestSuite",
    # licensing
    "Entitlement",
    "Invoice",
    "LicensePlan",
    "SeatAssignment",
    "TenantLicense",
    "UsageRecord",
]
