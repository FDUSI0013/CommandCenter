"""Wire contracts for the Configuration Center.

A configuration is a governed asset — a model binding, a prompt, a tool, a
guardrail, a routing table, a quota, an environment, a connector or an MCP
server — whose body is versioned. The ``Configuration`` row holds identity,
lifecycle and the pointer to the live revision; every body change is a new
``ConfigurationVersion``, so rollback republishes an old body rather than
overwriting the current one.

This module also carries the *declared schema* of each configuration type: the
field list ``POST /configurations/{id}/validate`` checks a body against, and
that ``GET /configurations/schema`` publishes so the console's editor can render
the right form. Keeping the declaration here rather than in the service means
the contract the API validates against is the contract the OpenAPI document
describes.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from typing import Any, Final
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.registry import (
    ConfigurationStatus,
    ConfigurationType,
    EnvironmentType,
    ImpactLevel,
)

#: Versions are ``vMAJOR.MINOR.PATCH``. The console renders them verbatim.
VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

#: What a brand new configuration's first version is called.
INITIAL_VERSION: Final[str] = "v0.1.0"

#: A configuration body is settings, not a payload store.
MAX_PAYLOAD_KEYS: Final[int] = 200

#: Ceiling on one import bundle, so a bad file cannot become a long transaction.
MAX_IMPORT_ITEMS: Final[int] = 200

#: Payload key holding the map of names this configuration depends on. Resolved
#: against the workspace's other configurations by validation and by the usage
#: endpoint, which is what fills the inspector's "Linked Configurations" panel.
LINKS_KEY: Final[str] = "links"

#: Link slots the console renders, in display order.
LINK_SLOTS: Final[tuple[str, ...]] = ("model", "guardrail", "tools", "routing")


def normalise_version(value: str) -> str:
    """Return ``v1.2.3`` for any accepted spelling of it."""
    match = VERSION_PATTERN.match(value.strip())
    if match is None:
        raise ValueError("version must look like 'v1.2.3'")
    major, minor, patch = match.groups()
    return f"v{int(major)}.{int(minor)}.{int(patch)}"


def next_version(current: str | None) -> str:
    """The version a New Version button offers: the next minor, patch reset."""
    if not current:
        return INITIAL_VERSION
    match = VERSION_PATTERN.match(current.strip())
    if match is None:
        return INITIAL_VERSION
    major, minor, _patch = (int(part) for part in match.groups())
    return f"v{major}.{minor + 1}.0"


# ---------------------------------------------------------------------------
# The declared schema of a configuration body
# ---------------------------------------------------------------------------


class FieldKind(enum.StrEnum):
    """The value shapes a declared field may take."""

    STRING = "string"
    URL = "url"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


class ConfigurationFieldSpec(BaseModel):
    """One declared field of a configuration body."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: FieldKind
    required: bool = False
    required_in_production: bool = Field(
        False,
        description="Optional everywhere else, mandatory once the row targets Production.",
    )
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    min_items: int | None = None
    description: str = ""


def _spec(
    name: str,
    kind: FieldKind,
    description: str,
    *,
    required: bool = False,
    required_in_production: bool = False,
    choices: tuple[str, ...] = (),
    minimum: float | None = None,
    maximum: float | None = None,
    min_items: int | None = None,
) -> ConfigurationFieldSpec:
    return ConfigurationFieldSpec(
        name=name,
        kind=kind,
        required=required,
        required_in_production=required_in_production,
        choices=choices,
        minimum=minimum,
        maximum=maximum,
        min_items=min_items,
        description=description,
    )


#: The body contract per configuration type. Anything not declared here is
#: reported as an unknown key rather than silently accepted, so a typo in a
#: production guardrail is caught before the version is activated.
CONFIGURATION_SCHEMAS: Final[dict[str, tuple[ConfigurationFieldSpec, ...]]] = {
    ConfigurationType.MODEL.value: (
        _spec("provider", FieldKind.STRING, "Serving provider.", required=True),
        _spec("model", FieldKind.STRING, "Model or deployment identifier.", required=True),
        _spec("deployment", FieldKind.STRING, "Provider-side deployment name."),
        _spec(
            "temperature",
            FieldKind.NUMBER,
            "Sampling temperature.",
            required_in_production=True,
            minimum=0.0,
            maximum=2.0,
        ),
        _spec("top_p", FieldKind.NUMBER, "Nucleus sampling cutoff.", minimum=0.0, maximum=1.0),
        _spec(
            "max_tokens",
            FieldKind.INTEGER,
            "Completion ceiling.",
            required_in_production=True,
            minimum=1,
            maximum=1_000_000,
        ),
        _spec(
            "timeout_seconds",
            FieldKind.INTEGER,
            "Per-call timeout.",
            minimum=1,
            maximum=600,
        ),
        _spec("stop_sequences", FieldKind.ARRAY, "Hard stops applied to completions."),
    ),
    ConfigurationType.PROMPT.value: (
        _spec("template", FieldKind.STRING, "Prompt text with {{variables}}.", required=True),
        _spec("variables", FieldKind.ARRAY, "Variable names the template expects."),
        _spec("token_budget", FieldKind.INTEGER, "Rendered ceiling.", minimum=1),
        _spec("role", FieldKind.STRING, "Message role.", choices=("system", "user", "assistant")),
    ),
    ConfigurationType.TOOL.value: (
        _spec("tool_name", FieldKind.STRING, "Name the agent calls.", required=True),
        _spec("endpoint", FieldKind.URL, "Absolute http(s) endpoint.", required=True),
        _spec(
            "auth_mode",
            FieldKind.STRING,
            "How the call authenticates.",
            required_in_production=True,
            choices=("None", "API Key", "OAuth", "Managed Identity", "Certificate"),
        ),
        _spec("scopes", FieldKind.ARRAY, "Scopes requested at call time."),
        _spec("timeout_seconds", FieldKind.INTEGER, "Call timeout.", minimum=1, maximum=600),
    ),
    ConfigurationType.GUARDRAIL.value: (
        _spec(
            "rule_type",
            FieldKind.STRING,
            "What the guardrail inspects.",
            required=True,
            choices=(
                "PII",
                "Toxicity",
                "Jailbreak",
                "Topic",
                "Schema",
                "Groundedness",
                "Custom",
            ),
        ),
        _spec(
            "action",
            FieldKind.STRING,
            "What happens on a hit.",
            required=True,
            choices=("Block", "Warn", "Redact", "Log"),
        ),
        _spec(
            "threshold",
            FieldKind.NUMBER,
            "Score at or above which the rule fires.",
            required_in_production=True,
            minimum=0.0,
            maximum=1.0,
        ),
        _spec("applies_to", FieldKind.ARRAY, "Agent names the rule covers."),
    ),
    ConfigurationType.ROUTING.value: (
        _spec(
            "strategy",
            FieldKind.STRING,
            "How a target is chosen.",
            required=True,
            choices=("priority", "weighted", "failover", "cost"),
        ),
        _spec("rules", FieldKind.ARRAY, "Ordered match rules.", required=True, min_items=1),
        _spec(
            "default_target",
            FieldKind.STRING,
            "Target used when no rule matches.",
            required=True,
        ),
        _spec("sticky_sessions", FieldKind.BOOLEAN, "Keep a session on its first target."),
    ),
    ConfigurationType.QUOTA.value: (
        _spec(
            "resource",
            FieldKind.STRING,
            "What is being limited.",
            required=True,
            choices=("tokens", "requests", "cost", "concurrency", "storage"),
        ),
        _spec("limit", FieldKind.NUMBER, "Ceiling per period.", required=True, minimum=0.000001),
        _spec(
            "period",
            FieldKind.STRING,
            "Window the limit resets on.",
            required=True,
            choices=("Minute", "Hour", "Day", "Week", "Month"),
        ),
        _spec(
            "enforcement",
            FieldKind.STRING,
            "What happens at the ceiling.",
            required_in_production=True,
            choices=("Hard", "Soft", "Notify"),
        ),
        _spec(
            "warn_at_percent",
            FieldKind.INTEGER,
            "Percent of the limit that raises a warning.",
            minimum=1,
            maximum=99,
        ),
    ),
    ConfigurationType.ENVIRONMENT.value: (
        _spec("region", FieldKind.STRING, "Cloud region.", required=True),
        _spec("endpoint", FieldKind.URL, "Control endpoint for the environment."),
        _spec(
            "tier",
            FieldKind.STRING,
            "Service tier.",
            choices=("Basic", "Standard", "Premium", "Isolated"),
        ),
        _spec(
            "network_isolation",
            FieldKind.BOOLEAN,
            "Private networking only.",
            required_in_production=True,
        ),
    ),
    ConfigurationType.CONNECTOR.value: (
        _spec("provider", FieldKind.STRING, "Upstream system.", required=True),
        _spec("endpoint", FieldKind.URL, "Absolute http(s) endpoint.", required=True),
        _spec(
            "auth_mode",
            FieldKind.STRING,
            "How the connector authenticates.",
            required=True,
            choices=("None", "API Key", "OAuth", "Managed Identity", "Certificate"),
        ),
        _spec("scopes", FieldKind.ARRAY, "Granted scopes."),
        _spec(
            "data_classification",
            FieldKind.STRING,
            "Highest classification the connector may carry.",
            required_in_production=True,
            choices=("Public", "Internal", "External", "Confidential", "Restricted"),
        ),
    ),
    ConfigurationType.MCP_SERVER.value: (
        _spec("server_url", FieldKind.URL, "Absolute http(s) server URL.", required=True),
        _spec(
            "transport",
            FieldKind.STRING,
            "Wire transport.",
            required=True,
            choices=("http", "sse", "stdio"),
        ),
        _spec("tools", FieldKind.ARRAY, "Tool names exposed to agents."),
        _spec(
            "auth_mode",
            FieldKind.STRING,
            "How the server authenticates callers.",
            required_in_production=True,
            choices=("None", "API Key", "OAuth", "Managed Identity"),
        ),
    ),
}


def schema_for(config_type: str) -> tuple[ConfigurationFieldSpec, ...]:
    """Declared fields for a configuration type; empty when the type is free-form."""
    return CONFIGURATION_SCHEMAS.get(config_type, ())


def is_absolute_http_url(value: Any) -> bool:
    """True when the value is a string carrying an absolute http(s) URL."""
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Validation report
# ---------------------------------------------------------------------------


class FindingSeverity(enum.StrEnum):
    """How much a finding matters. Only ``error`` blocks activation."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ConfigurationFinding(BaseModel):
    """One thing validation noticed, addressed to one field."""

    field: str = Field(description="Dot-path into the body, or '' for whole-body findings.")
    severity: FindingSeverity
    code: str = Field(description="Stable machine code, e.g. 'required_missing'.")
    message: str


class ConfigurationValidationReport(BaseModel):
    """Result of checking one body against its declared schema."""

    configuration_id: str
    version: str
    config_type: ConfigurationType
    environment: EnvironmentType
    valid: bool = Field(description="True when no finding has error severity.")
    error_count: int
    warning_count: int
    declared_fields: int
    checked_fields: int = Field(description="Keys actually present in the body.")
    findings: list[ConfigurationFinding] = Field(default_factory=list)


class ConfigurationSchemaRead(BaseModel):
    """The declared contract for one configuration type."""

    config_type: ConfigurationType
    fields: list[ConfigurationFieldSpec]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class ConfigurationRead(BaseModel):
    """One row of the Configuration Center table."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    config_type: ConfigurationType
    environment: EnvironmentType
    status: ConfigurationStatus
    current_version: str | None = None
    impact: ImpactLevel
    description: str | None = None

    owner_user_id: str | None = None
    owner_name: str | None = Field(None, description="Resolved from the owning user.")
    owner_team: str | None = None

    version_count: int = Field(0, description="Revisions recorded for this configuration.")

    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: str | None = None
    updated_by: str | None = None


class ConfigurationVersionRead(BaseModel):
    """One revision, without its body — what the version-history pipe renders."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    configuration_id: str
    version: str
    status: ConfigurationStatus
    change_note: str | None = None
    author_user_id: str | None = None
    author_name: str | None = None
    published_at: dt.datetime | None = None
    is_current: bool = False
    created_at: dt.datetime


class ConfigurationVersionDetail(ConfigurationVersionRead):
    """One revision with its body."""

    payload: dict[str, Any] = Field(default_factory=dict)


class ConfigurationLink(BaseModel):
    """One entry of the inspector's Linked Configurations panel."""

    slot: str = Field(description="Link slot: model, guardrail, tools or routing.")
    name: str
    configuration_id: str | None = Field(
        None, description="Null when the declared name matches no configuration here."
    )
    config_type: ConfigurationType | None = None
    status: ConfigurationStatus | None = None
    resolved: bool


class ConfigurationUsage(BaseModel):
    """Impact & Usage plus Linked Configurations for one configuration.

    ``runs_30d`` and ``success_rate`` are read from the telemetry engine for the
    agents this configuration is bound to. With no bound agent there is no
    telemetry to attribute, and both come back null rather than zero.
    """

    configuration_id: str
    impact: ImpactLevel
    used_by_agents: int
    agent_ids: list[str] = Field(default_factory=list)
    runs_30d: int | None = None
    success_rate: float | None = Field(
        None, description="Percentage of the window's runs that finished without error."
    )
    error_count_30d: int | None = None
    window_days: int
    telemetry_available: bool = Field(
        description="False when no bound agent reports to the telemetry engine."
    )
    links: list[ConfigurationLink] = Field(default_factory=list)


class ConfigurationFieldChange(BaseModel):
    """One field-level difference between two revisions."""

    field: str
    change: str = Field(description="added, removed or changed")
    before: Any = None
    after: Any = None


class ConfigurationDiff(BaseModel):
    """What changed between two revisions of one configuration."""

    configuration_id: str
    from_version: str
    to_version: str
    from_status: ConfigurationStatus
    to_status: ConfigurationStatus
    added: int
    removed: int
    changed: int
    identical: bool
    changes: list[ConfigurationFieldChange] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _clean_payload(value: dict[str, Any]) -> dict[str, Any]:
    if len(value) > MAX_PAYLOAD_KEYS:
        raise ValueError(f"a configuration body may not exceed {MAX_PAYLOAD_KEYS} keys")
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("configuration body keys must be non-empty strings")
    return value


class ConfigurationCreate(BaseModel):
    """Create a configuration.

    The row is created Draft with its first version; nothing reaches Active
    until a version is activated, and activation runs validation first.
    """

    name: str = Field(min_length=1, max_length=200)
    config_type: ConfigurationType
    environment: EnvironmentType = EnvironmentType.DEVELOPMENT
    impact: ImpactLevel = ImpactLevel.LOW
    description: str | None = Field(None, max_length=2000)
    owner_user_id: str | None = Field(None, max_length=36)
    version: str = Field(INITIAL_VERSION, description="Label of the first revision.")
    payload: dict[str, Any] = Field(default_factory=dict)
    change_note: str | None = Field(None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return normalise_version(value)

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _clean_payload(value)


class ConfigurationUpdate(BaseModel):
    """Partial update of a configuration's identity. Bodies move by version."""

    name: str | None = Field(None, min_length=1, max_length=200)
    config_type: ConfigurationType | None = None
    environment: EnvironmentType | None = None
    impact: ImpactLevel | None = None
    description: str | None = Field(None, max_length=2000)
    owner_user_id: str | None = Field(None, max_length=36)
    expected_updated_at: dt.datetime | None = Field(
        None,
        description=(
            "Optimistic concurrency guard: send the updated_at you last read and the "
            "write is refused with 409 if someone changed the row since."
        ),
    )

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class ConfigurationVersionCreate(BaseModel):
    """Cut a new revision — the New Version dialog."""

    version: str | None = Field(
        None, description="Defaults to the next minor of the current version."
    )
    payload: dict[str, Any] | None = Field(
        None, description="Defaults to a copy of the current body."
    )
    change_note: str | None = Field(None, max_length=2000)
    activate: bool = Field(
        True,
        description=(
            "Publish the revision as current. Validation runs first and the request "
            "is refused with 422 if the body has errors."
        ),
    )

    @field_validator("version")
    @classmethod
    def _version(cls, value: str | None) -> str | None:
        return None if value is None else normalise_version(value)

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _clean_payload(value)


class ConfigurationCloneRequest(BaseModel):
    """Copy a configuration and its current body into a new Draft."""

    name: str | None = Field(
        None, max_length=200, description="Defaults to '<name> (Copy)'."
    )
    environment: EnvironmentType | None = None
    owner_user_id: str | None = Field(None, max_length=36)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class ConfigurationRollbackRequest(BaseModel):
    """Rollback to Previous: republish an earlier body as a new revision."""

    version: str | None = Field(
        None,
        description=(
            "Revision whose body becomes current. Defaults to the one published "
            "immediately before the current revision."
        ),
    )
    change_note: str | None = Field(None, max_length=2000)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str | None) -> str | None:
        return None if value is None else normalise_version(value)


class ConfigurationDeprecateRequest(BaseModel):
    """Deprecate a configuration, with the reason recorded in the audit trail."""

    reason: str | None = Field(None, max_length=1000)
    archive: bool = Field(
        False, description="Archive instead of deprecating; archived rows are read-only."
    )


class ConfigurationValidateRequest(BaseModel):
    """Validate a body without storing it.

    With no body supplied the stored revision is checked, which is what the
    console does before activating a version.
    """

    version: str | None = Field(None, description="Revision to check; defaults to current.")
    payload: dict[str, Any] | None = Field(
        None, description="Check this body instead of the stored one."
    )
    environment: EnvironmentType | None = Field(
        None, description="Check against a different environment's rules."
    )

    @field_validator("version")
    @classmethod
    def _version(cls, value: str | None) -> str | None:
        return None if value is None else normalise_version(value)

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _clean_payload(value)


class ConfigurationBundleItem(BaseModel):
    """One configuration inside an import/export bundle."""

    name: str = Field(min_length=1, max_length=200)
    config_type: ConfigurationType
    environment: EnvironmentType = EnvironmentType.DEVELOPMENT
    impact: ImpactLevel = ImpactLevel.LOW
    status: ConfigurationStatus = ConfigurationStatus.DRAFT
    description: str | None = Field(None, max_length=2000)
    version: str = INITIAL_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)
    change_note: str | None = Field(None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return normalise_version(value)

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _clean_payload(value)


class ConfigurationBundle(BaseModel):
    """What ``GET /configurations/bundle`` emits and ``POST /import`` accepts."""

    exported_at: dt.datetime | None = None
    workspace: str | None = None
    items: list[ConfigurationBundleItem] = Field(default_factory=list)


class ImportConflictMode(enum.StrEnum):
    """What to do when a bundle names a configuration that already exists."""

    SKIP = "skip"
    NEW_VERSION = "new_version"


class ConfigurationImportRequest(BaseModel):
    """Body of ``POST /configurations/import``."""

    model_config = ConfigDict(extra="forbid")

    items: list[ConfigurationBundleItem] = Field(min_length=1, max_length=MAX_IMPORT_ITEMS)
    on_conflict: ImportConflictMode = ImportConflictMode.SKIP
    source: str | None = Field(
        None, max_length=255, description="Filename the bundle came from."
    )


class ConfigurationImportIssue(BaseModel):
    """One item that could not be imported, reported without failing the batch."""

    index: int
    name: str
    reason: str


class ConfigurationImportResult(BaseModel):
    """Outcome of an import run."""

    submitted: int
    created: int
    versioned: int
    skipped: int
    configuration_ids: list[str] = Field(default_factory=list)
    issues: list[ConfigurationImportIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Action envelope and KPI cards
# ---------------------------------------------------------------------------


class ConfigurationActionResponse(BaseModel):
    """Uniform answer for the verbs: the row as it now stands, plus a message."""

    configuration: ConfigurationRead
    message: str
    version: ConfigurationVersionRead | None = None
    validation: ConfigurationValidationReport | None = None


class ConfigurationStatusSlice(BaseModel):
    """One status count with its share of the total."""

    status: ConfigurationStatus
    count: int
    percent: float = Field(description="Share of all configurations, 0-100, one decimal")


class ConfigurationsSummary(BaseModel):
    """The six KPI cards above the Configuration Center table.

    Every number is a SQL aggregate: the status counts over ``configurations``,
    the windowed change count over the audit trail this domain writes.
    """

    total: int
    active: int
    draft: int
    deprecated: int
    archived: int
    changes_30d: int = Field(description="Audit events against configurations in the window.")
    created_30d: int = Field(description="Configurations created in the window.")
    versions_30d: int = Field(description="Revisions cut in the window.")
    window_days: int
    status_breakdown: list[ConfigurationStatusSlice] = Field(default_factory=list)
