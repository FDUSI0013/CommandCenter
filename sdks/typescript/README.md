# @fulcrum-ops/sdk

Report agent traces, spans, feedback scores and governance events to the
Fulcrum Ops control plane, from Node or the browser.

- **Zero runtime dependencies.** Global `fetch`, nothing else.
- **ESM and CJS**, with full type declarations.
- **Never throws into your code.** A telemetry failure is reported to your
  `onError` handler; it never becomes your user's error.
- Node >= 18, and any browser with `fetch`.

```bash
npm install @fulcrum-ops/sdk
```

---

## 60-second quickstart

**1. Get an API key.** Keys are minted per workspace and shown exactly once —
the control plane stores only a SHA-256 digest and cannot show it again.

```bash
fulcrum-ops-api issue-key \
  --workspace acme \
  --name "checkout-agent (production)" \
  --scopes ingest,read
```

`ingest` lets the key report telemetry; add `read` for prompt fetching. A key
whose scopes include `admin` may also register an agent the workspace has not
seen before, which is what lets a new service start reporting without anyone
visiting the console first. Without it, report under an agent that already
exists or the rows come back rejected.

**2. Put the key in the environment.**

```bash
export FULCRUM_OPS_API_KEY=fo_live_…
export FULCRUM_OPS_BASE_URL=https://controlplane.example.com/api/v1
```

**3. Trace something.**

```ts
import { FulcrumOps } from '@fulcrum-ops/sdk';

const fulcrum = new FulcrumOps({ agent: 'checkout-agent' });

const answer = await fulcrum.trace('support-question', async () => {
  const docs = await fulcrum.span({ name: 'retrieve', type: 'tool' }, () =>
    search(question),
  );

  return fulcrum.span({ name: 'answer', type: 'llm' }, (span) => {
    span.setModel('gpt-4o-mini', 'openai');
    return model.complete(docs);
  });
});

await fulcrum.close(); // flush before the process exits
```

That is the whole loop: the run appears in the console with its two spans
nested inside it, the model and token counts on the LLM span, and the duration
measured across the `await` rather than up to the first `return`.

---

## The API key

`apiKey` is read from the constructor first, then `FULCRUM_OPS_API_KEY`.

```ts
const fulcrum = new FulcrumOps({ apiKey: process.env.MY_OWN_VAR });
```

Every request sends `Authorization: Bearer <key>`. The key carries its own
workspace, so `workspace` only needs setting for a key that serves more than
one.

**With no key found, the SDK disables itself.** `trace()` still runs your
function and returns its value, nothing is queued, and nothing is sent. This is
deliberate: a missing environment variable in a developer's shell should not
change what the program does. Pass `enabled: true` to make that case loud
instead, or check `fulcrum.getStats()`.

**Never ship a key to a browser.** A key in front-end code is readable by
anyone who opens the network tab. In the browser, point `baseUrl` at your own
backend and forward from there.

### Environment variables

| Variable | Option | Default |
| --- | --- | --- |
| `FULCRUM_OPS_API_KEY` | `apiKey` | — (SDK disables itself) |
| `FULCRUM_OPS_BASE_URL` | `baseUrl` | `http://127.0.0.1:8080/api/v1` |
| `FULCRUM_OPS_WORKSPACE` | `workspace` | from the key |
| `FULCRUM_OPS_ENVIRONMENT` | `environment` | — |
| `FULCRUM_OPS_AGENT` | `agent` | — |
| `FULCRUM_OPS_DISABLED` | `enabled: false` | unset (reporting is on when there is a key) |
| `FULCRUM_OPS_TIMEOUT_MS` | `timeoutMs` | `30000` |
| `FULCRUM_OPS_TIMEOUT_SECONDS` | `timeoutMs`, in seconds | — |
| `FULCRUM_OPS_SAMPLING_RATE` | `samplingRate` | `1` |
| `FULCRUM_OPS_CAPTURE_INPUT` | `captureInput` | `true` |
| `FULCRUM_OPS_CAPTURE_OUTPUT` | `captureOutput` | `true` |
| `FULCRUM_OPS_DEBUG` | `debug` | `false` |

An option passed in code always wins over its variable. The names are shared
with the Python SDK, so one block in a compose file or a Kubernetes manifest
configures a mixed fleet: `FULCRUM_OPS_DISABLED=1` is the kill switch for both
(CI, or the middle of an incident), and the timeout is read under Python's name
and unit (`FULCRUM_OPS_TIMEOUT_SECONDS`) as well as this SDK's own, which wins
when both are set. Booleans accept `1/0`, `true/false`, `yes/no`, `on/off`.

---

## Tracing

### `trace()` and `span()`

Both take a name (or an options object) and a function, run it, and return
exactly what it returned. An async body keeps the unit open until its promise
settles. A throw is recorded on the unit and re-thrown untouched.

```ts
const total = fulcrum.trace('nightly-reconcile', () => 41 + 1); // number
const rows = await fulcrum.trace('sync', async () => fetchRows()); // Promise
```

Spans nest under whatever is in scope, with no parameter threading. On Node
that uses `AsyncLocalStorage`, which stays correct with any number of traces in
flight at once. Browsers have no such primitive, so the fallback is a stack:
right for sequential code, wrong for two overlapping `await`s. When that
matters, name the parent explicitly — the escape hatch works everywhere:

```ts
const parent = fulcrum.startSpan({ name: 'fan-out' });
const child = fulcrum.startSpan({ name: 'worker', parent });
```

Check which mechanism is in force with `fulcrum.contextBackend`.

### `traced()`

Wrap a function so every call to it is traced. The wrapper keeps the original's
name and arity, so it drops in where the original was. A `traced` function
called inside a trace becomes a span of it, which is what makes decorating a
call graph produce one tree instead of many roots.

```ts
import { traced } from '@fulcrum-ops/sdk';

const plan = traced(async function plan(goal: string) { … });
const act = traced(async function act(step: Step) { … });
```

A trace has no type, model or token counters — only a span does. So give
`traced()` any of `type`, `model`, `provider`, `usage` or `cost` and a call made
at the root opens the run *and* the typed span that is its whole body, rather
than a run with no steps; `fulcrum.currentSpan()` inside it is that span:

```ts
const callModel = traced(
  async function callModel(prompt: string) {
    const reply = await llm(prompt);
    fulcrum.currentSpan()?.setModel(reply.model).setUsage(reply.usage);
    return reply.text;
  },
  { type: 'llm', threadId: conversationId },
);
```

The same holds for `fulcrum.span()` / `startSpan()` — and so for every call
through a provider wrapper — when nothing is in scope: the trace opened to hold
the span carries its input, output, metadata, tags and start time, and is in
scope for whatever the body calls.

### Conversations: `threadId`

Runs that share a `threadId` are one session. The Sessions, Conversation State
and Memory views are built from threads, so an agent that never names one shows
runs and no sessions. `traced()` runs once, when the wrapper is built, which
makes a string there the same thread for every call — pass a function, which is
given the call's own arguments and `this`, or name the thread from inside once
the code knows it:

```ts
const getInsight = traced(
  async function getInsight(email: Email) { … },
  { type: 'llm', threadId: (email) => email.conversationId },
);

const handle = traced(function handle(payload: Payload): string {
  fulcrum.currentTrace()?.setThreadId(parse(payload).conversationId);
  …
});
```

The wrapped function keeps its own name and arity either way. A `threadId`
function that throws costs that run its thread, never the call. An inner
traced step that can name the thread gives it to a run that has none.

### Ids

Ids are minted for you as version 7 UUIDs. Supply your own (`id`) only to make
a retry idempotent, and mint it with `newId()`: the telemetry store takes
version 7 and nothing else, and refuses the whole request over one that is not,
so any other id — a `crypto.randomUUID()` included — is replaced. Read the id
in use back from `trace.id` / `span.id`.

### Long-running traces

Open and close by hand when the start and end are far apart:

```ts
const trace = fulcrum.startTrace({ name: 'batch-job', threadId: conversationId });
const span = trace.startSpan({ name: 'step-1', type: 'tool' });
span.end({ output: result });
trace.end({ output: summary });
```

With `streamSpans: true` each span is posted as it closes rather than waiting
for its trace, so a job that runs for an hour shows progress while it runs.
Sampling still applies to the run as a whole: the spans of a trace that was
sampled out are not posted either.

---

## Feedback scores

Scores usually arrive later than the run they describe — a thumbs-down minutes
after the answer, a judge's verdict after an offline pass — so they are posted
separately.

```ts
fulcrum.score({ id: traceId, name: 'helpfulness', value: 0.9, reason: 'resolved' });
fulcrum.score({ id: spanId, name: 'grounded', value: 1, target: 'span' });
```

Inside a trace, score what is currently in scope. If the unit is still open the
score travels with it rather than costing a second request:

```ts
fulcrum.scoreCurrent({ name: 'answered', value: 1 });
```

End-user feedback and governance events go through their own methods, which map
onto `POST /ingest/events`:

```ts
fulcrum.submitFeedback({ traceId, rating: 5, sentiment: 'positive', body: '👍' });
fulcrum.guardrailTriggered({ guardrail: 'pii-filter', actionTaken: 'Masked', score: 0.98 });
fulcrum.policyViolation({ policy: 'no-medical-advice', severity: 'high' });
```

---

## Prompts

Pull a prompt from the Prompt Manager and you get versioning, review and
rollback for free — but also a network round trip on a path that used to be a
string literal. So every lookup is cached, and the cache is the point rather
than an optimisation.

```ts
const prompt = await fulcrum.prompts.get('support-system');
const text = prompt.format({ customer: 'Ada', tier: 'gold' });
```

Unpinned lookups resolve to the prompt's current head and are cached for a TTL,
because the reason to fetch a prompt is that someone may change it without a
redeploy. Pinning to a commit caches forever, because a commit cannot change:

```ts
const pinned = await fulcrum.prompts.get('support-system', { commit: 'a1b2c3d' });
```

A placeholder with no value is left as `{{visible}}` rather than replaced with
an empty string — a visible placeholder in a model's context is a bug someone
notices, and a silently missing one is a bug nobody does.

---

## Batching, flushing and shutdown

Enqueueing is synchronous and cannot fail: a finished trace hands its payload
over and returns. No promise, no throw, nothing on your hot path.

The queue flushes when it reaches `maxItems`, when the pending body would
exceed `maxBytes`, when the traces waiting carry `maxSpans` spans between them,
or every `flushIntervalMs`, whichever comes first — and each request is cut to
fit all three, because the server refuses a request whole (413) past 1,000
spans or 8 MB however few traces it holds. A request that is refused as too
large anyway is halved and resent rather than dropped. Failed sends are retried
with exponential backoff and full jitter, honouring `Retry-After`. The queue is bounded: past `maxQueueSize` the *oldest* items are
dropped, because during an outage the freshest telemetry is the telemetry worth
having.

```ts
const fulcrum = new FulcrumOps({
  batch: { maxItems: 100, maxBytes: 4_194_304, maxSpans: 1_000, flushIntervalMs: 5_000, maxQueueSize: 10_000 },
  retry: { maxAttempts: 3, backoffMs: 500, maxBackoffMs: 30_000 },
  onError: (error, { operation }) => log.warn({ operation, err: error }, 'telemetry dropped'),
});
```

- `await fulcrum.flush()` — send everything queued, now.
- `await fulcrum.close()` — flush and stop. Idempotent.
- `flushOnExit` (default `true`) flushes on Node's `beforeExit`, on `SIGTERM` /
  `SIGINT`, and on the browser's `pagehide`/`beforeunload`.

`SIGTERM` is how a container is stopped, so without that hook every deploy
would lose the last few seconds of runs. It does not change how your process
answers the signal. If you handle it yourself, the SDK starts a flush and
leaves the shutdown to you — call `await fulcrum.close()` in your handler. If
you do not, the flush gets two seconds at most and the signal is then raised
again, so the process ends exactly as it would have.

Prefer an explicit `close()` in a short-lived process. Exit hooks are a safety
net, not a guarantee: `process.exit()` and `SIGKILL` both skip them. The
start-up `/ingest/config` fetch never keeps a finished process alive: in the
background it is one short attempt at a time, and the waits between retries do
not hold the event loop.

`getStats()` reports what happened — `pending`, `sent`, `accepted`, `rejected`,
`blocked`, `dropped`, `failedBatches`, `sampledOut`. A batch is answered with
HTTP 200 even when individual rows are refused by a policy or guardrail, so
`accepted` and `blocked` are where that shows up, not the status code.

---

## Provider wrappers

Optional peer dependencies, imported lazily: nothing provider-shaped loads
unless you ask for it, and the subpaths are safe to import without the provider
installed.

```ts
import OpenAI from 'openai';
import { wrapOpenAI } from '@fulcrum-ops/sdk/openai';

const openai = wrapOpenAI(new OpenAI(), fulcrum);
```

The wrapper opens an `llm` span around `chat.completions.create`,
`responses.create` and `embeddings.create`, records the model, token usage and
output, and returns the provider's own value untouched — the provider's own
promise and stream objects, so `.withResponse()`, `.asResponse()`, `.tee()`,
`.toReadableStream()` and Anthropic's `stream.on(...)` / `finalMessage()` work
as they do on an unwrapped client. Streaming is handled properly rather than
skipped: a streamed call resolves before the first token, so the span closes
when the stream is exhausted — otherwise a 12-second generation would be
reported as taking 40ms. A stream handed out of the trace it was opened in (a
chat route returning it to be piped) keeps its run open until it has been read.

Token counters are recorded as `prompt_tokens` / `completion_tokens` whichever
spelling the provider uses, as the Python SDK does. OpenAI sends no usage on a
streamed Chat Completion unless asked; `wrapOpenAI(client, fulcrum, {
streamUsage: true })` asks for it (it adds a final chunk with empty `choices`,
which is why it is opt-in). The Responses API always reports usage.

```ts
import Anthropic from '@anthropic-ai/sdk';
import { wrapAnthropic } from '@fulcrum-ops/sdk/anthropic';

const anthropic = wrapAnthropic(new Anthropic(), fulcrum);
```

For LangChain.js, pass the callback handler wherever callbacks are accepted:

```ts
import { FulcrumOpsCallbackHandler } from '@fulcrum-ops/sdk/langchain';

await chain.invoke(input, { callbacks: [new FulcrumOpsCallbackHandler(fulcrum)] });
```

---

## Redaction and capture

On start-up the SDK fetches `GET /ingest/config` and adopts the deployment's
sampling, batching and redaction settings. The fetch is fire-and-forget, so
start-up never blocks on it. Where a caller's setting and the deployment's
disagree, the **smaller** wins: the document expresses a limit the deployment
enforces, not a preference.

Redaction rules from that document are applied **locally, before content leaves
the process** — a rule that matches a credit card number means the number is
never sent, not that it is scrubbed on arrival. Add your own on top:

```ts
const fulcrum = new FulcrumOps({
  redaction: [{ id: 'local-1', name: 'internal ids', source: 'workspace', pattern: 'EMP-\\d{6}', replacement: '[redacted]' }],
});
```

To keep payloads off the wire entirely, turn capture off — the runs, timings,
token counts and scores still arrive, without the content:

```ts
const fulcrum = new FulcrumOps({ captureInput: false, captureOutput: false });
```

`samplingRate` reports a fraction of traces. The body always runs; sampling
decides whether the result is reported.

---

## Errors

The SDK never throws from the reporting path. Everything it absorbs goes to
`onError`, typed, with the operation that produced it:

```ts
import { QuotaExceededError, RateLimitError } from '@fulcrum-ops/sdk';

const fulcrum = new FulcrumOps({
  onError: (error, { operation }) => {
    if (error instanceof QuotaExceededError) pageSomeone(error);
    else if (!(error instanceof RateLimitError)) log.warn({ operation }, error.message);
  },
});
```

The two lookups you await for an answer — `config()` and `prompts.get()` — do
reject, because there a failure is the answer: a prompt that cannot be fetched
must not be silently replaced with an empty string.

`flush()` and `close()` never reject, even when the control plane is
unreachable. They are the calls that end up in a `finally` block or a shutdown
hook, and a telemetry flush has no business turning a request that worked into
a request that failed. What went wrong still arrives at `onError`, and the
counts show up in `getStats().failedBatches`.

`ApiError`, `AuthenticationError`, `ConfigurationError`, `NetworkError`,
`NotFoundError`, `PayloadTooLargeError`, `QuotaExceededError`,
`RateLimitError`, `ServerError` and `TimeoutError` all extend
`FulcrumOpsError`.

---

## Browser use

The package ships one build that runs in both places. In a browser:

- Route through your own backend rather than shipping an API key.
- Nesting uses the stack fallback; pass `parent` explicitly for concurrent work.
- `flushOnExit` uses `pagehide`, and the final flush is sent with `keepalive`
  so it survives the page going away.

---

## Development

```bash
npm run build      # ESM, CJS and type declarations into dist/
npm run typecheck  # tsc --noEmit
npm test           # compile and run against a local stub server
```

Tests run against a real HTTP stub on a real socket rather than a mocked
`fetch`, because most of what is worth testing here *is* the HTTP behaviour:
status handling, the retry loop, `Retry-After`, ETag revalidation, keepalive. A
stubbed fetch would let all of those pass while broken.
