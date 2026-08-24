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
pip install fulcrum-ops
```

---

## 60-second quickstart

**1. Get an API key.** Keys are minted per workspace and shown exactly once —
the control plane stores only a digest and cannot show it again.

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

**2. Put the key in the environment.**

```bash
export FULCRUM_OPS_API_KEY=fo_live_…
export FULCRUM_OPS_BASE_URL=https://controlplane.example.com/api/v1
```

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
change what the program does. Pass `enabled=True` to make that case loud
instead, or check `client.stats()["enabled"]`.

`FulcrumOps` is also a context manager, which is the right shape for a script:

```python
with FulcrumOps(agent="nightly-job") as client:
    ...
# flushed and closed on the way out
```

### Environment variables

| Variable | Argument | Default |
| --- | --- | --- |
| `FULCRUM_OPS_API_KEY` | `api_key` | — (SDK disables itself) |
| `FULCRUM_OPS_BASE_URL` | `base_url` | `http://127.0.0.1:8080/api/v1` |
| `FULCRUM_OPS_WORKSPACE` | `workspace` | from the key |
| `FULCRUM_OPS_ENVIRONMENT` | `environment` | — |
| `FULCRUM_OPS_AGENT` | `agent` | — |
| `FULCRUM_OPS_SAMPLING_RATE` | `sampling_rate` | `1.0` |
| `FULCRUM_OPS_CAPTURE_INPUT` | `capture_input` | `true` |
| `FULCRUM_OPS_CAPTURE_OUTPUT` | `capture_output` | `true` |
| `FULCRUM_OPS_TIMEOUT_SECONDS` | `timeout_seconds` | `30` |
| `FULCRUM_OPS_DEBUG` | `debug` | `false` |
| `FULCRUM_OPS_DISABLED` | — | unset |

---

## Tracing

### `@trace`

Decorate a function and every call to it is reported. The first decorated call
in a context opens a **trace**; every decorated call nested inside it opens a
child **span**. Nothing is passed between them — `contextvars` carries the
parent link, which is thread-local for threads and task-local for asyncio.

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
ingest path would drop.

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

- `client.flush(timeout=10)` — send everything queued, now. Returns `False` on
  timeout rather than raising.
- `client.close()` — flush, stop the worker, release the pool. Idempotent.
- An `atexit` hook flushes every live client with a two-second bound, so a
  process on its way out never hangs on a control plane that is not answering.

Prefer an explicit `close()` in a short-lived process — a Lambda handler, a CLI,
a test. Exit hooks are a safety net, not a guarantee: `os._exit()` and a fatal
signal both skip them.

`client.stats()` reports what happened:

```python
{'submitted': 12, 'sent': 12, 'accepted': 11, 'rejected': 1, 'blocked': 0,
 'dropped_overflow': 0, 'dropped_failed': 0, 'batches': 2, 'retries': 1,
 'errors': 0, 'pending': 0, 'last_error': None, 'enabled': True, ...}
```

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
is exhausted, and the accumulated text is what the span records.

For LangChain, pass the callback handler wherever callbacks are accepted:

```python
from fulcrum_ops.integrations import FulcrumOpsCallbackHandler

handler = FulcrumOpsCallbackHandler(agent="research-agent")
chain.invoke(question, config={"callbacks": [handler]})
```

The outermost run becomes the trace; chains, models, tools and retrievers
beneath it become spans of the matching type. The handler keeps its own
`run_id → span` map rather than using the ambient context, because LangChain
dispatches callbacks from its own executor and the context that started a call
is long gone by the time its `on_llm_end` fires.

---

## Configuration from the control plane

On start-up the SDK reads `GET /ingest/config` on a background thread and adopts
what it says: sampling, batching, the flush interval, the queue ceiling and the
redaction rules. The fetch never blocks start-up, and it is revalidated with an
ETag so a fleet restart costs one conditional request per process rather than
one full read each.

Where the document and your arguments disagree, the **stricter** value wins. The
document expresses a limit the deployment enforces, not a preference you
expressed: a workspace capped at 25% sampling is not raised to 100% by a
constructor argument, and `capture_input=False` from the server cannot be
switched back on locally.

```python
document = client.config()   # raises if it cannot be read — this one is a lookup
print(document["workspace"], document["sampling_rate"])
```

### Redaction

Rules from that document are applied **locally, before content leaves the
process**. A rule that matches a credit card number means the number is never
sent, not that it is scrubbed on arrival. Add your own on top:

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
