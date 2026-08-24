/**
 * Serialisation and id minting.
 *
 * The rule these tests encode is that conversion is *total*: every input
 * produces some JSON value, and the lossy cases are labelled rather than
 * dropped. A `JSON.stringify` throw inside a `trace()` body would be the SDK
 * breaking the caller's code, which is the one thing it must never do.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { isValidId, newId } from '../src/index.js';
import {
  approximateBytes,
  argsToJsonObject,
  encodeBody,
  normaliseMetadata,
  normaliseTags,
  normaliseUsage,
  toErrorInfo,
  toJsonObject,
  toJsonValue,
} from '../src/serialize.js';

describe('toJsonValue', () => {
  it('handles the types JSON.stringify refuses outright', () => {
    assert.equal(toJsonValue(10n), '10');
    assert.equal(toJsonValue(Number.POSITIVE_INFINITY), 'Infinity');
    assert.equal(toJsonValue(Number.NaN), 'NaN');
    assert.equal(toJsonValue(Symbol('tag')), 'Symbol(tag)');
    assert.equal(toJsonValue(function namedFn() {}), '[function namedFn]');
    assert.equal(toJsonValue(undefined), undefined);
    assert.equal(toJsonValue(null), null);
  });

  it('labels a cycle instead of overflowing the stack', () => {
    const node: Record<string, unknown> = { name: 'a' };
    node.self = node;
    node.children = [{ parent: node }];
    assert.deepEqual(toJsonValue(node), {
      name: 'a',
      self: '[circular]',
      children: [{ parent: '[circular]' }],
    });
  });

  it('does not mistake a repeated sibling for a cycle', () => {
    const shared = { id: 1 };
    assert.deepEqual(toJsonValue({ a: shared, b: shared }), { a: { id: 1 }, b: { id: 1 } });
  });

  it('converts the collection types a caller’s code already holds', () => {
    assert.deepEqual(toJsonValue(new Map([['k', 'v']])), { k: 'v' });
    assert.deepEqual(toJsonValue(new Set([1, 2])), [1, 2]);
    assert.equal(toJsonValue(new Date('2026-01-02T03:04:05.000Z')), '2026-01-02T03:04:05.000Z');
    assert.equal(toJsonValue(new Date('nonsense')), null);
    assert.equal(toJsonValue(/ab+c/gi), '/ab+c/gi');
    assert.equal(toJsonValue(new URL('https://example.com/x')), 'https://example.com/x');
  });

  it('respects an explicit toJSON, which is how provider objects ask to be written', () => {
    const provider = { secret: 'do not send', toJSON: () => ({ id: 'msg_1', model: 'm' }) };
    assert.deepEqual(toJsonValue(provider), { id: 'msg_1', model: 'm' });
  });

  it('falls back to enumeration when toJSON throws', () => {
    const awkward = {
      value: 1,
      toJSON() {
        throw new Error('nope');
      },
    };
    assert.deepEqual(toJsonValue(awkward), { value: 1 });
  });

  it('renders an Error as a readable object', () => {
    const rendered = toJsonValue(new TypeError('bad input')) as Record<string, unknown>;
    assert.equal(rendered.name, 'TypeError');
    assert.equal(rendered.message, 'bad input');
    assert.ok(typeof rendered.stack === 'string');
  });

  it('caps very deep and very wide structures rather than sending them', () => {
    let deep: Record<string, unknown> = { end: true };
    for (let index = 0; index < 30; index += 1) deep = { nested: deep };
    assert.ok(JSON.stringify(toJsonValue(deep)).includes('[max depth]'));

    const wide = toJsonValue(Array.from({ length: 1_200 }, (_v, index) => index)) as unknown[];
    assert.equal(wide.length, 1_001, '1000 items plus a marker for the remainder');
    assert.equal(wide[1_000], '[+200 more]');
  });
});

describe('toJsonObject', () => {
  it('boxes a bare value so the contract’s object-typed fields accept it', () => {
    assert.deepEqual(toJsonObject('just a string'), { value: 'just a string' });
    assert.deepEqual(toJsonObject([1, 2]), { value: [1, 2] });
    assert.deepEqual(toJsonObject({ already: 'an object' }), { already: 'an object' });
    assert.equal(toJsonObject(undefined), undefined);
  });

  it('names positional arguments', () => {
    assert.deepEqual(argsToJsonObject(['a', 2]), { arg0: 'a', arg1: 2 });
    assert.deepEqual(argsToJsonObject(['a', 2], ['text', 'limit']), { text: 'a', limit: 2 });
    assert.equal(argsToJsonObject([]), undefined);
  });
});

describe('normalisation', () => {
  it('caps metadata keys and says how many it dropped', () => {
    const many = Object.fromEntries(Array.from({ length: 100 }, (_v, index) => [`k${index}`, index]));
    const normalised = normaliseMetadata(many)!;
    assert.equal(Object.keys(normalised).length, 64);
    assert.equal(normalised.truncated_keys, 100 - 63);
  });

  it('de-duplicates, trims and caps tags', () => {
    assert.deepEqual(normaliseTags(['  a  ', 'a', 'b']), ['a', 'b']);
    assert.equal(normaliseTags(Array.from({ length: 100 }, (_v, index) => `t${index}`))!.length, 32);
    assert.equal(normaliseTags([]), undefined);
    assert.equal(normaliseTags(undefined), undefined);
  });

  it('rounds usage, drops nonsense, and derives the total from either dialect', () => {
    assert.deepEqual(normaliseUsage({ prompt_tokens: 10.6, completion_tokens: 4.2 }), {
      prompt_tokens: 11,
      completion_tokens: 4,
      total_tokens: 15,
    });
    assert.deepEqual(normaliseUsage({ input_tokens: 3, output_tokens: 7 }), {
      input_tokens: 3,
      output_tokens: 7,
      total_tokens: 10,
    });
    assert.deepEqual(normaliseUsage({ prompt_tokens: 1, bogus: 'x', negative: -4 }), {
      prompt_tokens: 1,
      total_tokens: 1,
    });
    assert.equal(normaliseUsage({}), undefined);
    assert.equal(normaliseUsage(undefined), undefined);
  });

  it('keeps a total the provider supplied', () => {
    assert.deepEqual(normaliseUsage({ prompt_tokens: 1, completion_tokens: 1, total_tokens: 99 }), {
      prompt_tokens: 1,
      completion_tokens: 1,
      total_tokens: 99,
    });
  });
});

describe('toErrorInfo', () => {
  it('renders a thrown Error into the contract’s shape', () => {
    const info = toErrorInfo(new RangeError('out of range'));
    assert.equal(info.exception_type, 'RangeError');
    assert.equal(info.message, 'out of range');
    assert.ok(typeof info.traceback === 'string');
  });

  it('renders a thrown non-Error too, because JavaScript allows it', () => {
    assert.deepEqual(toErrorInfo('a bare string'), {
      exception_type: 'Error',
      message: 'a bare string',
      traceback: null,
    });
  });

  it('trims an enormous message to the contract ceiling', () => {
    const info = toErrorInfo(new Error('x'.repeat(9_000)));
    assert.equal(info.message!.length, 4_000);
  });
});

describe('encodeBody', () => {
  it('measures the payload in UTF-8 bytes, as the server does', () => {
    assert.equal(encodeBody({ a: 1 }).text, '{"a":1}');
    // Four characters, twelve bytes: a byte budget must not be counted in
    // characters or a CJK payload silently triples it.
    assert.equal(encodeBody('한국어').bytes, encodeBody('한국어').text.length + 6);
    assert.ok(approximateBytes({ a: 'b' }) > 0);
  });

  it('still produces a body for a value stringify would reject', () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    assert.equal(encodeBody(cyclic).text, '{"self":"[circular]"}');
  });
});

describe('id minting', () => {
  it('mints well-formed, unique, time-ordered UUIDv7s', () => {
    const first = newId();
    assert.ok(isValidId(first), `${first} is a UUID the API will accept`);
    assert.equal(first[14], '7', 'version 7');
    assert.ok('89ab'.includes(first[19]!), 'RFC 4122 variant');

    const many = new Set(Array.from({ length: 5_000 }, () => newId()));
    assert.equal(many.size, 5_000, 'no collisions');
  });

  it('sorts by mint time, which is what keeps primary keys clustered', async () => {
    const early = newId();
    await new Promise((resolve) => setTimeout(resolve, 5));
    const late = newId();
    assert.ok(early < late, `${early} should sort before ${late}`);
  });

  it('rejects an id the API would refuse', () => {
    assert.equal(isValidId('not-a-uuid'), false);
    assert.equal(isValidId(''), false);
  });
});
