# Fulcrum Ops — make it fully functional and bulletproof

You are taking over a working but unfinished AI agent control plane. It is live,
it has real telemetry flowing, and roughly half its screens have never been
exercised with real data. Your job is to finish it: verify what exists, use it
as a real user would, find what breaks, and fix it.

Work from evidence. Do not trust this document over what you observe.

> **Corrected 2026-09-18.** This brief was written on 2026-08-24, before the
> repository existed, and three of its statements had since become false: that
> the code is not under version control, that a restore has never been tested,
> and that the encryption key exists only on the host. They are corrected in
> place below, each with where the evidence is. The rest of "What is not done"
> is as it was written — `git log` is the record of what has been closed since,
> and the 2026-09-18 audit (branch `fix/audit-2026-09-18`) is the current list
> of what is open.

## The product

Fulcrum Ops governs AI agents. Teams register agents, connect them with an SDK
or over OpenTelemetry, and the platform records every run and enforces policy,
guardrails, entitlement and quota **at ingest**, before telemetry is stored.

Twenty-four console screens over one FastAPI control plane, with a white-labelled
Apache-2.0 telemetry engine behind a private adapter.

**The governing principle, which you must not violate:** nothing in the console
is simulated. If a number is on screen a service computed it. If a service
cannot answer, the screen says so. A dash means *not measured* and is never
rendered as zero. Any fix that invents, interpolates or zero-fills a number is
wrong even when it looks better. When you are tempted, make the screen explain
itself instead.

## Where everything is

| Thing | Location |
|---|---|
| Source | `C:\Users\FDUSI0013\fulcrum-ops\` — a git repository; `origin` is AWS CodeCommit `fulcrum-ops` (us-east-1). History starts 2026-08-24 |
| Control plane | `apps/control-plane` (FastAPI, Python 3.12, venv at `.venv`) |
| Console | `apps/web` (vanilla JS, no build step) |
| SDKs | `sdks/python`, `sdks/typescript` |
| Docs | `docs/user-guide.html` (master) + `Fulcrum-Ops-Operator-Manual.docx` |
| Onboarding skill | `skills/connect-agent/SKILL.md` |
| Agent fleet | `C:\Users\FDUSI0013\fulcrum-ops-agents\` (keys in `.keys.env`) |
| Live console | https://controlplane.fdprod.net |
| Host | AWS EC2 `i-02c888d6ca6f07a6b`, us-east-1, **SSM only — no SSH key exists** |
| On-host root | `/opt/fulcrum/`, compose stack in `/opt/fulcrum/deploy/` |
| Owner password | `/opt/fulcrum/owner-password.txt` on the host (read it via SSM) |
| Owner account | `ops@fulcrumops.com` |
| Test user | `dana.rivers@fulcrumops.com` (operator role) |

Azure: subscription `TMNA_Demo_FD`, resource group `rg-FDUSE05569-0164`, eastus2.
Foundry project `fduse05569-0164` with a **gpt-5** deployment. Two Container Apps
run a demo agent (`incident-triage`, `incident-triage-fulcrum`). `az` is
installed at `C:\Users\FDUSI0013\azure-cli-venv\Scripts\az.bat`.

## How to deploy a change

There is no CI. The established path:

```
tar the changed dirs -> aws s3 cp to s3://fulcrum-ops-demo-deploy-155954279114/prod/
-> aws s3 presign -> aws ssm send-command runs curl+tar+docker compose build/up
-> deploy/publish-console.sh ../apps/web /srv/fulcrum-ops
```

Gates that must pass before any deploy:

```
apps/control-plane/.venv/Scripts/python.exe -m pytest          # 615 when this was written; it has grown, and takes ~30 min
apps/control-plane/.venv/Scripts/python.exe -m ruff check src
apps/control-plane/scripts/check_contract.py                   # console<->API
scripts/check-branding.sh                                      # no vendor strings
scripts/check-config.sh                                        # deploy XML, compose, Caddyfile, shell
```

A host deployed before 2026-09-18 needs two one-time steps **before** its next
`docker compose up -d` — the keeper's transaction log has to be moved onto a
named volume first, or the analytics store comes back read-only. They are in
`deploy/README.md` under "Upgrades".

## Environment traps, already paid for

- **The corporate proxy intercepts `aka.ms` but not Azure endpoints.** `az extension add`
  fails on certs until you set
  `REQUESTS_CA_BUNDLE=C:\Users\FDUSI0013\fulcrum-ops-agents\windows-ca-bundle.pem`.
- **`az acr build` reports failure while the build succeeds** — the proxy kills the
  log stream. Always check `az acr task list-runs --registry cab4fada9eefacr`
  before retrying.
- **Role assignment is blocked by an ABAC condition** in that Azure tenant even with
  Contributor + Foundry Owner, so managed identity could not be granted. Container
  Apps authenticate with an API key in a Container App secret instead.
- **gpt-5 rejects any `temperature` but its default.** Omit the parameter.
- **Cost resolves on model + provider.** Azure-served OpenAI models want provider
  `azure`, not `azure-openai`, and the real model name, not a deployment alias.
- **Large bash heredocs fail in this harness.** Keep them under ~120 lines.
- **`UID` is readonly in bash.** Use another variable name.
- The Bash tool cannot run dev servers; use the preview tooling.

## What is done

Live and verified: login and sessions, six roles enforced server-side,
concurrent multi-user sessions, Live Runs with an SSE stream, Replay Studio,
Metrics with per-agent filtering, Agent Registry and Agent Detail, API key
issuance with agent binding, user management, Hosting & Deployment with observed
tool-call traffic, Prompt Studio with model execution, licensing with
entitlement seeding, the Python and TypeScript SDKs including feedback helpers,
OTLP ingest, and four agents that have reported real telemetry across AWS, this
workstation and Azure.

## What is not done — your work

### 1. Thirteen screens have never held real data

Confirmed empty in production right now: **connectors, approvals, configurations,
prompts, knowledge, secrets, evaluations, testing, feedback, alerts, exports,
memory, deployments** (policies and guardrails have one row each).

These were documented from their code, not from use. Populate each with realistic
data **through the product's own API or UI**, then use every control on the
screen. Expect to find bugs — the last sweep of this kind found eight
engine-contract defects, a silent-zero cost bug, and a replay bug that loaded the
wrong run. Fix what you find, add a regression test for each, and correct the
documentation where it describes behaviour that turns out to be wrong.

### 2. Prove the flows end to end, as a user

Do not merely call endpoints. Drive the console and confirm the loop closes:

- Register an agent, mint a bound key, connect a **new** agent you write, and see
  its run appear with the right model and non-zero measured tokens.
- Create a policy that blocks something, then make an agent trip it, and verify
  the run is refused and a violation is recorded.
- Register a connector, grant it, **block** it, and confirm a granted agent then
  fails closed.
- Raise an approval, approve it as one user while signed in as another, and check
  the audit chain verifies.
- Run an evaluation against a dataset and see the judged average on the trend.
- Configure Prompt Studio execution (env vars on the host; the Azure gpt-5
  deployment works) and run a prompt.
- Exercise a quota to exhaustion and confirm ingest refuses rather than reports
  an overspend after the fact.
- Generate an export and download it. Schedule one.

### 3. Use the documentation as a new user would

`docs/user-guide.html` is the master; the .docx is generated from it by
`build-docx.js`. Follow it literally as though you had never seen the system.
Every place it is wrong, incomplete, or assumes knowledge you do not have is a
documentation bug — fix it, regenerate both formats, and republish.

Two such bugs were found by the 2026-09-18 audit and are **not yet fixed in the
master** (nor in its served copy, `apps/web/docs/index.html`). The SDK and
`sdks/python/README.md` are already right; the guide has to catch up with them:

- **`report_issue()`** ("Scores and feedback"). The guide says it "raises a row
  on Feedback and Quality Loop". What it does: it is delivered as negative
  feedback (`feedback.submitted`, source *Agent Response Rating*) and appears in
  the Feedback inbox with the title leading the body. It does not create a row
  on Feedback > Issues by itself.
- **`track_openai`** ("Getting cost to resolve" and "Provider wrappers"). The
  guide calls the Azure client "duck-typed, works the same". Say instead that it
  reports provider `azure` for an `AzureOpenAI` or Azure AI Foundry client
  (overridable with `provider=`), and that `model=` is the fallback for calls
  that name no model, as with a Foundry agent addressed by `agent_reference`.

One check behind the second bullet is still owed, because nobody could reach the
engine while the fix was written: post a `gpt-4o` span with provider `openai`
and one with provider `azure`, see which the engine prices, and make the guide
and the SDK agree with what you observe.

### 4. Close the three Red risks

- ~~**The code exists only on this laptop.**~~ **Closed.** The source is a git
  repository with its remote on AWS CodeCommit (`git remote -v`; `git log` goes
  back to the initial import on 2026-08-24). What is still true: nothing pushes
  for you, so an unpushed branch is still only on this laptop.
- **No CI.** Nothing gates a deploy but discipline. Still open.
- **Single host, no HA.** Still open. The other two halves of this risk are
  closed:
  - ~~untested restore~~ — a restore of the nightly dump into a fresh database
    was proven on 2026-08-24; the command is in the header of `deploy/backup.sh`
    and in `deploy/README.md`. Prove it again after any change to the backup.
  - ~~the encryption key lives only on the host~~ — `APP_ENCRYPTION_KEY` is
    escrowed off the host in SSM Parameter Store,
    `/fulcrum-ops/prod/APP_ENCRYPTION_KEY` (us-east-1), and the dumps are
    mirrored to S3. A dump plus that parameter is a full recovery; either alone
    is not.

  What the 2026-09-18 audit found underneath that: the nightly backup exited 0
  when the host had no `aws` CLI on cron's `PATH`, so "mirrored to S3" was never
  checked by anything. `backup.sh` now fails loudly (non-zero, and a
  `BACKUP_FAILED` marker beside the dumps) and verifies each upload — **confirm
  on the host that last night's dump is actually in the bucket** before relying
  on it.

### 5. Harden it

Probe for what a hostile or clumsy user does: oversized payloads, malformed
batches, concurrent writes to the same row, expired and revoked keys, a
workspace with no licence, an agent deleted mid-run, the engine down (stop the
container and confirm governance still works and telemetry fails closed with
`telemetry_unavailable`), session expiry mid-action, and every list endpoint
under a large page size. Check rate limiting and body caps exist and hold.

Re-run the tenancy suite and satisfy yourself that a second workspace cannot see
the first — production has only ever run one tenant.

### 6. Finish the loose ends

- The SDK ships as a local wheel; put it on a private index.
- Seats are purchased but unassigned on the current licence.
- Connection traffic has never been seen populated — it needs a connection whose
  kind matches a registered agent's platform.
- Only the AWS agent is still running; the local and Azure agents are stopped.
- Runs from before the cost fix still show `$0`; decide whether to backfill or
  leave them honest, and say which.

## How to work

Verify before you claim. Every "it works" in your final report must name what you
observed — a run id, a status code, a screenshot, a test that passed. This
codebase was built to be honest about what it knows; hold your own reporting to
the same standard.

Prefer a small fix with a regression test over a large refactor. Keep the house
style: the code explains *why* in comments, not *what*. Read a neighbouring
module before adding to one.

When you find something genuinely ambiguous, say so and choose the reading that
does not invent data. When you find something broken that you cannot fix safely,
report it plainly rather than working around it.

Finish by telling me: what you tested and how, what you fixed, what is still
broken, and what you would do next.

## Deferred: the schema changes this branch deliberately did not make

The audit pass of 2026-09-18/19 fixed 229 findings across 34 file owners working
in parallel. Seventeen of the fixes they proposed need a database migration, and
none was made: concurrent owners writing concurrent Alembic revisions fork the
revision chain, and a bug-fix release is the wrong place to change the schema.
Each is written out below with what it is for, because the reason a column is
wanted is the part that gets lost.

They are independent of one another. Take them one at a time, each as its own
revision, each with the data repair it needs — several of the indexes below are
unique and will refuse to build until existing duplicates are resolved, which is
itself the evidence that the constraint was missing.

**Correctness, in rough order of how much it matters**

1. `configuration_versions`: a partial unique index on `(configuration_id)` where
   `is_current`, plus a repair pass keeping the row whose `version` matches
   `configurations.current_version`. Two current versions is a state the service
   now prevents but the table still permits.
2. `seat_assignments`: unique on `(license_id, user_id)` where `released_at IS
   NULL`. The row lock closes the race today; the index is what makes a double
   seat impossible rather than merely unlikely.
3. `backlog_items`: partial unique on `issue_id` where the item is open, after
   de-duplicating. Backs the one-item-per-issue rule the service already keeps.
4. `alerts`: partial unique on `(workspace_id, dedupe_key)` where the alert is
   unresolved. `raise_alert` already absorbs the `IntegrityError` onto the twin,
   so nothing in the service changes once the index exists — two simultaneous
   raises of one condition simply stop producing two alerts.
5. `policies` data repair: `UPDATE policies SET enforcement = rules->'action'->>'mode'`
   where the two disagree. Sets the column to what ingest has really been
   enforcing; changes no behaviour, but until it runs the `?enforcement=` filter
   and the Policy Center's column can disagree with the rule that actually fires.

**Performance**

6. `policy_violations (policy_id, occurred_at)` — serves the 30-day rollup the
   scheduler now recomputes and the inspector's Violations tab.
7. `audit_events (workspace_id, entity_id, occurred_at)` — the existing
   `(entity_type, entity_id)` index cannot serve the entity-history filter behind
   Agent Detail and the connector and policy inspectors. Build it
   `CONCURRENTLY`: this is the table that grows fastest.

**New capability, each a small feature rather than a fix**

8. `agents.invoke_url` + `invoke_secret_id`: let the console call an agent's own
   runtime instead of parking a run and hoping something picks it up.
9. `pending_runs` (or `triggered_runs`): a real work queue for console-issued
   runs. Today a runtime polls `GET /runs?status=Running` every few seconds and
   the control plane answers by scanning telemetry — the single most expensive
   repeated query in production. An indexed table replaces a scan with a lookup,
   and lets ingest merge the trigger's own metadata into the reported trace.
   (The 2026-09-19 work made the scan much cheaper and made an abandoned request
   report `Failed` rather than `Running` for ever. This removes the scan.)
10. `evaluation_runs.progress` (JSON): so a worker that does not own a run can
    still show its live judged count instead of zero during Scoring.
11. `api_key_usage_daily`: real per-key metering, one upserted row per key per
    day, replacing the estimate the key usage modal shows now.
12. `users.credentials_changed_at`: stamp it on every password change and carry
    it as a session claim, so changing a password invalidates sessions issued
    before it. Today it does not.
13. `connections.status_detail`: hold the last probe diagnostic in its own column
    instead of deriving it back out of the operator's note.

**Not a migration, and already done on this branch**: `SecretAccessAction.REVOKE`
(the column is a plain string) — the vault was writing `revoke` rows that
`?action=revoke` then refused to filter for.
