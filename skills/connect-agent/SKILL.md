---
name: connect-agent
description: Connect an existing AI agent to the Fulcrum Ops control plane using the SDK. Registers the agent, mints a bound ingest key, instruments the code with @trace and typed spans, wires the environment, and verifies a run actually lands before declaring success. Use when someone says "connect this agent", "instrument this agent", "add Fulcrum to this project", "onboard an agent", or points at an agent repo and wants it governed.
---

# Connect an existing agent

Turns an uninstrumented agent into a governed one. The whole job is five steps,
and the last one is the one people skip: **prove a run arrived.**

## Before you touch anything

Establish these four facts. Ask only for what you cannot determine yourself.

| Fact | How to get it |
|---|---|
| Control plane URL | Ask. Looks like `https://host/api/v1`. |
| An admin session or key | Ask. Needed to register and mint. |
| The agent's entry point | Read the code. The function that takes a request and returns an answer. |
| Where it will run | Ask. Decides how the key is delivered. |

Never invent the URL, and never proceed against a control plane you cannot
reach — check `GET /health` first and stop if it does not answer.

## Step 1 — Read the agent before changing it

Find, and write down:

- **The entry point.** One function per request. If there are several, ask which
  one is the run — do not instrument all of them.
- **Model calls.** `openai`, `anthropic`, `AzureOpenAI`, `litellm`, `requests`
  to a completions URL, or a framework (LangChain, Semantic Kernel).
- **Tool calls.** Anything the model decides to invoke, plus retrieval.
- **Safety checks** already present: PII scrubbing, moderation, validation.
- **Language.** Python gets the decorator; TypeScript gets callbacks.

If the code is a framework the SDK has an integration for (OpenAI, Anthropic,
LangChain), prefer the provider wrapper over hand-written spans — it captures
model, tokens and streaming correctly without you having to.

## Step 2 — Register the agent

```bash
curl -X POST "$BASE/agents" -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"name":"Support Bot","platform":"Custom Agent","environment":"Production",
       "agent_type":"Pro-code","risk":"Medium"}'
```

Then **activate it** — new agents start in Pending Review and an inactive agent
cannot ingest:

```bash
curl -X POST "$BASE/agents/<id>/activate" -b cookies.txt -d '{}'
```

Platform is one of `Azure AI Foundry`, `Copilot Studio`, `M365 Copilot`,
`Power Platform`, `Custom Agent`. Pick the one that is true — it is how the
agent's traffic is matched to a hosting connection later.

## Step 3 — Mint a key bound to that agent

```bash
curl -X POST "$BASE/workspaces/api-keys" -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"name":"support-bot ingest","scopes":["ingest","read"],"agent_id":"<id>"}'
```

**Bind it.** An unbound key can report as any agent; a bound key cannot be used
to impersonate the rest of the fleet if it leaks. The plaintext is returned
once — capture it in that response or it is gone.

Never write the key into source. It goes in an env file, a container secret, or
a secret manager, according to where the agent runs.

## Step 4 — Instrument

Add the dependency (`fulcrum-ops` for Python, `@fulcrum-ops/sdk` for Node), then:

```python
import fulcrum_ops
from fulcrum_ops import trace

fulcrum_ops.configure(agent="Support Bot", environment="Production",
                      on_error=lambda e, op: print(f"[telemetry] {op}: {e}"))

@trace                                    # the entry point opens the run
def answer(question: str) -> str:
    client = fulcrum_ops.get_client()

    with client.span("retrieve", type="tool") as s:
        docs = search(question)
        s.set_output({"documents": len(docs)})

    with client.span("generate", type="llm") as s:
        s.set_model(response.model, "openai")     # what actually served it
        s.set_usage(prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens)
        s.set_output(text)
    return text
```

Rules that matter more than they look:

- **Report the real model, not a deployment alias.** Use the provider
  response's `model`. Cost tables are keyed on it, and an alias resolves to
  zero cost silently.
- **Provider vocabulary matters too.** Azure-served OpenAI models want provider
  `azure`, not `azure-openai`.
- **Prefer measured tokens** from the provider response over `len(text)//4`.
- **Set `on_error` during onboarding.** It is how the first mistake announces
  itself instead of vanishing.
- **Flush before exit** in short-lived processes: `fulcrum_ops.flush()`.
- If the agent already runs safety checks, report them:
  `client.log_guardrail_event("pii-scan", action_taken="Warn", matched={"EMAIL": 2})`
  — `matched` is a **mapping**, and the action is `Warn`, never `Warned`.

Do not restructure the agent's logic. Instrumentation observes; it does not
refactor.

## Step 5 — Verify, then say it works

Run the agent once for real, then confirm the run arrived:

```bash
curl -s "$BASE/runs?page_size=3&sort=-occurred_at" -b cookies.txt
```

Check all four, and fix what is wrong before reporting success:

1. A row exists, attributed to **the agent you registered**.
2. Its **model** is populated and correct.
3. **Tokens** are non-zero and match the provider.
4. The **span tree** shows the real path — `GET /runs/<id>/trace`.

If nothing arrived, work the list in order: is the key set in the process; is
the agent Active; does `client.stats()` show `rejected` (usually
`agent_unprovisioned`, meaning a name or binding mismatch); is the base URL
reachable from where the agent runs.

**Never report success on an unverified integration.** "It should be reporting
now" is not the deliverable — a run visible in Live Runs is.

## What to hand back

- The agent id, the key's display hint (never the key itself), and where the
  key was placed.
- The diff you made to their code, described in a sentence.
- The run id you verified, and what it showed.
- Anything you deliberately did not instrument, and why.
