/**
 * Machinery the provider wrappers share.
 *
 * The wrappers all face the same problem: instrument someone else's client
 * without owning it, without importing it, and without changing what it
 * returns. The answer is a `Proxy` that forwards everything and intercepts one
 * method — a subclass would need the constructor, and monkey-patching the
 * instance would be visible to the caller's other code.
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
    get(object, property, receiver) {
      const value = Reflect.get(object, property, receiver);
      if (property !== path[0]) return value;

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
