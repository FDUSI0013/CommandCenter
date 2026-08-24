/**
 * Where "the current span" lives.
 *
 * Nesting is the feature that makes `client.trace()` worth using: a span opened
 * three call frames deep should attach to the trace above it without anyone
 * threading a parameter through. Doing that requires the runtime to carry a
 * value across `await` boundaries, and only Node can:
 *
 * * **Node** — `AsyncLocalStorage` from `node:async_hooks`. Correct under any
 *   amount of concurrency: two traces running at once each see their own.
 * * **Browser** — there is no such primitive, so the fallback is a plain stack.
 *   It is exactly right for sequential code and wrong for two overlapping
 *   `await`s, which is why every span accepts an explicit `parent` — that is
 *   the escape hatch, and in a browser it is the correct thing to reach for.
 *
 * Acquiring the Node module without breaking browser bundles takes two steps.
 * `process.getBuiltinModule` (Node >= 20.16/22.3) is synchronous, so where it
 * exists the right backend is in force before the first trace opens. Where it
 * does not — Node 18, mainly — the fallback is a dynamic `import()` built from
 * a non-literal specifier, so a bundler targeting the browser cannot see the
 * string and will not try to polyfill or fail on `node:async_hooks`. That path
 * is asynchronous, which means a client can start on the stack backend and
 * switch to `AsyncLocalStorage` a few ticks later, mid-trace. `active()` is
 * written to survive exactly that: a context opened on the stack stays visible
 * after the switch, so a span opened after it still finds its parent instead of
 * being orphaned into a trace of its own.
 */

import { getBuiltinModule, isNode } from './runtime.js';

/** The structural slice of `AsyncLocalStorage` this module uses. */
interface AsyncLocalStorageLike<T> {
  getStore(): T | undefined;
  run<R>(store: T, callback: () => R): R;
}

/** What is in scope at a point in the program. */
export interface ActiveContext {
  /** The open trace. */
  trace: unknown;
  /** The innermost open span, if any. */
  span?: unknown;
}

let alsPromise: Promise<AsyncLocalStorageLike<ActiveContext> | null> | null = null;

/** Shape of the `node:async_hooks` exports this module reads. */
interface AsyncHooksModule {
  AsyncLocalStorage?: new () => AsyncLocalStorageLike<ActiveContext>;
  default?: { AsyncLocalStorage?: new () => AsyncLocalStorageLike<ActiveContext> };
}

function instantiate(module: AsyncHooksModule | undefined): AsyncLocalStorageLike<ActiveContext> | null {
  const Ctor = module?.AsyncLocalStorage ?? module?.default?.AsyncLocalStorage;
  return Ctor ? new Ctor() : null;
}

/**
 * `AsyncLocalStorage` without waiting, where the host can provide it.
 *
 * Returns `null` in a browser, on Node 18, and on edge runtimes that claim to
 * be Node but ship no `async_hooks` — all of which fall back to the import.
 */
export function loadAsyncLocalStorageSync(): AsyncLocalStorageLike<ActiveContext> | null {
  if (!isNode()) return null;
  try {
    return instantiate(getBuiltinModule(['node', 'async_hooks'].join(':')) as AsyncHooksModule | undefined);
  } catch {
    return null;
  }
}

/**
 * Load `AsyncLocalStorage`, once per process, returning `null` off Node.
 *
 * Never rejects: a runtime that reports itself as Node but has no
 * `async_hooks` (some edge runtimes do exactly this) simply gets the stack.
 */
export function loadAsyncLocalStorage(): Promise<AsyncLocalStorageLike<ActiveContext> | null> {
  if (alsPromise) return alsPromise;
  alsPromise = (async () => {
    if (!isNode()) return null;
    const immediate = loadAsyncLocalStorageSync();
    if (immediate) return immediate;
    try {
      // Assembled at runtime so bundlers leave it alone.
      const specifier = ['node', 'async_hooks'].join(':');
      const module = (await import(/* webpackIgnore: true */ /* @vite-ignore */ specifier)) as AsyncHooksModule;
      return instantiate(module);
    } catch {
      return null;
    }
  })();
  return alsPromise;
}

/** Which mechanism a context manager ended up using. */
export type ContextBackend = 'async-local-storage' | 'stack';

/**
 * Holds the active trace and span, by whichever mechanism the host supports.
 *
 * One instance per client, so two clients in the same process cannot see each
 * other's open spans.
 */
export class ContextManager {
  private als: AsyncLocalStorageLike<ActiveContext> | null = null;
  private readonly stack: ActiveContext[] = [];
  private readonly ready: Promise<void>;

  constructor(enableAsyncContext = true) {
    if (!enableAsyncContext) {
      this.ready = Promise.resolve();
      return;
    }
    // Prefer the synchronous acquisition, so the first trace of the process
    // already nests properly rather than the second one.
    this.als = loadAsyncLocalStorageSync();
    if (this.als) {
      this.ready = Promise.resolve();
      return;
    }
    this.ready = loadAsyncLocalStorage().then((storage) => {
      this.als = storage;
    });
    // The promise is stored, never left dangling: `whenReady()` exposes it and
    // an unawaited rejection is impossible because `loadAsyncLocalStorage`
    // resolves to `null` rather than rejecting.
  }

  /** Resolves once the async-context backend has been selected. */
  whenReady(): Promise<void> {
    return this.ready;
  }

  /** Which mechanism is in force right now. */
  get backend(): ContextBackend {
    return this.als ? 'async-local-storage' : 'stack';
  }

  /**
   * The innermost context, or `undefined` at the top level.
   *
   * The stack is consulted even once `AsyncLocalStorage` is in force, because
   * the backend can arrive mid-trace: a trace opened during the import window
   * lives on the stack, and a span opened just after the switch would otherwise
   * find an empty store and be orphaned into a trace of its own. Outside that
   * window the stack is empty — `run()` pushes to it only when there is no
   * `AsyncLocalStorage` — so this costs nothing and cannot cross-contaminate
   * two concurrent traces.
   */
  active(): ActiveContext | undefined {
    if (this.als) return this.als.getStore() ?? this.top();
    return this.top();
  }

  private top(): ActiveContext | undefined {
    return this.stack.length > 0 ? this.stack[this.stack.length - 1] : undefined;
  }

  /**
   * Run `callback` with `context` active.
   *
   * The stack backend must pop in a `finally`, and must pop *its own* entry
   * rather than the top one: an async callback can leave a nested entry behind
   * when it interleaves, and blindly popping would unbalance the stack for
   * everyone else.
   */
  run<R>(context: ActiveContext, callback: () => R): R {
    if (this.als) return this.als.run(context, callback);

    this.stack.push(context);
    let settled = false;
    const pop = () => {
      if (settled) return;
      settled = true;
      const index = this.stack.lastIndexOf(context);
      if (index >= 0) this.stack.splice(index, 1);
    };

    let result: R;
    try {
      result = callback();
    } catch (error) {
      pop();
      throw error;
    }

    // An async callback keeps the context active until its promise settles,
    // which is what makes sequential `await` chains nest correctly.
    if (isPromiseLike(result)) {
      return (result as unknown as Promise<unknown>).then(
        (value) => {
          pop();
          return value;
        },
        (error: unknown) => {
          pop();
          throw error;
        },
      ) as unknown as R;
    }

    pop();
    return result;
  }
}

function isPromiseLike(value: unknown): value is PromiseLike<unknown> {
  return (
    value !== null &&
    (typeof value === 'object' || typeof value === 'function') &&
    typeof (value as PromiseLike<unknown>).then === 'function'
  );
}

export { isPromiseLike };
