/**
 * Environment-variable conventions for zero-config setup.
 *
 * When `endpoint` / `auth.clientKey` are not passed to `init()`, they are
 * resolved from these variables so the common case needs no manual wiring:
 *
 * - Browser bundlers (Next.js, Vite) inline the `NEXT_PUBLIC_*` / public vars.
 * - Server runtimes read the unprefixed `APDL_*` vars from `process.env`.
 *
 * The earliest defined, non-empty value wins.
 */

const ENDPOINT_ENV_VARS = [
  'NEXT_PUBLIC_APDL_URL',
  'NEXT_PUBLIC_APDL_ENDPOINT',
  'APDL_URL',
  'APDL_ENDPOINT',
] as const;

const CLIENT_KEY_ENV_VARS = [
  'NEXT_PUBLIC_APDL_CLIENT_KEY',
  'APDL_CLIENT_KEY',
] as const;

function readEnv(name: string): string | undefined {
  // `process` is undefined in some browser bundles; guard before touching it.
  if (typeof process === 'undefined' || process.env == null) {
    return undefined;
  }

  const value = process.env[name];
  if (typeof value !== 'string') {
    return undefined;
  }

  const trimmed = value.trim();
  return trimmed === '' ? undefined : trimmed;
}

function firstEnv(names: readonly string[]): string | undefined {
  for (const name of names) {
    const value = readEnv(name);
    if (value !== undefined) {
      return value;
    }
  }
  return undefined;
}

/** Resolves the ingestion/config endpoint from documented env conventions. */
export function endpointFromEnv(): string | undefined {
  return firstEnv(ENDPOINT_ENV_VARS);
}

/** Resolves the client key from documented env conventions. */
export function clientKeyFromEnv(): string | undefined {
  return firstEnv(CLIENT_KEY_ENV_VARS);
}
