/** Must match the package.json version — enforced by package-scripts.test.ts. */
export const SDK_VERSION = '0.1.0';

/** Value sent in the X-APDL-SDK header to identify this SDK build. */
export const SDK_IDENTIFIER = `js/${SDK_VERSION}`;

export const API_KEY_HEADER = 'X-API-Key';
export const SDK_IDENTIFIER_HEADER = 'X-APDL-SDK';

/** Query parameter the services accept when headers are unavailable (SSE, beacons). */
export const API_KEY_QUERY_PARAM = 'api_key';
