"""Row builders, so a test can set up state without driving the API.

Every method commits and returns the persisted row. They exist for two reasons:
a test that is *about* approvals should not have to spend twenty lines creating
an agent through HTTP first, and some states — a locked account, an exhausted
quota, an expired key — cannot be reached through the API at all and still have
to be tested.

Where the application owns a value the factory does not invent one: an API key's
secret is minted by ``core.security`` so the real verification path accepts it,
and a stored credential is encrypted by ``core.security`` so a reveal really
decrypts. Anything a test does not care about gets a plausible default rather
than ``None``, so a row is realistic enough that a serialiser does not fall over
on it.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fulcrum_ops_api.core.security import encrypt_secret, hash_password, mask_secret, mint_api_key
from fulcrum_ops_api.models.governance import (
    ApprovalRequest,
    ApprovalStatus,
    Policy,
    PolicyCategory,
    PolicyEnforcement,
    PolicyScope,
    PolicyStatus,
    RiskLevel,
    Secret,
    SecretStatus,
    SecretType,
    default_workflow,
)
from fulcrum_ops_api.models.identity import ApiKey, Membership, Role, User, Workspace
from fulcrum_ops_api.models.licensing import (
    BillingPeriod,
    Entitlement,
    EntitlementValueType,
    LicensePlan,
    LicenseStatus,
    PlanStatus,
    PlanTier,
    TenantLicense,
)
from fulcrum_ops_api.models.operations import (
    Alert,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    Budget,
    Deployment,
    DeploymentStage,
    DeploymentStageStatus,
    DeploymentStatus,
    DeploymentStrategy,
    LimitPeriod,
    LimitScope,
    LimitStatus,
    Quota,
    QuotaEnforcement,
    QuotaResource,
)
from fulcrum_ops_api.models.quality import (
    EvaluationRun,
    EvaluationStatus,
    FeedbackItem,
    FeedbackSource,
    GuardrailAction,
    GuardrailConfig,
    GuardrailEvent,
    GuardrailScope,
    GuardrailStatus,
    GuardrailType,
    Sentiment,
    SuiteStatus,
    SuiteType,
    TestSuite,
)
from fulcrum_ops_api.models.registry import (
    Agent,
    AgentStatus,
    AgentType,
    Configuration,
    ConfigurationStatus,
    ConfigurationType,
    ConfigurationVersion,
    Connector,
    ConnectorStatus,
    ConnectorType,
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    ImpactLevel,
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
    MemoryStore,
    MemoryStoreStatus,
    MemoryStoreType,
    Platform,
)

#: Default password for factory-built accounts that need one. Only the login
#: tests pay the hashing cost; everything else is issued a signed session.
PASSWORD = "correct horse battery staple"  # noqa: S105 — a test fixture, not a credential


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Factory:
    """Builds persisted rows. One instance per test, sharing the test database."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self._counter = itertools.count(1)

    # -- plumbing ----------------------------------------------------------

    def _next(self) -> int:
        return next(self._counter)

    async def add(self, row: Any) -> Any:
        """Persist one row and return it, detached from any live transaction."""
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            session.expunge(row)
        return row

    async def add_all(self, rows: list[Any]) -> list[Any]:
        async with self._sessionmaker() as session:
            session.add_all(rows)
            await session.commit()
            for row in rows:
                await session.refresh(row)
                session.expunge(row)
        return rows

    # -- identity ----------------------------------------------------------

    async def workspace(
        self,
        *,
        name: str = "Northwind",
        slug: str | None = None,
        status: str = "active",
        engine_workspace: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Workspace:
        resolved = slug or f"workspace-{self._next()}"
        return await self.add(
            Workspace(
                name=name,
                slug=resolved,
                engine_workspace=engine_workspace or resolved,
                status=status,
                settings=settings or {},
            )
        )

    async def user(
        self,
        workspace: Workspace | None = None,
        *,
        email: str | None = None,
        full_name: str = "Test Person",
        role: Role = Role.MEMBER,
        password: str | None = None,
        is_active: bool = True,
        failed_login_count: int = 0,
        locked_until: dt.datetime | None = None,
        **fields: Any,
    ) -> User:
        """Create a user, and their membership when a workspace is given.

        ``password`` is hashed only when supplied — argon2 is deliberately slow,
        and most tests are handed a signed session instead of signing in.
        """
        row = User(
            email=email or f"person-{self._next()}@example.test",
            full_name=full_name,
            password_hash=hash_password(password) if password else None,
            is_active=is_active,
            failed_login_count=failed_login_count,
            locked_until=locked_until,
            **fields,
        )
        await self.add(row)
        if workspace is not None:
            await self.membership(workspace, row, role=role)
        return row

    async def membership(
        self, workspace: Workspace, user: User, *, role: Role = Role.MEMBER
    ) -> Membership:
        return await self.add(
            Membership(workspace_id=workspace.id, user_id=user.id, role=role.value)
        )

    async def api_key(
        self,
        workspace: Workspace,
        *,
        name: str = "SDK key",
        scopes: list[str] | None = None,
        agent_id: str | None = None,
        environment: str | None = None,
        expires_at: dt.datetime | None = None,
        revoked_at: dt.datetime | None = None,
        created_by_user_id: str | None = None,
    ) -> tuple[str, ApiKey]:
        """Mint a real key and return ``(token, row)``.

        The token is produced by ``core.security.mint_api_key``, so the
        verification path the API runs on every request is the one under test
        rather than a shortcut around it.
        """
        minted = mint_api_key()
        row = ApiKey(
            workspace_id=workspace.id,
            name=name,
            key_id=minted.key_id,
            secret_hash=minted.secret_hash,
            display_hint=minted.display_hint,
            agent_id=agent_id,
            scopes=list(scopes if scopes is not None else ApiKey.DEFAULT_SCOPES),
            environment=environment,
            expires_at=expires_at,
            revoked_at=revoked_at,
            created_by_user_id=created_by_user_id,
        )
        await self.add(row)
        return minted.token, row

    # -- registry ----------------------------------------------------------

    async def agent(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        slug: str | None = None,
        platform: str = Platform.CUSTOM_AGENT.value,
        agent_type: str = AgentType.PRO_CODE.value,
        environment: str = EnvironmentType.PRODUCTION.value,
        status: str = AgentStatus.ACTIVE.value,
        risk: str = RiskLevel.LOW.value,
        engine_project_id: str | None = None,
        engine_project_name: str | None = None,
        owner_user_id: str | None = None,
        team: str | None = None,
        model: str | None = "gpt-4o",
        last_used_at: dt.datetime | None = None,
        **fields: Any,
    ) -> Agent:
        index = self._next()
        resolved_name = name or f"Agent {index}"
        resolved_slug = slug or f"agent-{index}"
        return await self.add(
            Agent(
                workspace_id=workspace.id,
                name=resolved_name,
                slug=resolved_slug,
                platform=platform,
                agent_type=agent_type,
                environment=environment,
                status=status,
                risk=risk,
                engine_project_id=engine_project_id,
                engine_project_name=engine_project_name,
                owner_user_id=owner_user_id,
                team=team,
                model=model,
                last_used_at=last_used_at or _now(),
                **fields,
            )
        )

    async def provisioned_agent(
        self, workspace: Workspace, engine, *, name: str | None = None, **fields: Any
    ) -> Agent:
        """An agent paired with a project that exists in the telemetry engine.

        The pairing is what makes the agent visible to every telemetry read, so
        this is the factory the runs, metrics and memory tests want.
        """
        index = self._next()
        resolved = name or f"Agent {index}"
        slug = fields.pop("slug", None) or f"agent-{index}"
        project_name = fields.pop("engine_project_name", None) or f"{workspace.slug}-{slug}"
        project = engine.add_project(project_name)
        return await self.agent(
            workspace,
            name=resolved,
            slug=slug,
            engine_project_id=project["id"],
            engine_project_name=project_name,
            **fields,
        )

    async def environment(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        env_type: str = EnvironmentType.PRODUCTION.value,
        status: str = EnvironmentStatus.HEALTHY.value,
        region: str | None = "eastus",
        health: float | None = 99.9,
    ) -> Environment:
        return await self.add(
            Environment(
                workspace_id=workspace.id,
                name=name or f"Environment {self._next()}",
                env_type=env_type,
                status=status,
                region=region,
                health=health,
            )
        )

    async def connector(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        connector_type: str = ConnectorType.API.value,
        status: str = ConnectorStatus.ACTIVE.value,
        risk_level: str = RiskLevel.LOW.value,
        **fields: Any,
    ) -> Connector:
        return await self.add(
            Connector(
                workspace_id=workspace.id,
                name=name or f"Connector {self._next()}",
                connector_type=connector_type,
                status=status,
                risk_level=risk_level,
                **fields,
            )
        )

    async def configuration(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        config_type: str = ConfigurationType.MODEL.value,
        environment: str = EnvironmentType.PRODUCTION.value,
        status: str = ConfigurationStatus.ACTIVE.value,
        impact: str = ImpactLevel.LOW.value,
        current_version: str | None = "v1.0.0",
        payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Configuration:
        row = await self.add(
            Configuration(
                workspace_id=workspace.id,
                name=name or f"Configuration {self._next()}",
                config_type=config_type,
                environment=environment,
                status=status,
                impact=impact,
                current_version=current_version,
                **fields,
            )
        )
        if current_version:
            await self.configuration_version(
                workspace,
                row,
                version=current_version,
                payload=payload or {"temperature": 0.2},
                is_current=True,
                status=status,
            )
        return row

    async def configuration_version(
        self,
        workspace: Workspace,
        configuration: Configuration,
        *,
        version: str,
        payload: dict[str, Any] | None = None,
        is_current: bool = False,
        status: str = ConfigurationStatus.ACTIVE.value,
        change_note: str | None = None,
        author_user_id: str | None = None,
    ) -> ConfigurationVersion:
        return await self.add(
            ConfigurationVersion(
                workspace_id=workspace.id,
                configuration_id=configuration.id,
                version=version,
                status=status,
                payload=payload or {},
                change_note=change_note,
                author_user_id=author_user_id,
                is_current=is_current,
                published_at=_now() if is_current else None,
            )
        )

    async def knowledge_source(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        source_type: str = KnowledgeSourceType.SHAREPOINT.value,
        status: str = KnowledgeSourceStatus.ACTIVE.value,
        **fields: Any,
    ) -> KnowledgeSource:
        return await self.add(
            KnowledgeSource(
                workspace_id=workspace.id,
                name=name or f"Knowledge {self._next()}",
                source_type=source_type,
                status=status,
                **fields,
            )
        )

    async def memory_store(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        store_type: str = MemoryStoreType.CONVERSATION.value,
        status: str = MemoryStoreStatus.ACTIVE.value,
        **fields: Any,
    ) -> MemoryStore:
        return await self.add(
            MemoryStore(
                workspace_id=workspace.id,
                name=name or f"Memory {self._next()}",
                store_type=store_type,
                status=status,
                **fields,
            )
        )

    # -- governance --------------------------------------------------------

    async def policy(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        category: str = PolicyCategory.GUARDRAILS.value,
        scope: str = PolicyScope.GLOBAL.value,
        scope_ref: str | None = None,
        status: str = PolicyStatus.ACTIVE.value,
        enforcement: str = PolicyEnforcement.BLOCK.value,
        risk_level: str = RiskLevel.MEDIUM.value,
        rules: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Policy:
        return await self.add(
            Policy(
                workspace_id=workspace.id,
                name=name or f"Policy {self._next()}",
                category=category,
                scope=scope,
                scope_ref=scope_ref,
                status=status,
                enforcement=enforcement,
                risk_level=risk_level,
                rules=rules or {},
                **fields,
            )
        )

    async def approval(
        self,
        workspace: Workspace,
        *,
        request_ref: str | None = None,
        action: str = "Refund over threshold",
        resource: str | None = "Order #4182",
        risk: str = RiskLevel.MEDIUM.value,
        status: str = ApprovalStatus.PENDING.value,
        agent_id: str | None = None,
        policy_id: str | None = None,
        requested_by_user_id: str | None = None,
        sla_due_at: dt.datetime | None = None,
        payload: dict[str, Any] | None = None,
        impact: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        now = _now()
        return await self.add(
            ApprovalRequest(
                workspace_id=workspace.id,
                request_ref=request_ref or f"REQ-{1000 + self._next()}",
                action=action,
                resource=resource,
                risk=risk,
                status=status,
                agent_id=agent_id,
                policy_id=policy_id,
                requested_by_user_id=requested_by_user_id,
                requested_at=now,
                sla_due_at=sla_due_at or now + dt.timedelta(hours=4),
                sla_label="4h",
                payload=payload or {},
                impact=impact or {},
                workflow=default_workflow(),
            )
        )

    async def secret(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        value: str | None = "sk-live-0123456789abcdef",
        secret_type: str = SecretType.API_KEY.value,
        vault: str = "Azure Key Vault",
        status: str = SecretStatus.ACTIVE.value,
        rotation_period_days: int | None = 90,
        privileged: bool = False,
        owner_user_id: str | None = None,
        **fields: Any,
    ) -> Secret:
        now = _now()
        return await self.add(
            Secret(
                workspace_id=workspace.id,
                name=name or f"Secret {self._next()}",
                secret_type=secret_type,
                vault=vault,
                status=status,
                ciphertext=encrypt_secret(value) if value else None,
                display_hint=mask_secret(value) if value else None,
                rotation_period_days=rotation_period_days,
                last_rotated_at=now if value else None,
                next_rotation_at=(
                    now + dt.timedelta(days=rotation_period_days)
                    if rotation_period_days
                    else None
                ),
                privileged=privileged,
                owner_user_id=owner_user_id,
                **fields,
            )
        )

    # -- operations --------------------------------------------------------

    async def deployment(
        self,
        workspace: Workspace,
        environment: Environment,
        *,
        deployment_ref: str | None = None,
        version: str = "v1.4.0",
        status: str = DeploymentStatus.SUCCEEDED.value,
        strategy: str = DeploymentStrategy.ROLLING.value,
        agent_id: str | None = None,
        with_stages: bool = True,
        stage_status: str = DeploymentStageStatus.COMPLETED.value,
        started_at: dt.datetime | None = None,
        finished_at: dt.datetime | None = None,
        **fields: Any,
    ) -> Deployment:
        now = _now()
        row = await self.add(
            Deployment(
                workspace_id=workspace.id,
                deployment_ref=deployment_ref or f"dep-{9000 + self._next()}",
                environment_id=environment.id,
                version=version,
                status=status,
                strategy=strategy,
                agent_id=agent_id,
                started_at=started_at or now - dt.timedelta(minutes=10),
                finished_at=finished_at
                if finished_at is not None
                else (now if status != DeploymentStatus.RUNNING.value else None),
                **fields,
            )
        )
        if with_stages:
            from fulcrum_ops_api.models.operations import DEFAULT_PIPELINE_STAGES

            await self.add_all(
                [
                    DeploymentStage(
                        deployment_id=row.id,
                        name=name,
                        sequence=sequence,
                        status=stage_status,
                        detail={},
                    )
                    for sequence, name in enumerate(DEFAULT_PIPELINE_STAGES, start=1)
                ]
            )
        return row

    async def quota(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        resource: str = QuotaResource.TOKENS.value,
        scope: str = LimitScope.WORKSPACE.value,
        scope_ref: str | None = None,
        limit_value: float = 1_000_000.0,
        used_value: float = 0.0,
        unit: str = "tokens",
        enforcement: str = QuotaEnforcement.WARN.value,
        status: str = LimitStatus.ACTIVE.value,
        period: str = LimitPeriod.MONTHLY.value,
        resets_at: dt.datetime | None = None,
    ) -> Quota:
        return await self.add(
            Quota(
                workspace_id=workspace.id,
                name=name or f"Quota {self._next()}",
                resource=resource,
                scope=scope,
                scope_ref=scope_ref,
                limit_value=limit_value,
                used_value=used_value,
                unit=unit,
                enforcement=enforcement,
                status=status,
                period=period,
                resets_at=resets_at or _now() + dt.timedelta(days=20),
            )
        )

    async def budget(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        amount_usd: float = 5_000.0,
        spent_usd: float = 1_250.0,
        **fields: Any,
    ) -> Budget:
        now = _now()
        return await self.add(
            Budget(
                workspace_id=workspace.id,
                name=name or f"Budget {self._next()}",
                amount_usd=amount_usd,
                spent_usd=spent_usd,
                period_start=now - dt.timedelta(days=5),
                period_end=now + dt.timedelta(days=25),
                **fields,
            )
        )

    async def alert(
        self,
        workspace: Workspace,
        *,
        title: str | None = None,
        severity: str = AlertSeverity.MEDIUM.value,
        status: str = AlertStatus.OPEN.value,
        source: str = "Policy Center",
        alert_ref: str | None = None,
        **fields: Any,
    ) -> Alert:
        return await self.add(
            Alert(
                workspace_id=workspace.id,
                alert_ref=alert_ref or f"ALT-{100 + self._next()}",
                title=title or f"Alert {self._next()}",
                severity=severity,
                status=status,
                source=source,
                raised_at=_now(),
                **fields,
            )
        )

    async def alert_rule(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        source: str = "Policy Center",
        condition: dict[str, Any] | None = None,
        **fields: Any,
    ) -> AlertRule:
        return await self.add(
            AlertRule(
                workspace_id=workspace.id,
                name=name or f"Rule {self._next()}",
                source=source,
                condition=condition or {"metric": "violations", "operator": "gt", "value": 5},
                **fields,
            )
        )

    # -- quality -----------------------------------------------------------

    async def guardrail(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        guardrail_type: str = GuardrailType.PII.value,
        status: str = GuardrailStatus.ACTIVE.value,
        action: str = GuardrailAction.BLOCK.value,
        scope: str = GuardrailScope.GLOBAL.value,
        scope_ref: str | None = None,
        threshold: float = 0.5,
        config: dict[str, Any] | None = None,
        **fields: Any,
    ) -> GuardrailConfig:
        return await self.add(
            GuardrailConfig(
                workspace_id=workspace.id,
                name=name or f"Guardrail {self._next()}",
                guardrail_type=guardrail_type,
                status=status,
                action=action,
                scope=scope,
                scope_ref=scope_ref,
                threshold=threshold,
                config=config or {},
                **fields,
            )
        )

    async def guardrail_event(
        self,
        workspace: Workspace,
        guardrail: GuardrailConfig,
        *,
        action_taken: str = GuardrailAction.BLOCK.value,
        agent_id: str | None = None,
        trace_id: str | None = None,
        score: float | None = 0.9,
        matched: dict | None = None,
    ) -> GuardrailEvent:
        return await self.add(
            GuardrailEvent(
                workspace_id=workspace.id,
                guardrail_id=guardrail.id,
                agent_id=agent_id,
                trace_id=trace_id,
                occurred_at=_now(),
                action_taken=action_taken,
                score=score,
                matched=matched if matched is not None else {},
            )
        )

    async def test_suite(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        suite_type: str = SuiteType.REGRESSION.value,
        environment: str = EnvironmentType.PRODUCTION.value,
        status: str = SuiteStatus.ACTIVE.value,
        dataset_ref: str = "golden-set",
        **fields: Any,
    ) -> TestSuite:
        return await self.add(
            TestSuite(
                workspace_id=workspace.id,
                name=name or f"Suite {self._next()}",
                suite_type=suite_type,
                environment=environment,
                status=status,
                dataset_ref=dataset_ref,
                **fields,
            )
        )

    async def evaluation_run(
        self,
        workspace: Workspace,
        *,
        name: str | None = None,
        dataset_ref: str = "golden-set",
        judge_model: str = "gpt-4o",
        status: str = EvaluationStatus.COMPLETED.value,
        **fields: Any,
    ) -> EvaluationRun:
        return await self.add(
            EvaluationRun(
                workspace_id=workspace.id,
                name=name or f"Evaluation {self._next()}",
                dataset_ref=dataset_ref,
                judge_model=judge_model,
                status=status,
                **fields,
            )
        )

    async def feedback_item(
        self,
        workspace: Workspace,
        *,
        rating: int | None = 4,
        sentiment: str = Sentiment.POSITIVE.value,
        source: str = FeedbackSource.END_USER.value,
        body: str | None = "Helpful answer",
        agent_id: str | None = None,
        feedback_ref: str | None = None,
        **fields: Any,
    ) -> FeedbackItem:
        return await self.add(
            FeedbackItem(
                workspace_id=workspace.id,
                feedback_ref=feedback_ref or f"FB-{500 + self._next()}",
                rating=rating,
                sentiment=sentiment,
                source=source,
                body=body,
                agent_id=agent_id,
                submitted_at=_now(),
                **fields,
            )
        )

    # -- licensing ---------------------------------------------------------

    async def license(
        self,
        workspace: Workspace,
        *,
        plan_code: str | None = None,
        status: str = LicenseStatus.ACTIVE.value,
        seats_purchased: int = 25,
        entitlements: dict[str, Any] | None = None,
        hard_limits: bool = True,
    ) -> TenantLicense:
        """A plan, a licence and its entitlements, in one call.

        ``entitlements`` maps key to value; booleans become boolean
        entitlements and integers integer ones, which is the distinction
        ``licensing.enforce_entitlement`` branches on.
        """
        plan = await self.add(
            LicensePlan(
                name="Enterprise",
                code=plan_code or f"plan-{self._next()}",
                tier=PlanTier.ENTERPRISE.value,
                billing_period=BillingPeriod.ANNUAL.value,
                included_seats=seats_purchased,
                status=PlanStatus.ACTIVE.value,
            )
        )
        now = _now()
        license_row = await self.add(
            TenantLicense(
                tenant_workspace_id=workspace.id,
                plan_id=plan.id,
                status=status,
                seats_purchased=seats_purchased,
                starts_at=now - dt.timedelta(days=30),
                expires_at=now + dt.timedelta(days=335),
            )
        )
        for key, value in (entitlements or {}).items():
            await self.entitlement(license_row, key, value, hard_limit=hard_limits)
        return license_row

    async def entitlement(
        self,
        license_row: TenantLicense,
        key: str,
        value: Any,
        *,
        hard_limit: bool = True,
    ) -> Entitlement:
        if isinstance(value, bool):
            kind, fields = EntitlementValueType.BOOL, {"bool_value": value}
        elif isinstance(value, int):
            kind, fields = EntitlementValueType.INT, {"int_value": value}
        else:
            kind, fields = EntitlementValueType.STRING, {"string_value": str(value)}
        return await self.add(
            Entitlement(
                license_id=license_row.id,
                key=key,
                value_type=kind.value,
                hard_limit=hard_limit,
                **fields,
            )
        )
