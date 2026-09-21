# fulcrum-ops

Report agent traces, spans, feedback scores and governance events to the
Fulcrum Ops control plane.

- **One dependency.** `httpx`, and nothing else.
- **Never breaks your agent.** A telemetry failure is counted, logged and handed
  to your `on_error` hook. It never becomes your user's error.
- **Covers the four shapes of Python.** `@trace` works on functions,
  coroutines, generators and async generators, each with its own wrapper so the
  decorated function keeps its own contract.
- Python 3.9+.

```bash
pip install --extra-index-url https://controlplane.fdprod.net/pypi/simple fulcrum-ops
```

The package is served by the control plane it reports to, not by the public
index: it is that deployment's client, versioned with the API it speaks to, and
any agent that can report telemetry can already reach the host. `--extra-index-url`
rather than `--index-url`, so the one dependency (`httpx`) still comes from
wherever you normally get packages. To pin it for a project, put the same line in
`requirements.txt` as `--extra-index-url https://controlplane.fdprod.net/pypi/simple`
followed by `fulcrum-ops==1.0.1`.

Replace the host if your control plane lives somewhere else; every deployment
serves its own matching build at `/pypi/simple`.

---

## 60-second quickstart

**1. Get an API key.** In the console, open **Workspace Settings → API Keys**,
create a key and bind it to the agent that will report with it. Keys are minted
per workspace and shown exactly once — the control plane stores only a digest
and cannot show it again, so copy it before closing the dialog.

An operator with a shell on the control plane's host can mint one there instead,
which is how the first key of a brand-new deployment is made:

```bash
fulcrum-ops-api issue-key \
  --workspace acme \
  --name "checkout-agent (production)" \
  --scopes ingest,read
```

`ingest` lets the key report telemetry; add `read` for prompt and dataset
lookups. A key whose scopes include `admin` may also register an agent the
workspace has not seen before, which is what lets a new service start reporting
without anyone visiting the console first. Without it, report under an agent
that already exists or the rows come back rejected.

**2. Put the key *and the address* in the environment.** Both are required.

```bash
export FULCRUM_OPS_API_KEY=fo_live_…
export FULCRUM_OPS_BASE_URL=https://controlplane.fdprod.net/api/v1
```

`FULCRUM_OPS_BASE_URL` has no useful default. Left out, the SDK falls back to
`http://127.0.0.1:8080/api/v1` — a control plane running on your own machine —
and says so in one `WARNING` on the `fulcrum_ops` logger when the client is
built, because a key with nowhere to go is a mistake: nothing reaches your
console, and the key and your prompts are posted to whatever owns that port.

**3. Trace something.**

```python
import fulcrum_ops
from fulcrum_ops import trace

fulcrum_ops.configure(agent="checkout-agent", environment="Production")

@trace
def answer(question: str) -> str:
    client = fulcrum_ops.get_client()

    with client.span("retrieval", type="tool") as span:
        docs = search(question)
        span.set_output({"chunks": len(docs)})

    with client.span("answer", type="llm") as span:
        span.set_model("gpt-4o-mini", "openai")
        reply = model.complete(docs)
        span.set_usage(prompt_tokens=120, completion_tokens=40)
        return reply

print(answer("where is my order?"))
fulcrum_ops.flush()
```

That is the whole loop. The run appears in the console with its two spans nested
inside it, the model and token counts on the LLM span, and the duration measured
across the real work rather than up to the first `return`.

---

## The client

`FulcrumOps` is the object that holds the connection. Build one explicitly, or
call `configure()` once and let the decorators find it.

```python
from fulcrum_ops import FulcrumOps

client = FulcrumOps(
    api_key="fo_live_…",                 # or FULCRUM_OPS_API_KEY
    base_url="https://…/api/v1",         # or FULCRUM_OPS_BASE_URL
    environment="Production",
    agent="checkout-agent",
)
```

Every request sends `Authorization: Bearer <key>`. The key carries its own
workspace, so `workspace` only needs setting for a key that serves more than one.

**With no key found, the SDK disables itself.** `@trace` still runs your
function and returns its value, nothing is queued, and nothing is sent. That is
deliberate: a missing environment variable in a developer's shell must not
change what the program does. It is said once per process, at `WARNING`, so a
mistyped variable name does not pass for a quiet console; set
`FULCRUM_OPS_DISABLED=1` (or pass `enabled=False`) to switch reporting off on
purpose and silence it. `client.stats()["enabled"]` tells you which you got.

`environment=` labels the runs this process reports, and it is where an agent
the control plane has never seen is first filed. For an agent that is already
registered, the environment set in the console is the one policies, quotas and
guardrail scope go by.

Behind a TLS-inspecting proxy, or anywhere the default trust store is not the
right one, hand the SDK the HTTP client to use. It must be a **synchronous**
`httpx.Client`; the SDK never closes a client it did not build:

```python
import ssl, httpx, truststore          # truststore (Python 3.10+): the OS trust store

client = FulcrumOps(
    http_client=httpx.Client(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        follow_redirects=True,         # httpx does not follow redirects unless told to
        timeout=30.0,
    ),
)
```

Without `follow_redirects=True` an `http://` address behind a proxy that
upgrades to `https://` answers every request with a 308. The SDK follows a
same-host redirect itself and says so once; it never follows one to another
host, because the request carries your API key. Set `base_url` to the final
address and neither happens.

`FulcrumOps` is also a context manager, which is the right shape for a script:

```python
with FulcrumOps(agent="nightly-job") as client:
    ...
# flushed and closed on the way out
```

### Environment variables

| Variable | Argument | Default |
| --- | --- | --- |
| `FULCRUM_OPS_API_KEY` | `api_key` | — (SDK disables itself, and says so) |
| `FULCRUM_OPS_BASE_URL` | `base_url` | **required** — falls back to `http://127.0.0.1:8080/api/v1` with a warning |
| `FULCRUM_OPS_WORKSPACE` | `workspace` | from the key |
| `FULCRUM_OPS_ENVIRONMENT` | `environment` | — |
| `FULCRUM_OPS_AGENT` | `agent` | — |
| `FULCRUM_OPS_SAMPLING_RATE` | `sampling_rate` | `1.0` |
| `FULCRUM_OPS_CAPTURE_INPUT` | `capture_input` | `true` |
| `FULCRUM_OPS_CAPTURE_OUTPUT` | `capture_output` | `true` |
| `FULCRUM_OPS_TIMEOUT_SECONDS` | `timeout_seconds` | `30` |
| `FULCRUM_OPS_TIMEOUT_MS` | `timeout_seconds`, in milliseconds | — |
| `FULCRUM_OPS_DEBUG` | `debug` | `false` |
| `FULCRUM_OPS_DISABLED` | — | unset |

The timeout is read under the TypeScript SDK's name and unit
(`FULCRUM_OPS_TIMEOUT_MS`) as well as this SDK's own, which wins when both are
set — so one variable covers a fleet that runs agents in both languages.

---

## Tracing

### `@trace`

Decorate a function and every call to it is reported. The first decorated call
in a context opens a **trace**; every decorated call nested inside it opens a
child **span**. Nothing is passed between them — `contextvars` carries the
parent link, which is thread-local for threads and task-local for asyncio (see
[Threads and executors](#threads-and-executors) for the one hop it does not
cross by itself).

```python
from fulcrum_ops import trace

@trace
def plan(goal: str) -> Plan: ...

@trace(name="retrieve", type="tool")
def retrieve(question: str) -> list[str]: ...

@trace(type="llm", capture_input=False)
async def generate(prompt: str) -> str: ...
```

Arguments are bound against the signature, so the console shows
`{"question": "…", "top_k": 5}` rather than `{"args": [...]}`. `self` and `cls`
are dropped.

All four callable shapes are covered, each with a wrapper of its own kind —
wrapping a generator in a coroutine would change your function's contract, and
the SDK is not allowed to change how your code behaves:

| Shape | Span opens | Span closes |
| --- | --- | --- |
| function | on call | on return or raise |
| coroutine | on call | when the coroutine finishes |
| generator | on the first `next()` | when iteration ends |
| async generator | on the first `__anext__()` | when iteration ends |

For generators that means the recorded duration is how long the stream took,
not the microsecond it took to build the generator object. Yielded values are
recorded up to a cap and the rest are counted, because a stream of ten thousand
tokens is not telemetry.

### `type=` on the outermost call

A trace cannot carry a model, tokens or a cost; only a span can. So when the
*outermost* decorated call names a `type` other than `general`, the decorator
opens the run **and** a root span of that type inside it, and the whole
integration can be one line:

```python
import fulcrum_ops
from fulcrum_ops import trace

@trace(name="underwriting_insight", type="llm")
def get_insight(self, email):
    reply = call_the_model(email)
    fulcrum_ops.current_span().set_model(reply.model, "azure").set_usage(
        prompt_tokens=reply.usage.input_tokens,
        completion_tokens=reply.usage.output_tokens,
    )
    return reply.output_text
```

`type="llm"` only *labels* the step. The model, the token counts and the cost
come from one of two places — report them yourself as above, or let a
[provider wrapper](#provider-wrappers) read them off the response. Pick one: do
both and the run's totals count every token twice. With a wrapper in place, a
plain `@trace` on the entry point is all it needs.

### `fulcrum_ops.current_span()` and `current_trace()`

`fulcrum_ops.current_span()` is the innermost open span, found through the
execution context — no client, no argument passing. It **never returns `None`**:
with reporting off, a run that was not sampled in, a plain `@trace` root (a run
with no step), or a call from outside anything traced, it hands back a
`NoopSpan` that accepts `set_model`, `set_usage`, `set_output`, `log`, `score`
and the rest and records nothing, so the line above cannot raise inside your
agent. It is falsy, so `if fulcrum_ops.current_span():` still tells you whether
anything is listening. `fulcrum_ops.current_trace()` is the run, or `None`.

### Conversations: `thread_id`

Runs that share a `thread_id` are one session. The Sessions, Conversation State
and Memory views are built from threads, so an agent that never names one shows
runs and no sessions. A decorator is evaluated once, which makes a string there
the same thread for every call — pass a callable, which is given the function's
own arguments, or name the thread from inside once the code knows it:

```python
@trace(name="underwriting_insight", type="llm",
       thread_id=lambda self, email, **_: email.conversation_id)
def get_insight(self, email): ...

@trace
def handle(payload: dict) -> str:
    fulcrum_ops.set_thread_id(parse(payload).conversation_id)
    ...
```

The decorated function keeps its own signature either way. A `thread_id`
callable that raises costs that run its thread, never the call. An inner
decorated step that can name the thread gives it to a run that has none.
`Trace.set_thread_id(...)` is the same thing on a run you hold.

### Runs the console started: `trace_id`

**Run** on an agent's page — `POST /agents/{id}/run` — opens the run in the
console and answers with a `run_id` and a `session_id`. Nothing is dispatched:
whoever asked hands both to the runtime, and the run stays `Running` until the
runtime reports under that id. Give it to the run that does the work:

```python
@trace(name="handle", trace_id=lambda job: job.run_id, thread_id=lambda job: job.session_id)
def handle(job): ...

with client.trace("handle", id=job.run_id, thread_id=job.session_id, sampled=True):
    ...
```

Only the call that opens the run reads it; a decorated call nested inside one is
a step of that run. The id has to be a version 7 UUID — what the console issues
and `fulcrum_ops.new_id()` mints — because the telemetry store takes no other
kind and refuses the whole request over one that is not. Anything else is
replaced, with a warning, and `.id` is the id actually in use. Sampling applies
to these runs like any other; `client.trace(..., sampled=True)` keeps a run
somebody is waiting on out of the draw.

### `client.span(...)`

```python
with client.span("retrieval", type="tool") as span:
    span.log("querying the index", filters=filters)
    chunks = index.search(query)
    span.set_output({"chunks": len(chunks)})
    span.score("recall", 0.82, reason="8 of 10 gold chunks returned")
```

`type` is one of `general`, `llm`, `tool`, `guardrail`. On an LLM span,
`set_model(model, provider)` and `set_usage(...)` are what drive the per-model
cost and token breakdowns.

`span.log(...)` is not a logging framework. It exists so that the one value
which explains a run — the retrieved chunk count, the tool's raw arguments, the
branch the agent took — sits next to the span it belongs to, instead of in a log
file nobody correlates.

Called with no trace open, `client.span(...)` opens one around itself, so a
single instrumented function is still a complete run rather than an orphan the
ingest path would drop. That run takes the span's input, output and failure as
its own, so it does not list as a run that took nothing in and gave nothing back.
Pass `thread_id=` to file that run under a conversation.

### Threads and executors

A new asyncio task starts with a copy of its creator's context, and so do
`asyncio.to_thread` and the anyio/Starlette thread pool: spans opened there nest
where you expect. A plain worker thread starts with an empty one, so work handed
to `ThreadPoolExecutor.submit`/`map` or `loop.run_in_executor` cannot see the run
it came from, and each step it opens becomes a run of its own. Carry it across:

```python
import fulcrum_ops

with fulcrum_ops.TracedThreadPoolExecutor(max_workers=4) as pool:   # drop-in
    pages = list(pool.map(extract, attachments))

with ThreadPoolExecutor(4) as pool:                                 # or per callable
    pages = list(pool.map(fulcrum_ops.propagate(extract), attachments))

await loop.run_in_executor(None, fulcrum_ops.propagate(poll_once), mailbox)
```

`propagate(fn)` binds `fn` to the run and span that are open where `propagate`
is called, and takes them down again after each call, because pool threads are
reused. Only this SDK's own context travels.

### Long-running work

`client.trace(...)` is a context manager, but the objects underneath are plain:

```python
run = client.trace("batch-job", thread_id=conversation_id)
run.__enter__()
...
run.end(output=summary)
```

With `stream_spans=True` each span is posted as it closes rather than waiting
for its trace, so a job that runs for an hour shows progress while it runs.

---

## Feedback scores

Scores usually arrive later than the run they describe — a thumbs-down two
minutes after the answer, a judge's verdict from an offline pass — so they are
posted on their own.

```python
client.score(trace_id, "helpfulness", 0.9, reason="resolved on first reply")
client.score(span_id, "grounded", 1.0, target="span")
client.score(conversation_id, "csat", 4.0, target="thread")
```

Inside a trace, a score attached to the open unit travels with it rather than
costing a second request:

```python
with client.trace("run") as run:
    run.score("answered", 1.0)
```

Governance events map onto `POST /ingest/events` and its three kinds:

```python
client.log_feedback(trace_id=trace_id, rating=5, sentiment="positive", body="perfect")
client.log_guardrail_event("pii-filter", action_taken="Masked", score=0.98, sample=text)
client.log_policy_violation("no-medical-advice", severity="high")
```

`sample` goes through the same redaction rules as any other captured content —
it is a slice of the text that tripped the rule, which makes it the single field
most likely to carry exactly what redaction exists to remove.

When the agent itself can tell an answer went wrong — a retrieval that returned
nothing, a tool that answered nonsense — it can say so without waiting for a
person to complain:

```python
fulcrum_ops.report_issue("Retrieval returned nothing", severity="High",
                         detail="0 chunks for a question the index should cover.")
```

It travels as negative feedback (`feedback.submitted`, source *Agent Response
Rating*) and lands in the Feedback inbox with the title leading the body; the
title and severity also ride, structured, in the event's `detail`. The ingest
contract has exactly three event kinds, and a row of any other kind is refused.

---

## Prompts

```python
prompt = client.get_prompt("support-system")
text = prompt.format(customer_name="Ada", tier="gold")
```

Unpinned lookups resolve to the prompt's current head and are cached for a TTL,
because the reason to fetch a prompt at all is that someone may change it
without a redeploy. Pinning to a commit caches forever, because a commit cannot
change:

```python
pinned = client.get_prompt("support-system", commit="a1b2c3d")
```

A placeholder with no value is left as `{{visible}}` rather than replaced with an
empty string. A visible placeholder in a model's context is a bug someone
notices; a silently missing one is a bug nobody does.

Unlike telemetry, this call **raises**. A missing system prompt is not a
degraded agent, it is a broken one.

---

## Offline evaluation

A dataset is a fixed set of cases; an experiment runs your code over all of them
and scores the results. The scores land on real traces, so a regression shows up
in the same console screens as production traffic.

```python
client.datasets.create("checkout-questions", description="Golden set, Q3")
client.datasets.add_items("checkout-questions", [
    {"input": "where is my order?", "expected_output": "tracking link"},
    ("can I return this?", "returns policy"),
])

def task(question: str) -> str:
    return answer(question)

def contains_expected(output, expected):
    return 1.0 if expected and expected in str(output) else 0.0

result = client.evaluate("checkout-questions", task, scorers=[contains_expected])
print(result.summary())
# {'name': 'experiment:checkout-questions', 'cases': 2, 'failures': 0,
#  'scores': {'contains_expected': 0.5}}
```

A case that raises is recorded as a failed trace and the run continues. An
experiment that stops at the first bad case tells you far less than one that
finishes and shows you all four.

`client.experiments.run(dataset, judge_model=...)` is the other half: it asks the
control plane to evaluate the dataset server-side with a judge model, and hands
back an evaluation id.

---

## Batching, flushing and shutdown

Handing work in never blocks. A finished trace is appended to an in-memory
deque and the call returns; the network happens on a worker thread.

Batches are cut on three triggers — item count, byte budget, and the flush
interval, whichever comes first. Failed sends are retried with exponential
backoff and full jitter, honouring `Retry-After`. A `413` halves the batch and
retries rather than dropping it, so one oversized trace does not take its
neighbours down with it.

Those in-request retries cover a blip of a few seconds. **An outage longer than
that is waited out, not thrown away**: a batch that fails for a reason that can
clear — no connection, a timeout, a 5xx, a 429 — goes back to the front of the
queue, and the worker backs off (one flush interval, doubling to a minute, or
whatever `Retry-After` said) before trying again. A deploy of the control plane
costs you nothing. What *is* dropped: a batch the control plane refused for a
reason retrying cannot change (a revoked key, a malformed body), a row that has
been failing for ten minutes, whatever is still queued when the process closes
during an outage, and anything past the queue ceiling.

The queue is **bounded**. Past `max_queue_size` the *oldest* rows are dropped,
because during an outage the freshest telemetry is the telemetry someone is
waiting to look at, and an unbounded queue turns a control-plane outage into
your own out-of-memory kill.

```python
client = FulcrumOps(
    batch_max_items=100,
    batch_max_bytes=4 * 1024 * 1024,
    flush_interval_seconds=5.0,
    max_queue_size=10_000,
    retry_max_attempts=3,
    retry_backoff_seconds=0.5,
    on_error=lambda error, operation: log.warning("telemetry %s: %s", operation, error),
)
```

- `client.flush(timeout=10)` — send everything queued, now. `True` means it was
  all handed to the control plane. `False` means it was not — the wait timed
  out, or a batch could not be delivered and was kept for later or dropped — and
  during an outage it comes back as soon as the attempt has failed, not after the
  whole timeout. It never raises.
- `client.close()` — flush, stop the worker, release the pool. Idempotent.
- An `atexit` hook flushes every live client with a two-second bound, so a
  process on its way out never hangs on a control plane that is not answering.

Prefer an explicit `close()` in a short-lived process — a Lambda handler, a CLI,
a test. Exit hooks are a safety net, not a guarantee: `os._exit()` and a fatal
signal both skip them.

In a **long-lived server, do not `flush()` per request.** The worker sends every
`flush_interval_seconds` by itself, and a flush is a wait on the network that
you would be putting on your user's request. Call `fulcrum_ops.shutdown()` once,
on the way out:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    fulcrum_ops.configure(agent="uw-bridge", environment="Production")
    yield
    fulcrum_ops.shutdown()
```

**Pre-forking servers** (gunicorn `--preload`, Celery prefork, `multiprocessing`
with `fork`) are handled: a forked child gets a fresh worker thread and
connection pool on its first report, and does not re-send what the parent had
queued. An `http_client` you injected is yours to make fork-safe.

`client.stats()` reports what happened:

```python
{'submitted': 12, 'sent': 12, 'accepted': 11, 'rejected': 1, 'blocked': 0,
 'dropped_overflow': 0, 'dropped_failed': 0, 'requeued': 0, 'batches': 2,
 'retries': 1, 'errors': 0, 'pending': 0, 'last_error': None, 'enabled': True, ...}
```

`requeued` rising with `pending` means the control plane is unreachable and rows
are waiting for it; `dropped_failed` and `dropped_overflow` are what was lost.

A batch is answered with HTTP 200 even when individual rows are refused, so
`accepted`, `rejected` and `blocked` are where that shows up — not the status
code. `rejected` rising is worth an alert: the reason is in the log line, and it
is usually `agent_unprovisioned`, which is fixable in one click.

---

## Provider wrappers

Tracing that does not change your call sites. Every wrapper works structurally
on whatever object it is handed, so none of the provider packages is imported by
this SDK and none of them is a dependency of it.

```python
from openai import OpenAI
from fulcrum_ops.integrations import track_openai

openai = track_openai(OpenAI())
openai.chat.completions.create(model="gpt-4o-mini", messages=messages)
```

```python
from anthropic import Anthropic
from fulcrum_ops.integrations import track_anthropic

claude = track_anthropic(Anthropic())
claude.messages.create(model="claude-sonnet-4-5", messages=messages, max_tokens=512)
```

Both return a proxy: attribute access falls through to the real client and only
the completion methods are wrapped, on that instance alone. Monkey-patching the
provider's classes would work too, but it is a global mutation of somebody
else's library — it breaks a second, untraced client in the same process and
survives past the point where anyone remembers doing it.

Streaming is handled rather than skipped. A streamed call returns before the
first token, so closing the span there would report a 12-second generation as
taking 200 microseconds. The stream is wrapped instead, the span closes when it
is exhausted (or when you stop reading it), and the accumulated text is what the
span records. The model and the token counts are read from the events that carry
them — the Responses API's `response.completed`, Anthropic's `message_start` and
`message_delta`, the final Chat Completions chunk. That last one only exists if
you ask for it: pass `stream_options={"include_usage": True}`, or a streamed
Chat Completions call reports its text and no tokens.

What is traced, on sync and async clients alike:

| | |
| --- | --- |
| OpenAI | `chat.completions.create` / `.parse` / `.stream`, `beta.chat.completions.parse` / `.stream`, `responses.create` / `.parse` / `.stream`, `completions.create`, `embeddings.create`, `moderations.create` |
| Anthropic | `messages.create` / `.stream`, `beta.messages.create` / `.stream`, `completions.create` |
| Both | the same methods through `client.with_options(...)`, `.copy(...)` and `.with_raw_response`; `.with_streaming_response` records the call and its duration, not the usage, because reading the body would take the stream away from you |

A turn that only calls tools has no text; its span records the tool calls the
model asked for instead of recording nothing.

A wrapped call made with no trace open is a complete run of its own, with the
call's input, output and failure on the run. The wrapper never touches the
ambient context, so a call awaited under `asyncio.gather`, in a task, or inside
`asyncio.to_thread` cannot leave a finished run behind for later spans to be
lost on.

The wrapper looks up the default client on each call, so it can be built at
import time, before `fulcrum_ops.configure()` runs. Pass `fulcrum=` to pin it to
a specific client.

### The Responses API, Azure OpenAI and Azure AI Foundry

`track_openai` reads a Responses API result the same way it reads a chat
completion: `response.model`, `usage.input_tokens` / `usage.output_tokens`
(reported as `prompt_tokens` / `completion_tokens`) and `output_text`. An
`AzureOpenAI` client, and the client an Azure AI Foundry project hands out, are
wrapped identically:

```python
import fulcrum_ops
from fulcrum_ops import trace
from fulcrum_ops.integrations import track_openai

fulcrum_ops.configure(agent="uw-bridge", environment="Production")

class AgentClient:
    def __init__(self, project_client):
        self._openai = track_openai(project_client.get_openai_client(), model="gpt-5")

    @trace(name="underwriting_insight")          # plain: the wrapper supplies the llm step
    def get_insight(self, email):
        fulcrum_ops.set_thread_id(email.conversation_id)
        response = self._openai.responses.create(
            input=[{"role": "user", "content": email.body}],
            extra_body={"agent_reference": {"name": "uw-agent", "type": "agent_reference"}},
        )
        return response.output_text
```

- **`provider`** is what cost is priced under, together with the model. It is
  detected — `azure` for an `AzureOpenAI` client or an `*.openai.azure.com`,
  `*.cognitiveservices.azure.com` or `*.services.ai.azure.com` endpoint, `openai`
  otherwise; `bedrock` / `google_vertexai` for those Anthropic clients — and
  `track_openai(client, provider="...")` overrides it.
- **`model=`** is the fallback when neither the call nor the response names one.
  A Foundry agent call is addressed by `agent_reference`, not by `model`, so the
  request has nothing to offer; when the response names a model, that wins.
- The call may run anywhere — `await asyncio.to_thread(agent.get_insight, email)`
  keeps the run, because `to_thread` copies the context.

For LangChain, pass the callback handler wherever callbacks are accepted:

```python
from fulcrum_ops.integrations import FulcrumOpsCallbackHandler

handler = FulcrumOpsCallbackHandler(agent="research-agent")
chain.invoke(question, config={"callbacks": [handler]})
```

The provider is taken from LangChain's own `ls_provider` metadata (`openai`,
`azure`, `anthropic`, …), not from the class family name, so LangChain runs are
priced under the same provider names as everything else.

The outermost run becomes the trace; chains, models, tools and retrievers
beneath it become spans of the matching type. The handler keeps its own
`run_id → span` map rather than using the ambient context, because LangChain
dispatches callbacks from its own executor and the context that started a call
is long gone by the time its `on_llm_end` fires.

---

## Configuration from the control plane

On start-up the SDK reads `GET /ingest/config` on a background thread and adopts
what it says: sampling, batching, the flush interval, the queue ceiling and the
redaction rules. The fetch never blocks start-up.

It is then **kept current**. The document says how long it is good for
(`refresh_after_seconds`, five minutes as served) and the worker thread re-reads
it when that is up, revalidated with an ETag so a refresh that finds nothing new
is one `304`. A read that fails — the process started while the control plane was
restarting — is tried again after 5 s, 30 s, a minute and so on, and reported to
`on_error` once per outage. So a guardrail switched to *Mask*, or a sampling rate
lowered in the console, reaches a running agent within minutes, without a
redeploy. `bootstrap=False` turns all of it off.

Where the document and your arguments disagree, the **stricter** value wins. The
document expresses a limit the deployment enforces, not a preference you
expressed: a workspace capped at 25% sampling is not raised to 100% by a
constructor argument, and `capture_input=False` from the server cannot be
switched back on locally. Each refresh narrows from what *you* passed, so a limit
the console later relaxes is relaxed here too — up to your own value, never past
it.

```python
document = client.config()   # raises if it cannot be read — this one is a lookup
print(document["workspace"], document["sampling_rate"])
```

### Redaction

Rules from that document are applied **locally, before content leaves the
process**. A rule that matches a credit card number means the number is never
sent, not that it is scrubbed on arrival. That holds for a run that finished
before the rules arrived, too: a queued row remembers which rules it was built
under and is put through the current ones just before it is sent, and the first
send of a process waits (on the worker thread, for at most five seconds) for the
start-up read. If the control plane cannot be reached at all there are no server
rules to apply, and your own still are. Add your own on top:

```python
from fulcrum_ops import RedactionRule

client = FulcrumOps(redaction=[
    RedactionRule(name="employee ids", pattern=r"EMP-\d{6}", replacement="[redacted]"),
    RedactionRule(name="contact details", entity_types=["email", "phone"]),
])
```

Named entity types this SDK matches without a server-supplied pattern:
`api_key`, `aws_access_key`, `credit_card`, `email`, `iban`, `ip`, `ipv4`,
`ipv6`, `jwt`, `phone`, `ssn`, `url`. When the server names one this version
does not know, it says so once in the log and lets the server mask it instead of
silently sending it unprotected.

To keep payloads off the wire entirely, turn capture off — the runs, timings,
token counts and scores still arrive, without the content:

```python
client = FulcrumOps(capture_input=False, capture_output=False)
```

---

## Errors

The reporting path never raises. Everything it absorbs goes to `on_error`, typed,
with the operation that produced it:

```python
from fulcrum_ops import QuotaExceededError, RateLimitError

def on_error(error, operation):
    if isinstance(error, QuotaExceededError):
        page_someone(error)
    elif not isinstance(error, RateLimitError):
        log.warning("telemetry %s: %s", operation, error)

client = FulcrumOps(on_error=on_error)
```

The calls whose whole purpose is to return a value do raise: `config()`,
`get_prompt()` and the dataset helpers. There a failure *is* the answer, and
swallowing it would hand a model an empty system prompt.

`flush()` and `close()` never raise either, even when the control plane is
unreachable. They are what ends up in a `finally` block and a shutdown hook, and
a telemetry flush has no business turning a request that worked into a request
that failed.

```
FulcrumOpsError
├── ConfigurationError
├── ApiError
│   ├── AuthenticationError        401
│   ├── PermissionDeniedError      403
│   ├── QuotaExceededError         402   (alias: EntitlementError)
│   ├── ValidationError            400 / 422
│   ├── NotFoundError              404
│   ├── PayloadTooLargeError       413
│   ├── RateLimitError             429
│   └── ServerError                5xx
│       └── TelemetryUnavailableError
└── TransportError
    ├── NetworkError
    └── TimeoutError
```

Every error carries `code`, `status`, `request_id`, `details`, `retryable` and
`retry_after_seconds`. `request_id` is the one to quote in a support ticket: it
is echoed by the control plane on every deliberate failure.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The suite runs the SDK against a real HTTP stub on a real socket rather than a
mocked transport, because most of what is worth testing here *is* the HTTP
behaviour: status handling, the retry loop, `Retry-After`, ETag revalidation,
the per-item result rows. A stubbed transport would let every one of those pass
while broken.
