"""The governed inventory: agents and everything an agent is allowed to touch.

These tables are the *system of record* for what exists and who owns it. They
deliberately do not store behaviour or telemetry — runs, traces, evaluations and
every rolling metric live in the telemetry engine and are read back through the
engine client. An ``Agent`` row is the control-plane half of a 1:1 pairing with
an engine project, joined by ``engine_project_id``.

Status/risk vocabularies below are the exact strings the control-plane UI
renders, so no translation layer sits between the API and the frontend.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.base import (
    ActorMixin,
    Base,
    PrimaryKeyMixin,
    TimestampMixin,
    UtcDateTime,
    WorkspaceScopedMixin,
)

# ---------------------------------------------------------------------------
# Shared vocabularies
#
# Stored as plain strings (not native DB enums) because SQLite has no ENUM type
# and Postgres enum ALTERs are painful; the enum classes are the single place a
# route or seed script looks up a legal value.
# ---------------------------------------------------------------------------


class Platform(str, enum.Enum):
    """Where the agent actually runs. Drives which connection syncs it."""

    AZURE_AI_FOUNDRY = "Azure AI Foundry"
    COPILOT_STUDIO = "Copilot Studio"
    M365_COPILOT = "M365 Copilot"
    POWER_PLATFORM = "Power Platform"
    CUSTOM_AGENT = "Custom Agent"


class AgentType(str, enum.Enum):
    """Build style — governance depth differs sharply between these."""

    PRO_CODE = "Pro-code"
    LOW_CODE = "Low-code"
    COPILOT = "Copilot"


class EnvironmentType(str, enum.Enum):
    """Deployment target class. Production rows get the strictest policy set."""

    PRODUCTION = "Production"
    STAGING = "Staging"
    UAT = "UAT"
    DEVELOPMENT = "Development"
    QA = "QA"
    SANDBOX = "Sandbox"
    DR = "DR"


class AgentStatus(str, enum.Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    PENDING_REVIEW = "Pending Review"


class RiskLevel(str, enum.Enum):
    """Assessed blast radius. The registry list view colour-codes on this."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class PolicyStatus(str, enum.Enum):
    """Verdict of the policy engine's last evaluation of this agent."""

    ALLOWED = "Allowed"
    WARNED = "Warned"
    BLOCKED = "Blocked"
    APPROVAL_REQUIRED = "Approval Required"


class ConnectionStatus(str, enum.Enum):
    CONNECTED = "Connected"
    WARNING = "Warning"
    DISCONNECTED = "Disconnected"


class HealthState(str, enum.Enum):
    HEALTHY = "Healthy"
    WARNING = "Warning"
    UNHEALTHY = "Unhealthy"


class ConnectorType(str, enum.Enum):
    """Shape of the integration. MCP servers are governed harder than plain APIs."""

    API = "API"
    MCP_SERVER = "MCP Server"
    DATABASE = "Database"
    VECTOR_STORE = "Vector Store"
    CUSTOM_TOOL = "Custom Tool"
    # Legacy groupings the Connector Governance filters still offer.
    CONNECTOR = "Connector"
    DATA_SOURCE = "Data Source"
    TOOL = "Tool"


class ConnectorStatus(str, enum.Enum):
    ACTIVE = "Active"
    BLOCKED = "Blocked"
    DEPRECATED = "Deprecated"
    WARNING = "Warning"
    INACTIVE = "Inactive"


class AccessLevel(str, enum.Enum):
    """What the grant lets an agent do through the connector."""

    READ = "Read"
    READ_WRITE = "Read-Write"
    ADMIN = "Admin"


class DataClassification(str, enum.Enum):
    """Highest classification of data the connector is cleared to carry."""

    PUBLIC = "Public"
    INTERNAL = "Internal"
    EXTERNAL = "External"
    CONFIDENTIAL = "Confidential"
    RESTRICTED = "Restricted"


class EnvironmentStatus(str, enum.Enum):
    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    STANDBY = "Standby"
    OFFLINE = "Offline"


class KnowledgeSourceType(str, enum.Enum):
    SHAREPOINT = "SharePoint"
    BLOB_STORAGE = "Blob Storage"
    CONFLUENCE = "Confluence"
    WEB = "Web"
    DATABASE = "Database"
    VECTOR_INDEX = "Vector Index"
    GITHUB = "GitHub"
    FILE_SHARE = "File Share"


class KnowledgeSourceStatus(str, enum.Enum):
    ACTIVE = "Active"
    SYNCING = "Syncing"
    ERROR = "Error"
    FAILED = "Failed"
    PAUSED = "Paused"


class Sensitivity(str, enum.Enum):
    """Drives whether ACL trimming is mandatory on retrieval."""

    PUBLIC = "Public"
    INTERNAL = "Internal"
    CONFIDENTIAL = "Confidential"
    HIGHLY_CONFIDENTIAL = "Highly Confidential"
    RESTRICTED = "Restricted"


class MemoryStoreType(str, enum.Enum):
    CONVERSATION = "Conversation"
    VECTOR = "Vector"
    KEY_VALUE = "Key-Value"
    SESSION = "Session"
    LONG_TERM = "Long-term"


class MemoryStoreStatus(str, enum.Enum):
    ACTIVE = "Active"
    PAUSED = "Paused"
    DEGRADED = "Degraded"


class ConfigurationType(str, enum.Enum):
    MODEL = "Model"
    PROMPT = "Prompt"
    TOOL = "Tool"
    GUARDRAIL = "Guardrail"
    ROUTING = "Routing"
    QUOTA = "Quota"
    ENVIRONMENT = "Environment"
    CONNECTOR = "Connector"
    MCP_SERVER = "MCP Server"


class ConfigurationStatus(str, enum.Enum):
    ACTIVE = "Active"
    DRAFT = "Draft"
    DEPRECATED = "Deprecated"
    ARCHIVED = "Archived"


class ImpactLevel(str, enum.Enum):
    """How much breaks if this configuration is wrong. Gates the approval path."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class Agent(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A governed AI agent — the central entity every other screen hangs off.

    Rolling metrics (runs30, success30, tokens30, cost30, avgLatency, evalScore,
    violations30, escalations30, toolCalls30) are intentionally NOT columns: they
    are aggregated live from the telemetry engine for the requested window, so a
    stored copy would drift the moment a run lands. ``metrics_cache`` holds the
    last engine response purely so the 500-row list view can render without
    fanning out one engine call per agent.
    """

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug"),
        Index("ix_agents_workspace_status", "workspace_id", "status"),
        Index("ix_agents_workspace_environment", "workspace_id", "environment"),
        Index("ix_agents_workspace_risk", "workspace_id", "risk"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    platform: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    agent_type: Mapped[str] = mapped_column(String(24), nullable=False)
    environment: Mapped[str] = mapped_column(
        String(24), default=EnvironmentType.DEVELOPMENT.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=AgentStatus.PENDING_REVIEW.value, nullable=False
    )
    risk: Mapped[str] = mapped_column(
        String(12), default=RiskLevel.LOW.value, nullable=False
    )
    policy_status: Mapped[str] = mapped_column(
        String(24), default=PolicyStatus.ALLOWED.value, nullable=False
    )

    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    team: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tags: Mapped[list] = mapped_column(default=list)

    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tools_enabled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    policies_applied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    memory_policy: Mapped[str | None] = mapped_column(String(48), nullable=True)
    access_scope: Mapped[str | None] = mapped_column(String(48), nullable=True)

    last_used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 0-100 composite score shown as the health pill on the registry cards.
    health: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Link into the telemetry engine; null until the agent has been provisioned.
    engine_project_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    engine_project_name: Mapped[str | None] = mapped_column(String(160), nullable=True)

    metrics_cache: Mapped[dict | None] = mapped_column(nullable=True)
    metrics_cached_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    connectors: Mapped[list[AgentConnector]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )

    @property
    def is_provisioned(self) -> bool:
        return self.engine_project_id is not None


# ---------------------------------------------------------------------------
# Connection Center
# ---------------------------------------------------------------------------


class Connection(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A platform integration (Foundry, Copilot Studio, Purview, Key Vault…).

    One row per tile on the Connection Center. Credentials never live here — only
    a pointer to the encrypted secret row.
    """

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_connections_workspace_status", "workspace_id", "status"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    # Frontend icon lookup key ("foundry", "keyvault", "mcp"…).
    logo_key: Mapped[str | None] = mapped_column(String(40), nullable=True)

    status: Mapped[str] = mapped_column(
        String(24), default=ConnectionStatus.DISCONNECTED.value, nullable=False
    )
    health: Mapped[str] = mapped_column(
        String(16), default=HealthState.UNHEALTHY.value, nullable=False
    )

    # Ordered [label, value] pairs rendered verbatim under the tile title.
    metadata_pairs: Mapped[list] = mapped_column(default=list)

    last_sync_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_agent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    syncs_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    config: Mapped[dict] = mapped_column(default=dict)
    credential_secret_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    activity: Mapped[list[ConnectionActivity]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class ConnectionActivity(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Append-only sync/error feed under each connection. Never updated in place."""

    __tablename__ = "connection_activity"
    __table_args__ = (
        Index("ix_connection_activity_connection_occurred", "connection_id", "occurred_at"),
        Index("ix_connection_activity_workspace_occurred", "workspace_id", "occurred_at"),
    )

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    event: Mapped[str] = mapped_column(String(80), nullable=False)
    # "Success" | "Warning" | "Error"
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    connection: Mapped[Connection] = relationship(back_populates="activity")


# ---------------------------------------------------------------------------
# Connector and MCP governance
# ---------------------------------------------------------------------------


class Connector(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A tool, API or MCP server an agent may call, plus its blocking record.

    Blocking is soft (``status`` + ``blocked_*``) rather than a delete so the
    audit trail survives and the block can be lifted.
    """

    __tablename__ = "connectors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_connectors_workspace_status", "workspace_id", "status"),
        Index("ix_connectors_workspace_risk", "workspace_id", "risk_level"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(120), nullable=True)
    risk_level: Mapped[str] = mapped_column(
        String(12), default=RiskLevel.LOW.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=ConnectorStatus.ACTIVE.value, nullable=False
    )
    access: Mapped[str] = mapped_column(
        String(16), default=AccessLevel.READ.value, nullable=False
    )
    scopes: Mapped[list] = mapped_column(default=list)

    endpoint_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # "OAuth 2.0" | "API Key" | "Managed Identity" | "Service Principal"
    auth_mode: Mapped[str | None] = mapped_column(String(40), nullable=True)
    data_classification: Mapped[str] = mapped_column(
        String(24), default=DataClassification.INTERNAL.value, nullable=False
    )

    blocked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    blocked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)

    agents: Mapped[list[AgentConnector]] = relationship(
        back_populates="connector", cascade="all, delete-orphan"
    )

    @property
    def is_blocked(self) -> bool:
        return self.status == ConnectorStatus.BLOCKED.value


class AgentConnector(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Grant of one connector to one agent — the edge the access graph draws."""

    __tablename__ = "agent_connectors"
    __table_args__ = (
        UniqueConstraint("agent_id", "connector_id"),
        Index("ix_agent_connectors_connector_id", "connector_id"),
    )

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    connector_id: Mapped[str] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False
    )
    granted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    granted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    agent: Mapped[Agent] = relationship(back_populates="connectors")
    connector: Mapped[Connector] = relationship(back_populates="agents")


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


class Environment(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A deployment target. Agents and configurations are scoped to one of these."""

    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_environments_workspace_status", "workspace_id", "status"),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    env_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), default=EnvironmentStatus.HEALTHY.value, nullable=False
    )
    # Uptime percentage; null for a standby DR target that reports nothing.
    health: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Matching environment in the telemetry engine; null until provisioned.
    engine_environment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_deployment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ---------------------------------------------------------------------------
# Knowledge (RAG governance)
# ---------------------------------------------------------------------------


class KnowledgeSource(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """An indexed corpus an agent may ground on.

    ``has_acl`` is stored rather than derived: retrieval must refuse a
    confidential source that has lost its ACL trimming, and that check has to be
    answerable without calling the indexer.
    """

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_knowledge_sources_workspace_status", "workspace_id", "status"),
        Index("ix_knowledge_sources_workspace_environment", "workspace_id", "environment"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    environment: Mapped[str] = mapped_column(
        String(24), default=EnvironmentType.PRODUCTION.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=KnowledgeSourceStatus.ACTIVE.value, nullable=False
    )

    document_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 0-1 grounding quality from the latest evaluation; null before first run.
    grounding_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    sensitivity: Mapped[str] = mapped_column(
        String(24), default=Sensitivity.INTERNAL.value, nullable=False, index=True
    )
    has_acl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    acl_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_sync_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    sync_progress: Mapped[int] = mapped_column(Integer, default=100, nullable=False)  # 0-100
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)

    index_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    chunk_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_overlap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Top-k, reranker, filters — whatever the retriever needs, versioned as a blob.
    retrieval_policy: Mapped[dict] = mapped_column(default=dict)


# ---------------------------------------------------------------------------
# Memory and state
# ---------------------------------------------------------------------------


class MemoryStore(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A conversation/vector/session store an agent reads and writes at runtime.

    Retention is recorded here because deletion SLAs are audited against the
    control plane, not against whichever backend happens to hold the bytes.
    """

    __tablename__ = "memory_stores"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_memory_stores_workspace_status", "workspace_id", "status"),
        Index("ix_memory_stores_workspace_environment", "workspace_id", "environment"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    store_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    environment: Mapped[str] = mapped_column(
        String(24), default=EnvironmentType.PRODUCTION.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=MemoryStoreStatus.ACTIVE.value, nullable=False
    )

    usage_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Human-readable label ("90 days"); retention_days is what the purge job reads.
    retention_policy: Mapped[str | None] = mapped_column(String(48), nullable=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_updated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    backend: Mapped[str | None] = mapped_column(String(80), nullable=True)

    active_session_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_retrieval_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_backup_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)


# ---------------------------------------------------------------------------
# Configuration Center
# ---------------------------------------------------------------------------


class Configuration(Base, PrimaryKeyMixin, TimestampMixin, ActorMixin, WorkspaceScopedMixin):
    """A versioned governed asset (model, prompt, guardrail, quota…).

    The row itself holds only identity and the pointer to the live version; every
    payload change is a new ``ConfigurationVersion`` so rollback is a pointer
    move rather than a destructive edit.
    """

    __tablename__ = "configurations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_configurations_workspace_type", "workspace_id", "config_type"),
        Index("ix_configurations_workspace_status", "workspace_id", "status"),
        Index("ix_configurations_workspace_environment", "workspace_id", "environment"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config_type: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(
        String(24), default=EnvironmentType.DEVELOPMENT.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=ConfigurationStatus.DRAFT.value, nullable=False
    )
    # Denormalised pointer to the version flagged is_current, e.g. "v2.1.0".
    current_version: Mapped[str | None] = mapped_column(String(24), nullable=True)
    impact: Mapped[str] = mapped_column(
        String(12), default=ImpactLevel.LOW.value, nullable=False
    )
    owner_user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    versions: Mapped[list[ConfigurationVersion]] = relationship(
        back_populates="configuration", cascade="all, delete-orphan"
    )


class ConfigurationVersion(Base, PrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One immutable revision of a configuration. Rollback republishes an old row."""

    __tablename__ = "configuration_versions"
    __table_args__ = (
        Index(
            "ix_configuration_versions_config_version",
            "configuration_id",
            "version",
            unique=True,
        ),
        Index("ix_configuration_versions_config_current", "configuration_id", "is_current"),
    )

    configuration_id: Mapped[str] = mapped_column(
        ForeignKey("configurations.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=ConfigurationStatus.DRAFT.value, nullable=False
    )
    payload: Mapped[dict] = mapped_column(default=dict)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    configuration: Mapped[Configuration] = relationship(back_populates="versions")
