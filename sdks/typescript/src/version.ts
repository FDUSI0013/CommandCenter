/** Identity this SDK reports on every batch, so the console can tell clients apart. */

/** The `sdk` field on every ingest body. */
export const SDK_NAME = 'typescript';

/** Kept in step with `package.json` by hand; there is no build-time inlining. */
export const SDK_VERSION = '1.0.0';

/** Sent as `User-Agent` on Node, where a client is allowed to set one. */
export const USER_AGENT = `fulcrum-ops-sdk-${SDK_NAME}/${SDK_VERSION}`;
