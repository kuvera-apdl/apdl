/**
 * The SDK version, injected from package.json at build time (Rollup) and test
 * time (Vitest), so it always tracks the published version with no manual sync.
 * package.json is the single source of truth — enforced by package-scripts.test.ts.
 */
declare const __APDL_SDK_VERSION__: string;
export const SDK_VERSION = __APDL_SDK_VERSION__;

/** Value sent in the X-APDL-SDK header to identify this SDK build. */
export const SDK_IDENTIFIER = `js/${SDK_VERSION}`;

export const API_KEY_HEADER = 'X-API-Key';
export const SDK_IDENTIFIER_HEADER = 'X-APDL-SDK';

/** Query parameter the services accept when headers are unavailable (SSE, beacons). */
export const API_KEY_QUERY_PARAM = 'api_key';
