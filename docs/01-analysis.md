# Fulcrum Ops — Analysis

_Prepared 2026-08-18. Covers: current AWS hosting, the demo frontend's full feature
inventory, the telemetry engine's capabilities, and the feature→backend mapping._

---

## 1. Where `controlplane.fdprod.net` is hosted

| Fact | Value |
|---|---|
| DNS | `controlplane.fdprod.net` → **54.162.124.30** (resolved via 8.8.8.8) |
| DNS authority | **GoDaddy** nameservers (`ns45/ns46.domaincontrol.com`). The Route 53 zone `fdprod.net` (`Z07141131DWLETTXZJJIU`) in account 155954279114 exists but is **not delegated** — records there have no public effect. |
| Elastic IP | `eipalloc-0021c67954037b816`, tagged `fulcrum-ops-demo` |
| Instance | `i-04727f2dde4e32493` — **t3.micro**, Ubuntu 24.04, `us-east-1a` |
| Network | `vpc-0e91e1057d8c46f5c` / `subnet-0212fcd55c42fa9f9`, SG `sg-047aa4c9ca740b18d` (80/443) |
| IAM | instance profile `mcpcloud-ssm` (SSM only, no SSH key) |
| Web server | **Caddy**, one site block serving `controlplane.fdprod.net` + `54-162-124-30.sslip.io`, auto Let's Encrypt |
| Docroot | `/srv/fulcrum-ops` (`index.html`, `css/`, `js/`, `README.md`) |
| Deploy artifact | `s3://fulcrum-ops-demo-deploy-155954279114/fulcrum-ops-demo.zip` |
| **Host capacity** | **911 MB RAM, 6.8 GB disk, 2 vCPU, no Docker installed** |

**Account context (us-east-1):** 20 EC2 instances (14 running), 11 S3 buckets, **no RDS**,
no ECS/EKS in use for this workload. Five Elastic IPs, all attached.

**Conclusion:** the control plane is a pure static site. The host is 1/16th the size needed to
run a real backend — the engine stack alone needs 8–16 GB RAM. **A new instance is required.**

---

## 2. The demo frontend — complete feature inventory

23 screens, 6 nav groups, ~6,080 lines of vanilla JS. State lives in `js/data.js` across
**27 in-memory collections**: `people, agents, connections, connActivity, runs, connectors,
policies, approvals, auditEvents, configurations, prompts, knowledgeSources, quota, secrets,
memoryStores, testSuites, feedback, fbIssues, fbImprovements, fbBacklog, environments,
deployments, evaluations, guardrails, alerts, exportJobs, licensing, metrics`.

Shared machinery every screen depends on (`js/components.js`): a table engine with
search / per-column sort / dropdown filters + Clear All / pagination / rows-per-page /
row-action menus / CSV export; KPI cards with delta+direction; status & risk badges;
modals; toasts; dropdown menus; the inspector panel; and an audit-event logger (`C.logAudit`).

### PLATFORM

| Screen | Features |
|---|---|
| **Live Runs** | 4 KPI cards (Total Runs, Success Rate, Avg Latency, Policy Violations) + 4 sparkline mini-KPIs (Tokens Used, Estimated Cost, Fallback Rate, Human Escalations); connection status chips; 16-column run table (Run ID, Source, Agent, Status, Model, Input Preview, Tools, Tokens, Cost, Duration, Confidence, Risk, Policy, Time, Tenant); 6 filters (Tenant, Source, Status, Risk, Policy, Time Range); **live streaming** of new runs with LIVE pause/resume pill; `Run` action; row→inspector; modals for *Full Response* and *Execution Trace*; flag-for-review; CSV export; cross-links to Connections/Connectors/Agent Detail/Replay |
| **Replay Studio** | Run/Status/Duration/Fidelity KPIs; step-by-step player with Play Replay; per-step prompt, retrieval, tool and guardrail context; timeline |
| **Metrics** | 6 platform KPIs (Total Runs 30d, Success Rate, p50, p95, Tokens 30d, Cost 30d); per-model breakdown (gpt-4o, gpt-4o-mini, text-embedding-3-large, gpt-4-turbo, phi-3-medium); line charts + donut; Export |

### AGENT GOVERNANCE

| Screen | Features |
|---|---|
| **Connection Center** | 5 KPIs (Total/Connected/Warning/Disconnected/Last Sync); health donut; per-connection cards with platform logo, metadata pairs, latency, agents, syncs-today; activity feed; actions: Test Connection, Sync, Refresh All, Test All, Sync All, Add Connection, View Details, Manage Webhooks, View System Health; full activity log modal |
| **Agent Registry** | 6 KPIs (Total, Active, High Risk, Policy Violations, Pending Approval, Inactive); 10-column table; 6 filters (Source, Environment, Status, Risk Level, Policy Status, Owner); Columns picker; New Agent modal; row actions: View Details, Edit, Clone, Activate/Deactivate; export |
| **Agent Detail** | 9 tabs; run history table (Run ID, Status, Time, Model, Duration, Tokens, Cost, Policy, Confidence); connector/tool table (Type, Provider, Risk, Access, Last Used); actions: Run Agent, View Live Runs, Edit, Clone, Export Configuration, Open in Prompt Manager, Compare Versions (diff modal), Run Evaluation Now, Copy JSON, Create New Version; latency chart; cross-links to policies, prompts, connectors, evaluations, memory, configurations |
| **Connector & MCP Governance** | 5 KPIs (Total, Active, External, High Risk Tools, Blocked); 8-column table; 4 filters; Add Connector, Edit, Test Connection, Block/Unblock (modal); per-connector usage sparkline; used-by-agents cross-links |
| **Policy Center** | 6 KPIs (Total, Active, Warning, Blocked Actions 30d, Policies Violated 30d, Pending Review); 7-column table; 4 filters; Create Policy (modal), Edit, Clone, Activate/Deactivate, Import Policy, Export; enforcement modes; scope + category taxonomy |
| **Approvals & Audit** | 6 KPIs (Pending, Approved 30d, Rejected 30d, Escalated 30d, Avg Time to Approve, Approval SLA Met); pending queue table (Request ID, Time, Agent/Source, Action, Resource, Risk, Policy, Requested By, SLA, Status); **Approve / Reject / Escalate / Add Comment** state machine with 4-step workflow tracker; **immutable audit trail** tab (Actor, Detail, Source Screen, IP); New Approval Rule; sidebar pending badge |

### CONFIGURATION

| Screen | Features |
|---|---|
| **Configuration Center** | 6 KPIs (Total, Active, Draft, Deprecated, Archived, Changes 30d); 8-column table; 5 filters; New Configuration, Edit, Clone, **New Version**, Deprecate, **Rollback to Previous**, Import/Export; validation flow with progress |
| **Prompt Manager** | 5 KPIs (Total, Approved, In Review, Blocked, Avg Success Rate); 9-column table; version history per prompt (version, status, change note, author, date); New Prompt, Edit, Test Prompt, draft→review→approve lifecycle; per-prompt run counts and success rate |
| **RAG & Knowledge Governance** | 6 KPIs (Sources, Active, Documents, Chunks, Avg Grounding Score, Sources with ACL); 10-column table; 4 filters; Add Source, **Sync Now** (progress %), Edit, View Documents (modal), Delete; sensitivity + ACL tracking |
| **Secrets & Credentials** | 11 KPIs incl. type breakdown (API Key, Service Principal, OAuth Client, Certificate, Connector Credential) + Compliance Score, Expiring Soon, Rotation Overdue, Privileged Access 30d; 10-column table; 4 filters; **masked values with audited reveal**; Rotate Secret (multi-step flow), Add Secret, Edit Access, Disable/Enable, View Audit Log; vault reference copy |

### OPERATIONS

| Screen | Features |
|---|---|
| **Quota, Cost & Capacity** | 6 KPIs (Spend MTD, Budget MTD, Tokens MTD, API Calls MTD, Avg Cost/1K Tokens, Capacity Health); per-model cost breakdown; cost-by-service table; budgets with thresholds; top cost drivers; quota table; capacity table; team allocation; insights; Create Quota, Manage Budgets, New Budget, Edit Thresholds, Request Increase; charts |
| **Memory & State** | 6 KPIs (Stores, Active Sessions, Stored Memories, Avg Retrieval Latency, State Sync Success, Expired/Purged); 9-column table; 4 filters; Create Store, View Records, Update Retention Policy, Purge Data, Create Backup, Restore |
| **Deployment & Environment** | 6 KPIs (Environments, Active Deployments, Successful, Failed, Avg Deployment Time, Rollbacks); environment table (Type, Region, Status, Active Deployments, Health); **Create Deployment** runs an animated Build → Automated Tests → Security Scan → Approval → Deploy pipeline; Promote, Halt, Approve, Restart Services, Environment Settings; deployment history |

### QUALITY

| Screen | Features |
|---|---|
| **Evaluations** | 5 KPIs (Evaluations 30d, Avg Score, Test Cases Run, Regressions Caught, Judge Model); table with **Correctness / Grounding / Faithfulness / Safety** scores per agent+dataset; Run Evaluation (modal, progress), Re-run, baseline comparison; trend chart |
| **Guardrails** | 5 KPIs (Active, Triggers 30d, Blocked 30d, PII Items Masked 30d, Avg Added Latency); 8-column table (Type, Status, Action, Triggers, Blocked, Effectiveness, Last Triggered); New Guardrail, **Test** (modal), Tune threshold, Enable/Disable |
| **Testing & Regression** | 6 KPIs (Suites, Tests Executed 30d, Pass Rate, Regression Detected, Avg Duration, Flaky Tests); 8-column table; 4 filters; Create Suite, **Run Suite** (progress), Promote Baseline, Compare Baselines, View Schedules, Create Evaluation |
| **Feedback & Quality Loop** | 15 KPIs incl. source breakdown (End User In-App, Agent Response Rating, Support Tickets, Manual Review) and a funnel (received → auto-clustered → issues opened → backlog → deployed); feedback table (Rating, Sentiment, Source, Run ID); Submit Feedback, **Analyze Feedback** (clustering modal), Create Issue, Add to Backlog, Assign to Team, Create Fix Task, Edit SLA Rules |

### SYSTEM

| Screen | Features |
|---|---|
| **Alerts** | 5 KPIs (Open, Critical, Investigating, Acknowledged, MTTA); table (Severity, Alert, Source, Status, Time); 3 filters; Acknowledge / Resolve / Assign / Mute / Acknowledge All; Alert Rules modal; **sidebar unread badge**; Open Source cross-link to originating screen |
| **Exports** | 4 KPIs (Exports 30d, Scheduled, Total Volume, Failed); table (Format, Rows, Size, Requested By, Status, Source Screen); New Export (modal) with async generation + download; scheduling |
| **Licensing & Entitlements** | 6 KPIs (Plans, Active Tenant Licenses, Seats Assigned, Expiring in 30 Days, Suspended/Revoked, Overage Alerts); plans, tenants and entitlements tables; Create Plan, Manage Tenant, Reassign Seat, Purchase Seats, Suspend/Reactivate, Invoice PDF |

---

## 3. The telemetry engine — what it actually provides

Java 21 / Dropwizard, 1,689 source files. Data plane: **ClickHouse** (traces, spans, feedback
scores, threads, experiment items) + **MySQL** (control state) + **Redis** (streams, cache,
rate limits, online-scoring queues) + **MinIO/S3** (attachments). Optional side services:
a Python sandbox runner (user-defined metrics), a guardrails inference service, an OTel collector.

**API surface: 47 resource classes under `/v1/private/*` and `/v1/internal/*`.** Notably the
paths are already vendor-neutral. Highlights:

- `traces` — list/search (streamed), get, create, **batch create**, patch, batch patch, delete,
  `/stats`, `/exists`, feedback scores (single + batch), comments, **`/threads`** (+ search, stats)
- `spans` — same shape, plus span-level feedback scores and comments
- `projects`, `environments`, `agent-configs`, `blueprints` (versioned config with deltas/diff/history)
- `datasets` (+ items, bulk, from-csv/json, streaming, versions, export-jobs), `experiments`
  (+ items, bulk, groups, aggregations), `optimizations`
- `prompts` — **full versioning**: versions, by-commit, hash, tags, restore, diff
- `automations/evaluators` — online scoring: LLM-as-judge, user-defined Python metrics,
  thread-level judges, samplers
- `guardrails`, `feedback-definitions`, `annotation-queues`, `assertion-results`, `manual-evaluation`
- `alerts` (+ webhook tests/examples; Slack and PagerDuty payload adapters exist)
- `llm-provider-key` (encrypted), `llm/models`, `chat/completions` (playground)
- `dashboards`, `insights-views`, `agent-insights` (+ scheduled report jobs)
- `retention/rules` (+ sliding-window and catch-up jobs), `internal/usage`, `costs`, `costs/summaries`
- `otel/v1` — **OpenTelemetry ingest**, so any OTel-instrumented agent can report
- attachments, workspaces, workspace-permissions, toggles, MCP OAuth bundle

**Auth:** `authentication.enabled` defaults to **false** in self-hosted mode → the engine trusts
its caller and resolves everything from a workspace header. That is exactly what we want: the
engine runs on a private network and **our** control plane is the sole identity authority.

---

## 4. Feature → backend mapping

**DIRECT** = engine endpoint serves it as-is · **ADAPT** = engine data, reshaped by our API ·
**BUILD** = no engine equivalent, our own service + database

| Screen | Verdict | Backing |
|---|---|---|
| Live Runs | **ADAPT** | `POST /v1/private/traces/search`, `/traces/stats`, `/spans` for tool chips; our API adds SSE fan-out, tenant/risk/policy joins |
| Replay Studio | **DIRECT** | `GET /traces/{id}` + `GET /spans?trace_id=` (span hierarchy is already modelled) |
| Metrics | **DIRECT** | `/traces/stats`, `/spans/stats`, `/costs/summaries`, `/internal/usage` |
| Connection Center | **BUILD** | our connector registry + health prober; per-connection sync/latency history |
| Agent Registry | **ADAPT** | our `agents` table, 1:1 mapped to engine **projects**; counts/latency from `/traces/stats` |
| Agent Detail | **ADAPT** | our agent record + `/traces` filtered by project + `/prompts` + `/experiments` |
| Connector & MCP Governance | **ADAPT** | our registry for governance fields; usage stats from spans of type `tool` |
| Policy Center | **BUILD** | our policy engine; enforcement hooks into engine `guardrails` + `automations/evaluators` |
| Approvals & Audit | **BUILD** | our approvals state machine + append-only audit log |
| Configuration Center | **ADAPT** | engine `blueprints` gives versioning/diff/history/rollback; our layer adds status + impact |
| Prompt Manager | **DIRECT** | `/v1/private/prompts` + versions, tags, restore, diff |
| RAG & Knowledge | **ADAPT** | our source registry; grounding scores from feedback scores on retrieval spans |
| Secrets & Credentials | **BUILD** | our Fernet-encrypted vault + rotation + audited reveal (engine's `llm-provider-key` is the pattern, provider-keys only) |
| Quota, Cost & Capacity | **ADAPT** | `/costs`, `/costs/summaries`, `/internal/usage`; our budgets/thresholds/capacity |
| Memory & State | **ADAPT** | engine **trace threads** = sessions/conversation state; our retention + backup metadata |
| Deployment & Environment | **BUILD** | our pipeline runner; engine `environments` for the env registry |
| Evaluations | **DIRECT** | `datasets` + `experiments` + `automations/evaluators` (LLM-as-judge) + feedback scores |
| Guardrails | **DIRECT** | `/v1/private/guardrails` + the guardrails inference service |
| Testing & Regression | **ADAPT** | experiments as suite runs + `assertion-results`; our scheduling and baselines |
| Feedback & Quality Loop | **ADAPT** | trace feedback scores + comments + `feedback-definitions`; our issue/backlog/SLA workflow |
| Alerts | **ADAPT** | engine `alerts` + webhooks for telemetry-sourced alerts; our alerts for governance sources |
| Exports | **ADAPT** | engine `export-jobs` for datasets; our generic export service for every other screen |
| Licensing & Entitlements | **BUILD** | our plans/seats/entitlements; enforced via quota checks at ingest |

**Roughly:** 4 screens DIRECT, 13 ADAPT, 6 BUILD.

### Engine capabilities the demo does *not* yet surface (free upside)

Playground/chat completions · annotation queues (human labelling) · optimization runs ·
dashboards & insights views · scheduled AI insight reports · dataset versioning ·
experiment comparison & aggregations · attachments on traces · retention rules with
sliding windows · **OpenTelemetry-native ingest** · Slack/PagerDuty alert delivery.

---

## 5. Licensing note

The engine is **Apache-2.0**. That permits commercial redistribution and white-labelling: the
licence grants no trademark rights and imposes **no attribution requirement in a product UI**.
Section 4 does require that redistributed *source or binaries* retain the licence text,
copyright notices and any `NOTICE` content. The clean way to satisfy both the requirement
("nothing vendor-branded anywhere in the product") and the licence is:

- product surfaces — UI, SDK, API, docs, package names, env vars — carry **only Fulcrum Ops branding**;
- a `THIRD_PARTY_LICENSES.md` in the repo (not shipped to users, not rendered in the app) retains
  the upstream licence text.

This is standard practice for OEM'd open-source components and is what the architecture assumes.
