/**
 * Identifier minting.
 *
 * The server addresses traces and spans by UUID and refuses anything
 * else on its own result row, so ids are minted here rather than left to the
 * server. Two things follow from doing it client-side:
 *
 * * **Retries are idempotent.** A batch that times out mid-flight can be resent
 *   with the same ids, and the second attempt overwrites rather than duplicates.
 * * **Ids are time-ordered.** The first 48 bits are the millisecond clock, so
 *   primary keys cluster by insert time in the store instead of scattering.
 */

import { randomBytes } from './runtime.js';

const HEX = '0123456789abcdef';

function hex(byte: number): string {
  return HEX[(byte >> 4) & 0x0f]! + HEX[byte & 0x0f]!;
}

/**
 * A time-ordered UUIDv7.
 *
 * Layout: 48 bits of Unix milliseconds, the version nibble, then random bits.
 * The version and variant fields are set so the value is a well-formed UUID for
 * any parser, which matters because the API validates it as one.
 */
export function newId(): string {
  const bytes = randomBytes(16);
  const millis = Date.now();

  // 48-bit big-endian timestamp.
  bytes[0] = (millis / 2 ** 40) & 0xff;
  bytes[1] = (millis / 2 ** 32) & 0xff;
  bytes[2] = (millis / 2 ** 24) & 0xff;
  bytes[3] = (millis / 2 ** 16) & 0xff;
  bytes[4] = (millis / 2 ** 8) & 0xff;
  bytes[5] = millis & 0xff;

  bytes[6] = (bytes[6]! & 0x0f) | 0x70; // version 7
  bytes[8] = (bytes[8]! & 0x3f) | 0x80; // RFC 4122 variant

  let out = '';
  for (let index = 0; index < 16; index += 1) {
    out += hex(bytes[index]!);
    if (index === 3 || index === 5 || index === 7 || index === 9) out += '-';
  }
  return out;
}

const UUID_V7_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/**
 * Whether a caller-supplied trace or span id is one the telemetry store will take.
 *
 * "Is a UUID" is not the test. The store checks the version nibble and accepts
 * version 7 only, and its refusal is not confined to the item that earned it:
 * one `crypto.randomUUID()` (version 4) on a trace costs every trace sent in
 * the same request, and on a span it costs that request's spans while the
 * traces are still reported as accepted. The server's own check stops at
 * the UUID shape, so this is the last place the difference can be caught while
 * it still belongs to one item. An id that fails is replaced by `newId()`.
 */
export function isValidId(value: string): boolean {
  return typeof value === 'string' && UUID_V7_PATTERN.test(value);
}

/** An RFC 3339 timestamp in UTC, which is the only format the API reads. */
export function nowIso(): string {
  return new Date().toISOString();
}

/** Coerce a `Date`, epoch-millis number or ISO string into the wire format. */
export function toIso(value: Date | number | string | undefined): string | undefined {
  if (value === undefined) return undefined;
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'number') return new Date(value).toISOString();
  return value;
}
