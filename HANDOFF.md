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

> **State on 2026-09-21.** Production runs `e25193b`, at database revision
> `a41c6b58d902`. `main` contains it -- the audit branch `fix/audit-2026-09-18`
> is merged -- and the commits on `main` after it change only tests and
> documentation, so the running code is `main`'s. Verified live after the deploy: 175 read operations with 0
> failures and 0 slow, all 10 containers healthy; full suite 1315 passed. What
> remains needs a decision rather than code: the prompt-injection guardrail needs
> the scanner's privately distributed model (an HF token plus two model settings
> on `safety-scanner`), and items 8–11 below are new features. The old demo
> instance `i-04727f2dde4e32493` is STOPPED, not terminated.

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

Two such bugs were found by the 2026-09-18 audit. **Both are fixed in the master
as of 2026-09-19**, and `build-docx.js` has been re-run, so the .docx and the
console's copy in the repository (`apps/web/docs/index.html`) carry the
correction too. That file is what `deploy/publish-console.sh` puts on the host,
which means it is ahead of what the site serves until the next deploy — check
https://controlplane.fdprod.net/docs/ rather than assuming. What the
guide now says, matching the SDK and `sdks/python/README.md`:

- **`report_issue()`** ("Scores and feedback"). It is delivered as negative
  feedback (`feedback.submitted`, source *Agent Response Rating*) and appears in
  the Feedback inbox with the title leading the body. It does not create a row
  on Feedback > Issues by itself — issues are raised from themes on that screen.
  The guide previously claimed it "raises a row on Feedback and Quality Loop".
- **`track_openai`** ("Getting cost to resolve", "Provider wrappers" and the
  cost row in Troubleshooting). It reports provider `azure` for an `AzureOpenAI`
  client or an Azure AI Foundry endpoint (overridable with `provider=`), and
  `model=` is the fallback for calls that name no model, as with a Foundry agent
  addressed by `agent_reference`. The guide previously called the Azure client
  "duck-typed, works the same", which left the reader to set the provider.

One check behind the second bullet is **still owed**, because nobody could reach
the engine while any of this was written: post a `gpt-4o` span with provider
`openai` and one with provider `azure`, see which the engine prices, and make
the guide and the SDK agree with what you observe. Until that is done, both are
stating what the SDK sends, not what the price table was seen to accept.

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

- ~~The SDK ships as a local wheel; put it on a private index.~~ **Done.**
  The control plane serves its own PEP 503 index at `/pypi/simple`, which is
  now the documented install route:
  `pip install --extra-index-url https://controlplane.fdprod.net/pypi/simple fulcrum-ops`.
  The wheel lives in `apps/web/pypi/` and is published with the console.
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

## The schema changes: what is written, and what is left

The audit pass of 2026-09-18/19 fixed 229 findings across 34 file owners working
in parallel. Thirteen of the fixes they proposed looked like they needed a
database migration — twelve turned out to, and item 13 was withdrawn on
inspection (its entry says why). None was made during that pass: concurrent
owners writing concurrent Alembic revisions fork the revision chain, and a
bug-fix release is the wrong place to change the schema. Each is written out
below with what it is for, because the reason a column is wanted is the part
that gets lost.

**Items 1–7 and 12 shipped on 2026-09-21** (release `e25193b`), as three
revisions on top of `51168850ae0f`, in this order:

| revision | what |
| --- | --- |
| `7b2e4c9a10d3` | items 1–4 (the four partial unique indexes, each with its repair) and 6–7 (the two read indexes) |
| `8f3d1c07a2be` | item 12, `users.credentials_changed_at` |
| `a41c6b58d902` | item 5, the `policies.enforcement` repair |

Check the deployed state before trusting this paragraph — a document says what
was true when it was written, and the host says what is true now:

```
aws ssm send-command --instance-ids i-02c888d6ca6f07a6b --document-name AWS-RunShellScript   --parameters 'commands=["cd /opt/fulcrum/deploy && docker compose exec -T control-plane alembic current"]'
```

Each was rehearsed against the production database before being proposed for a
release — rendered with `alembic upgrade 51168850ae0f:head --sql`, wrapped in
`BEGIN … ROLLBACK` by hand (the rendered SQL ends in `COMMIT;`, so running it
unaltered would apply it), run on the host, and read back. Every repair matched
**zero rows**: production has no duplicates and no policy disagreeing with its
own rule body, so the indexes go on clean. `scripts/` has no runner for this; the
procedure is written out in the header of `deploy/ship.sh`.

Each repair keeps the row the service that owns that table would have returned —
`_current_version` for configurations, `_planning_item` for the backlog,
`_live_alert` for alerts, the reassign path's `ORDER BY assigned_at ASC` for
seats — and the two denormalised counters that shadow those tables
(`configurations.current_version`, `tenant_licenses.seats_assigned`) are brought
along in the same revision. A repair that picked a defensible row rather than
*that* row would silently change what the product serves, which is not a repair.

None of the three is built `CONCURRENTLY`. `policy_violations` (114k rows) and
`audit_events` (18k) are small enough that the ordinary lock is milliseconds,
and `CONCURRENTLY` cannot run inside the transaction Alembic wraps a revision
in. On a deployment where these have reached millions of rows, build them by
hand first and let the revision find them already present.

**Rolling any of them back means running `downgrade()`, not just putting the
previous image back.** The control plane migrates itself at start, from scripts
baked into its own image, so an image that predates a revision cannot resolve the
revision the database reports: it exits 255 before uvicorn and
`restart: unless-stopped` turns that into a crash loop. `deploy/ship.sh`'s
`restore()` handles it — it downgrades to the revision recorded before the
deploy while the new image is still the tagged one, and refuses to swap the image
at all if that downgrade fails. Any revision added here needs a `downgrade()`
that actually works.

**Items 8–11 are still open**, and item 13 was withdrawn (see its entry). They
are independent of one another. Take them one at a time, each as its own
revision, each with the data repair it needs.

**Correctness, in rough order of how much it matters**

1. **(shipped 2026-09-21, `7b2e4c9a10d3`)** `configuration_versions`: a partial unique index on `(configuration_id)` where
   `is_current`, plus a repair pass keeping the row whose `version` matches
   `configurations.current_version`. Two current versions is a state the service
   now prevents but the table still permits.
2. **(shipped 2026-09-21, `7b2e4c9a10d3`)** `seat_assignments`: unique on `(license_id, user_id)` where `released_at IS
   NULL`. The row lock closes the race today; the index is what makes a double
   seat impossible rather than merely unlikely.
3. **(shipped 2026-09-21, `7b2e4c9a10d3`)** `backlog_items`: partial unique on `issue_id` where the item is open, after
   de-duplicating. Backs the one-item-per-issue rule the service already keeps.
4. **(shipped 2026-09-21, `7b2e4c9a10d3`)** `alerts`: partial unique on `(workspace_id, dedupe_key)` where the alert is
   unresolved. `raise_alert` already absorbs the `IntegrityError` onto the twin,
   so nothing in the service changes once the index exists — two simultaneous
   raises of one condition simply stop producing two alerts.
5. **(shipped 2026-09-21, `a41c6b58d902`)** `policies` data repair: `UPDATE policies SET enforcement = rules->'action'->>'mode'`
   where the two disagree. Sets the column to what ingest has really been
   enforcing; changes no behaviour, but until it runs the `?enforcement=` filter
   and the Policy Center's column can disagree with the rule that actually fires.

**Performance**

6. **(shipped 2026-09-21, `7b2e4c9a10d3`)** `policy_violations (policy_id, occurred_at)` — serves the 30-day rollup the
   scheduler now recomputes and the inspector's Violations tab.
7. **(shipped 2026-09-21, `7b2e4c9a10d3`)** `audit_events (workspace_id, entity_id, occurred_at)` — the existing
   `(entity_type, entity_id)` index cannot serve the entity-history filter behind
   Agent Detail and the connector and policy inspectors. Build it
   `CONCURRENTLY` on a deployment where it has grown large — on this one it is
   18k rows and `7b2e4c9a10d3` builds it inline, for the reason given above.

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
12. **(shipped 2026-09-21, `8f3d1c07a2be`)** `users.credentials_changed_at`: stamped when a
    password is *rotated* — by its owner or by an admin reset, but not by the
    re-hash a sign-in may perform — and compared against the token's `iat` in
    `_principal_from_session`, which already has the user row loaded. Changing a
    password now ends every other session on the account; the caller doing the
    changing keeps working because `change_password` re-issues its cookie after
    the stamp. `iat` was widened from whole seconds to a float in the same
    change: at second resolution a token minted in the same second as the change
    could not be told from one minted just after it, and would have survived for
    the rest of its life.
13. ~~`connections.status_detail`~~ **— withdrawn, and the reason is worth
    keeping.** The defect was that the probe wrote its diagnostic over whatever
    the operator had written in `note`. The audit fix stopped that and now
    derives the diagnostic from the newest probe-bearing row of the activity
    feed (`services/connections._attach_status_detail`) — one indexed statement
    for a whole page, set as a plain instance attribute that cannot be written
    back. A column would save that one query and buy a write path that can go
    stale; the feed is already the record. Re-open this only if the extra
    statement shows up in a profile.

**Not a migration, and already done on this branch**: `SecretAccessAction.REVOKE`
(the column is a plain string) — the vault was writing `revoke` rows that
`?action=revoke` then refused to filter for.
