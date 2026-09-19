/**
 * Stand-ins for the provider SDKs' own classes.
 *
 * What makes them faithful is the `#private` state. Both vendors' clients keep
 * theirs that way, and a method reached through a `Proxy` runs with the proxy
 * as `this` and dies on the first private read ("Cannot read private member
 * from an object whose class did not declare it"). A plain-object double cannot
 * catch that, which is how a wrapper that handed back proxies shipped.
 */

/** The slice of a fetch `Response` the doubles need: headers, and a body that reads once. */
export interface FakeHttpResponse {
  headers: Map<string, string>;
  bodyUsed: boolean;
  json(): Promise<unknown>;
}

export function fakeHttpResponse(body: unknown, headers: Record<string, string> = {}): FakeHttpResponse {
  return {
    headers: new Map(Object.entries(headers)),
    bodyUsed: false,
    async json() {
      if (this.bodyUsed) throw new TypeError('Body is unusable: Body has already been read');
      this.bodyUsed = true;
      return body;
    },
  };
}

/**
 * Shaped like the providers' `APIPromise`.
 *
 * A `Promise` subclass that parses lazily, routes `then`, `catch` and `finally`
 * each through its own parse step (none of them through another), and carries
 * `withResponse()` / `asResponse()`.
 */
export class FakeAPIPromise<T> extends Promise<T> {
  #response: Promise<FakeHttpResponse>;
  #parsed: Promise<T> | undefined;

  constructor(response: Promise<FakeHttpResponse>) {
    super((resolve) => resolve(null as unknown as T));
    this.#response = response;
  }

  #parse(): Promise<T> {
    this.#parsed ??= this.#response.then((response) => response.json() as Promise<T>);
    return this.#parsed;
  }

  asResponse(): Promise<FakeHttpResponse> {
    return this.#response;
  }

  async withResponse(): Promise<{ data: T; response: FakeHttpResponse }> {
    const [data, response] = await Promise.all([this.#parse(), this.asResponse()]);
    return { data, response };
  }

  override then<A = T, B = never>(
    onFulfilled?: ((value: T) => A | PromiseLike<A>) | null,
    onRejected?: ((reason: unknown) => B | PromiseLike<B>) | null,
  ): Promise<A | B> {
    return this.#parse().then(onFulfilled, onRejected);
  }

  override catch<B = never>(onRejected?: ((reason: unknown) => B | PromiseLike<B>) | null): Promise<T | B> {
    return this.#parse().catch(onRejected);
  }

  override finally(onFinally?: (() => void) | null): Promise<T> {
    return this.#parse().finally(onFinally);
  }
}

/** Shaped like the providers' `Stream`: an `iterator` field, and a private member `tee()` reads. */
export class FakeStream<T> implements AsyncIterable<T> {
  #client = 'the client this stream came from';
  controller = new AbortController();

  constructor(private iterator: () => AsyncIterator<T>) {}

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return this.iterator();
  }

  tee(): [FakeStream<T>, FakeStream<T>] {
    if (this.#client.length === 0) throw new Error('unreachable');
    const left: Array<Promise<IteratorResult<T>>> = [];
    const right: Array<Promise<IteratorResult<T>>> = [];
    const iterator = this.iterator();
    const branch = (queue: Array<Promise<IteratorResult<T>>>): AsyncIterator<T> => ({
      next: () => {
        if (queue.length === 0) {
          const result = iterator.next();
          left.push(result);
          right.push(result);
        }
        return queue.shift()!;
      },
    });
    return [new FakeStream(() => branch(left)), new FakeStream(() => branch(right))];
  }
}

/**
 * Shaped like Anthropic's `MessageStream`.
 *
 * It runs on its own from the moment it is built and reports through events;
 * the documented use is `.on('text', ...)` and `await stream.finalMessage()`,
 * with no `for await` at all.
 */
export class FakeMessageStream implements AsyncIterable<unknown> {
  #listeners = new Map<string, Array<(...args: unknown[]) => void>>();
  #final: Promise<Record<string, unknown>>;
  errored = false;
  aborted = false;

  constructor(events: Array<Record<string, unknown>>, message: Record<string, unknown>, failure?: Error) {
    this.#final = this.#run(events, message, failure);
    // A failure surfaces through `finalMessage()`, not as a stray rejection
    // from the constructor.
    this.#final.catch(() => undefined);
  }

  on(event: string, listener: (...args: unknown[]) => void): this {
    const listeners = this.#listeners.get(event) ?? [];
    listeners.push(listener);
    this.#listeners.set(event, listeners);
    return this;
  }

  #emit(event: string, ...args: unknown[]): void {
    for (const listener of this.#listeners.get(event) ?? []) listener(...args);
  }

  async #run(
    events: Array<Record<string, unknown>>,
    message: Record<string, unknown>,
    failure?: Error,
  ): Promise<Record<string, unknown>> {
    for (const event of events) {
      await new Promise((resolve) => setTimeout(resolve, 5));
      this.#emit('streamEvent', event);
      const delta = event.delta as { text?: string } | undefined;
      if (delta?.text) this.#emit('text', delta.text);
    }
    if (failure) {
      this.errored = true;
      this.#emit('error', failure);
      this.#emit('end');
      throw failure;
    }
    this.#emit('finalMessage', message);
    this.#emit('end');
    return message;
  }

  finalMessage(): Promise<Record<string, unknown>> {
    return this.#final;
  }

  [Symbol.asyncIterator](): AsyncIterator<unknown> {
    const queue: unknown[] = [];
    let done = false;
    let wake: (() => void) | undefined;
    this.on('streamEvent', (event) => {
      queue.push(event);
      wake?.();
    });
    this.on('end', () => {
      done = true;
      wake?.();
    });
    return {
      next: async () => {
        while (queue.length === 0 && !done) await new Promise<void>((resolve) => (wake = resolve));
        return queue.length > 0 ? { done: false, value: queue.shift() } : { done: true, value: undefined };
      },
    };
  }
}
