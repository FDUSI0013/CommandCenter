/**
 * Machinery the provider wrappers share.
 *
 * The wrappers all face the same problem: instrument someone else's client
 * without owning it, without importing it, and without changing what it
 * returns. The answer is a `Proxy` that forwards everything and intercepts one
 * method — a subclass would need the constructor, and monkey-patching the
 * instance would be visible to the caller's other code.
 *
 * What a call *returns* is a different matter, and is never proxied. The
 * provider SDKs keep their state in `#private` fields, and a method reached
 * through a proxy runs with the proxy as `this` — so `stream.on(...)`,
 * `stream.tee()` and `promise.withResponse()` all die with "Cannot read private
 * member" the moment a stand-in is handed back. The caller therefore gets the
 * provider's own promise and the provider's own stream, and the span watches
 * them from the side: `observePromise` and `observeIteration` below.
 *
 * None of the provider packages are imported at module load. `openai` and
 * `@anthropic-ai/sdk` are never imported at all by the `wrap*` functions: the
 * caller passes an instance they already built. The `create*` helpers do import
 * them, lazily, so a project that never calls one never pays for it and a
 * project that does not install them still builds.
 */

/** Load an optional peer dependency, with a message that names the fix. */
export async function loadOptionalPackage<T>(specifier: string, installHint: string): Promise<T> {
  try {
    // Non-literal specifier: a bundler cannot resolve it statically, so an
    // uninstalled optional peer does not become a build error.
    const moduleName = specifier;
    return (await import(/* webpackIgnore: true */ /* @vite-ignore */ moduleName)) as T;
  } catch (cause) {
    throw new Error(
      `The "${specifier}" package is required for this integration but could not be loaded. Install it with \`${installHint}\`.`,
      { cause },
    );
  }
}

/**
 * Replace one nested method on an object graph, leaving everything else alone.
 *
 * `path` is walked with a proxy per level so `client.chat.completions.create`
 * can be intercepted without the intermediate `chat` and `completions` objects
 * being rebuilt — the provider SDKs hang state and other methods off those.
 */
export function proxyMethod<T extends object>(
  target: T,
  path: readonly string[],
  wrap: (original: (...args: never[]) => unknown, self: unknown) => (...args: never[]) => unknown,
): T {
  if (path.length === 0) return target;

  return new Proxy(target, {
    get(object, property) {
      if (property !== path[0]) return forward(object, property);
      // Read with the real object as the receiver, never the proxy: an accessor
      // that touches a `#private` field throws when `this` is a stand-in.
      const value = Reflect.get(object, property, object);

      if (path.length === 1) {
        if (typeof value !== 'function') return value;
        const wrapped = wrap(value.bind(object) as (...args: never[]) => unknown, object);
        // Copy own properties so a caller checking `create.length` or a
        // provider's own annotations still sees them.
        return wrapped;
      }

      if (value === null || typeof value !== 'object') return value;
      return proxyMethod(value as object, path.slice(1), wrap);
    },
  });
}

type AnyFunction = (...args: unknown[]) => unknown;

const boundMethods = new WeakMap<object, Map<PropertyKey, { source: AnyFunction; bound: AnyFunction }>>();

/**
 * Read a property the wrapper is not intercepting, as the real object would.
 *
 * A method inherited from the provider's class is handed back bound to the real
 * instance, so `wrapped.post(...)` or `wrapped.messages.countTokens(...)` runs
 * with the `this` its `#private` fields belong to. The binding is cached, so
 * `wrapped.fn === wrapped.fn` still holds. Own properties are left exactly as
 * they are: an instance field holding a function is already bound to whatever
 * it needs, and `constructor` has to stay the class.
 */
function forward(object: object, property: PropertyKey): unknown {
  const value = Reflect.get(object, property, object) as unknown;
  if (typeof value !== 'function' || property === 'constructor') return value;
  if (Object.prototype.hasOwnProperty.call(object, property)) return value;

  let cache = boundMethods.get(object);
  if (!cache) {
    cache = new Map();
    boundMethods.set(object, cache);
  }
  const known = cache.get(property);
  if (known && known.source === value) return known.bound;
  const bound = (value as AnyFunction).bind(object) as AnyFunction;
  cache.set(property, { source: value as AnyFunction, bound });
  return bound;
}

/** True for a promise or anything else `await` would wait on. */
export function isThenable(value: unknown): value is PromiseLike<unknown> {
  return (
    value !== null &&
    (typeof value === 'object' || typeof value === 'function') &&
    typeof (value as PromiseLike<unknown>).then === 'function'
  );
}

function define(target: object, key: PropertyKey, value: unknown): void {
  Object.defineProperty(target, key, { value, configurable: true, writable: true, enumerable: false });
}

/** Run a telemetry step that must not be able to fail the caller's own chain. */
function quietly(step: () => void): void {
  try {
    step();
  } catch {
    /* the span is best-effort; the provider call is not */
  }
}

/** What a wrapper wants to hear about the promise it handed back. */
export interface PromiseObserver {
  /**
   * The call resolved. Returns what the caller should receive — the same value
   * in every case but one: a stream that could not be watched in place.
   */
  onValue(value: unknown): unknown;
  /** The call rejected. */
  onError(error: unknown): void;
  /** The caller took the raw HTTP response instead; its body is theirs to read. */
  onRawResponse(): void;
}

/**
 * Watch a provider call settle, and hand the caller back the provider's promise.
 *
 * `openai` and `@anthropic-ai/sdk` return an `APIPromise`, a `Promise` subclass
 * carrying `.withResponse()` and `.asResponse()`. Returning `promise.then(...)`
 * would swap it for a plain `Promise` and those helpers would be gone — which
 * is how people read rate-limit headers — so the instance itself goes back, with
 * `then` shadowed on it. Everything the caller can do with it (`await`,
 * `.catch`, `.finally`, `Promise.all`) funnels through `then`, so the span sees
 * the outcome at the moment the caller does and at no other: nothing is parsed
 * early (an eager `then` would consume the body `asResponse()` promises to leave
 * unread), and a call nobody awaits still surfaces as the unhandled rejection it
 * would have been.
 *
 * A plain `Promise` has nothing to preserve, and `await` reads a native promise
 * through its internal slots without ever calling a shadowed `then` — so that
 * case keeps the ordinary derived promise.
 */
export function observePromise(promise: PromiseLike<unknown>, observer: PromiseObserver): PromiseLike<unknown> {
  const native = promise instanceof Promise && promise.constructor === Promise;
  if (!native) {
    try {
      return observeInPlace(promise, observer);
    } catch {
      /* frozen or otherwise unpatchable: fall through to the derived promise */
    }
  }
  return promise.then(
    (value) => settledValue(observer, value),
    (error: unknown) => {
      quietly(() => observer.onError(error));
      throw error;
    },
  );
}

function settledValue(observer: PromiseObserver, value: unknown): unknown {
  let seen = value;
  quietly(() => {
    seen = observer.onValue(value);
  });
  return seen;
}

function observeInPlace(promise: PromiseLike<unknown>, observer: PromiseObserver): PromiseLike<unknown> {
  const target = promise as unknown as Record<string, unknown>;
  const then = target.then as AnyFunction;
  const withResponse = target.withResponse;
  const asResponse = target.asResponse;
  // `withResponse()` is built on `asResponse()`. While it is running, the raw
  // response is not "the caller took the body" — the parsed value is coming.
  let insideWithResponse = 0;

  const observedThen = (onFulfilled?: unknown, onRejected?: unknown): Promise<unknown> =>
    then.call(
      promise,
      (value: unknown) => {
        const seen = settledValue(observer, value);
        return typeof onFulfilled === 'function' ? (onFulfilled as AnyFunction)(seen) : seen;
      },
      (error: unknown) => {
        quietly(() => observer.onError(error));
        if (typeof onRejected === 'function') return (onRejected as AnyFunction)(error);
        throw error;
      },
    ) as Promise<unknown>;

  define(target, 'then', observedThen);
  // `APIPromise` routes `catch` and `finally` to its parse step directly rather
  // than through `then`, so `create(...).catch(fallback)` would otherwise go
  // unseen. Both are, by definition, a `then` with one side filled in.
  define(target, 'catch', (onRejected?: unknown) => observedThen(undefined, onRejected));
  define(target, 'finally', (onFinally?: unknown) =>
    Promise.resolve(observedThen()).finally(onFinally as (() => void) | undefined),
  );

  if (typeof withResponse === 'function') {
    define(target, 'withResponse', (...args: unknown[]) => {
      insideWithResponse += 1;
      let outcome: unknown;
      try {
        outcome = (withResponse as AnyFunction).apply(promise, args);
      } finally {
        insideWithResponse -= 1;
      }
      if (!isThenable(outcome)) return outcome;
      return outcome.then(
        (envelope) => {
          const data = pick<unknown>(envelope, 'data');
          const seen = settledValue(observer, data);
          return seen === data ? envelope : { ...(envelope as object), data: seen };
        },
        (error: unknown) => {
          quietly(() => observer.onError(error));
          throw error;
        },
      );
    });
  }

  if (typeof asResponse === 'function') {
    define(target, 'asResponse', (...args: unknown[]) => {
      const outcome = (asResponse as AnyFunction).apply(promise, args);
      if (insideWithResponse > 0 || !isThenable(outcome)) return outcome;
      return outcome.then(
        (response) => {
          quietly(() => observer.onRawResponse());
          return response;
        },
        (error: unknown) => {
          quietly(() => observer.onError(error));
          throw error;
        },
      );
    });
  }

  return promise;
}

/** What a wrapper wants to hear about the stream it handed back. */
export interface IterationObserver {
  onItem(item: unknown): void;
  /** The stream is over: exhausted, abandoned by the reader, or failed. */
  onEnd(error?: unknown): void;
}

const watched = new WeakSet<object>();

function watchIterator(inner: AsyncIterator<unknown>, observer: IterationObserver): AsyncIterator<unknown> {
  return {
    async next(...args: [] | [undefined]) {
      try {
        const result = await inner.next(...args);
        if (result.done) quietly(() => observer.onEnd());
        else quietly(() => observer.onItem(result.value));
        return result;
      } catch (error) {
        quietly(() => observer.onEnd(error));
        throw error;
      }
    },
    async return(value?: unknown) {
      quietly(() => observer.onEnd());
      return inner.return ? inner.return(value) : { done: true as const, value };
    },
    async throw(error?: unknown) {
      quietly(() => observer.onEnd(error));
      if (inner.throw) return inner.throw(error);
      throw error;
    },
    [Symbol.asyncIterator]() {
      return this;
    },
  } as AsyncIterator<unknown>;
}

/**
 * Watch a stream being read, and hand the caller back the provider's stream.
 *
 * The object is instrumented where it stands rather than wrapped, for the
 * reason at the top of this file. The provider `Stream` classes keep the
 * function that starts iteration in an `iterator` field, and `for await`,
 * `tee()` and `toReadableStream()` all begin there — so that is the one place
 * to stand to see every read, including the `return new
 * Response(stream.toReadableStream())` a route handler ends with. Anything
 * else (an async generator, a custom iterable) gets `Symbol.asyncIterator`
 * shadowed on the instance instead.
 *
 * Only an object that refuses both — a frozen one — falls back to a proxy, and
 * that proxy binds every method to the real stream so private state still
 * resolves.
 */
export function observeIteration<T extends AsyncIterable<unknown>>(stream: T, observer: IterationObserver): T {
  if (watched.has(stream)) return stream;
  const target = stream as unknown as Record<PropertyKey, unknown>;

  try {
    const start = target.iterator;
    if (typeof start === 'function' && Object.prototype.hasOwnProperty.call(target, 'iterator')) {
      target.iterator = function iterator(this: unknown, ...args: unknown[]) {
        const inner = (start as AnyFunction).apply(stream, args) as AsyncIterator<unknown> | undefined;
        // Only stand in front of something that really is an iterator; a field
        // that merely shares the name is handed back as it came.
        return inner && typeof inner.next === 'function' ? watchIterator(inner, observer) : inner;
      };
    } else {
      const iterate = target[Symbol.asyncIterator] as AnyFunction;
      define(target, Symbol.asyncIterator, () =>
        watchIterator(iterate.call(stream) as AsyncIterator<unknown>, observer),
      );
    }
    watched.add(stream);
    return stream;
  } catch {
    const proxy = new Proxy(stream as object, {
      get(object, property) {
        if (property === Symbol.asyncIterator) {
          return () => watchIterator((object as AsyncIterable<unknown>)[Symbol.asyncIterator](), observer);
        }
        const value = Reflect.get(object, property, object) as unknown;
        return typeof value === 'function' ? (value as AnyFunction).bind(object) : value;
      },
    }) as T;
    watched.add(proxy);
    return proxy;
  }
}

/** A provider's spelling of a token counter, and the one the console charts. */
const CANONICAL_COUNTERS: Record<string, string> = {
  input_tokens: 'prompt_tokens',
  output_tokens: 'completion_tokens',
};

/**
 * A provider's `usage` object as the numeric counters the span records.
 *
 * Chat Completions says `prompt_tokens` / `completion_tokens`; Anthropic and
 * the Responses API say `input_tokens` / `output_tokens` for the same two
 * numbers. The console charts one series and the cost estimate reads one pair
 * of names, so both are reported under the first spelling — as the Python SDK
 * does, which is what makes a fleet's per-model breakdown add up across
 * languages. Every other counter (cache reads, reasoning tokens) keeps its own
 * name.
 */
export function canonicalUsage(usage: unknown): Record<string, number> | undefined {
  if (!usage || typeof usage !== 'object') return undefined;
  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(usage as Record<string, unknown>)) {
    if (typeof value === 'number') out[key] = value;
  }
  for (const [native, canonical] of Object.entries(CANONICAL_COUNTERS)) {
    const value = out[native];
    if (value === undefined) continue;
    delete out[native];
    // A payload that carries both spellings already said which it means.
    if (out[canonical] === undefined) out[canonical] = value;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/** Pull the first defined value from a loosely typed provider response. */
export function pick<T>(source: unknown, ...keys: string[]): T | undefined {
  if (!source || typeof source !== 'object') return undefined;
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null) return value as T;
  }
  return undefined;
}

/** True for an async iterator, which is how both providers return a stream. */
export function isAsyncIterable(value: unknown): value is AsyncIterable<unknown> {
  return (
    value !== null &&
    typeof value === 'object' &&
    typeof (value as AsyncIterable<unknown>)[Symbol.asyncIterator] === 'function'
  );
}
