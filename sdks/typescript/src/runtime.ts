/**
 * The handful of host-environment facts the SDK needs, behind one narrow door.
 *
 * The package ships a single build that runs on Node and in a browser, so
 * nothing below may reference a global that only one of them has. Every such
 * access goes through a guarded accessor here and returns `undefined` when the
 * host does not provide it, which keeps the feature checks in the rest of the
 * SDK to a single `if`.
 */

/** Minimal shape of the pieces of `process` the SDK touches. */
export interface ProcessLike {
  env?: Record<string, string | undefined>;
  versions?: { node?: string };
  /** Node >= 20.16/22.3: synchronous access to a built-in module. */
  getBuiltinModule?: (name: string) => unknown;
  on?: (event: string, listener: (...args: unknown[]) => void) => unknown;
  off?: (event: string, listener: (...args: unknown[]) => void) => unknown;
  once?: (event: string, listener: (...args: unknown[]) => void) => unknown;
  removeListener?: (event: string, listener: (...args: unknown[]) => void) => unknown;
}

interface GlobalLike {
  process?: ProcessLike;
  crypto?: { getRandomValues?: (array: Uint8Array) => Uint8Array; randomUUID?: () => string };
  TextEncoder?: new () => { encode(input: string): Uint8Array };
  addEventListener?: (type: string, listener: () => void, options?: unknown) => void;
  removeEventListener?: (type: string, listener: () => void, options?: unknown) => void;
  document?: unknown;
  fetch?: typeof fetch;
}

const globals = globalThis as unknown as GlobalLike;

/** `process` when running on Node, `undefined` in a browser. */
export function getProcess(): ProcessLike | undefined {
  const candidate = globals.process;
  return candidate && typeof candidate === 'object' ? candidate : undefined;
}

/** True on Node (and Node-compatible runtimes that report `process.versions.node`). */
export function isNode(): boolean {
  return typeof getProcess()?.versions?.node === 'string';
}

/** True in a document-bearing browser environment. */
export function isBrowser(): boolean {
  return typeof globals.document !== 'undefined' && typeof globals.addEventListener === 'function';
}

/** One environment variable, or `undefined` where there is no environment. */
export function readEnv(name: string): string | undefined {
  const value = getProcess()?.env?.[name];
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

/**
 * A Node built-in module, synchronously, or `undefined` where that is not possible.
 *
 * `process.getBuiltinModule` landed in Node 20.16/22.3 and is the only way to
 * reach `node:async_hooks` from ESM without an `await`. Older Node and every
 * browser get `undefined` and the caller falls back to a dynamic import.
 */
export function getBuiltinModule(name: string): unknown {
  const load = getProcess()?.getBuiltinModule;
  if (typeof load !== 'function') return undefined;
  try {
    return load.call(getProcess(), name);
  } catch {
    return undefined;
  }
}

/** `globalThis.fetch`, which Node has had since 18 and browsers have always had. */
export function getFetch(): typeof fetch | undefined {
  return typeof globals.fetch === 'function' ? globals.fetch.bind(globalThis) : undefined;
}

/** Cryptographically strong random bytes when available, `Math.random` when not. */
export function randomBytes(length: number): Uint8Array {
  const out = new Uint8Array(length);
  const webcrypto = globals.crypto;
  if (webcrypto && typeof webcrypto.getRandomValues === 'function') {
    webcrypto.getRandomValues(out);
    return out;
  }
  for (let index = 0; index < length; index += 1) {
    out[index] = Math.floor(Math.random() * 256);
  }
  return out;
}

let encoder: { encode(input: string): Uint8Array } | undefined;

/**
 * UTF-8 byte length of a string.
 *
 * The batch limit the control plane enforces is in bytes, not characters, so a
 * payload of emoji or CJK text must be measured as the server will measure it.
 */
export function byteLength(value: string): number {
  const TextEncoderCtor = globals.TextEncoder;
  if (TextEncoderCtor) {
    encoder ??= new TextEncoderCtor();
    return encoder.encode(value).length;
  }
  // Last resort: count UTF-8 code units by hand.
  let bytes = 0;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff) {
      bytes += 4;
      index += 1;
    } else bytes += 3;
  }
  return bytes;
}

/** Register a browser page-teardown listener; returns the undo function. */
export function onPageHide(listener: () => void): () => void {
  if (!isBrowser() || typeof globals.addEventListener !== 'function') return () => undefined;
  const events = ['pagehide', 'beforeunload'];
  for (const event of events) globals.addEventListener(event, listener);
  return () => {
    if (typeof globals.removeEventListener !== 'function') return;
    for (const event of events) globals.removeEventListener(event, listener);
  };
}

/** Register a Node exit-path listener; returns the undo function. */
export function onProcessExit(listener: () => void): () => void {
  const proc = getProcess();
  if (!proc || typeof proc.on !== 'function') return () => undefined;
  // `beforeExit` only — a SIGINT/SIGTERM handler would stop the default
  // "terminate now" behaviour a host expects, which is not the SDK's to change.
  proc.on('beforeExit', listener);
  return () => {
    const remove = proc.off ?? proc.removeListener;
    if (typeof remove === 'function') remove.call(proc, 'beforeExit', listener);
  };
}

/** A `setTimeout` handle that never keeps a Node process alive on its own. */
export function unrefTimer(handle: unknown): void {
  const timer = handle as { unref?: () => void } | undefined;
  if (timer && typeof timer.unref === 'function') timer.unref();
}
