# Fulcrum Ops — make it fully functional and bulletproof

You are taking over a working but unfinished AI agent control plane. It is live,
it has real telemetry flowing, and roughly half its screens have never been
exercised with real data. Your job is to finish it: verify what exists, use it
as a real user would, find what breaks, and fix it.

Work from evidence. Do not trust this document over what you observe.

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
| Source | `C:\Users\FDUSI0013\fulcrum-ops\` — **not a git repo** |
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
apps/control-plane/.venv/Scripts/python.exe -m pytest          # 615 pass today
apps/control-plane/.venv/Scripts/python.exe -m ruff check src
apps/control-plane/scripts/check_contract.py                   # console<->API
scripts/check-branding.sh                                      # no vendor strings
```

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

### 4. Close the three Red risks

- **The code exists only on this laptop.** No git repo, no remote, no history.
  This is the cheapest catastrophic risk to close.
- **No CI.** Nothing gates a deploy but discipline.
- **Single host, no HA, untested restore** — and the Postgres backup is unreadable
  without `APP_ENCRYPTION_KEY`, which lives on the same host. Verify a restore
  actually works and get the key escrowed elsewhere.

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
