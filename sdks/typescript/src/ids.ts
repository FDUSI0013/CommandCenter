/**
 * Identifier minting.
 *
 * The control plane addresses traces and spans by UUID and refuses anything
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

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Whether a caller-supplied id is one the API will accept. */
export function isValidId(value: string): boolean {
  return UUID_PATTERN.test(value);
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
