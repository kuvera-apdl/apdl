/**
 * Environment-variable conventions for zero-config setup.
 *
 * When `endpoint` / `auth.clientKey` are not passed to `init()`, they are
 * resolved from these variables so the common case needs no manual wiring:
 *
 * - Next.js inlines the `NEXT_PUBLIC_*` vars into browser bundles.
 * - Server runtimes read the unprefixed `APDL_*` vars from `process.env`.
 *
 * The earliest defined, non-empty value wins.
 */

function normalizeEnv(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }

  const trimmed = value.trim();
  return trimmed === '' ? undefined : trimmed;
}

function nextPublicEndpoint(): unknown {
  try {
    // Keep this as a direct property access: Next.js only inlines public
    // variables when their names are statically analyzable.
    return process.env.NEXT_PUBLIC_APDL_URL;
  } catch {
    // `process` is absent in unbundled browser and IIFE environments.
    return undefined;
  }
}

function nextPublicClientKey(): unknown {
  try {
    // Keep this as a direct property access for Next.js browser inlining.
    return process.env.NEXT_PUBLIC_APDL_CLIENT_KEY;
  } catch {
    return undefined;
  }
}

function serverEndpoint(): unknown {
  if (typeof process === 'undefined' || process.env == null) {
    return undefined;
  }
  return process.env.APDL_URL;
}

function serverClientKey(): unknown {
  if (typeof process === 'undefined' || process.env == null) {
    return undefined;
  }
  return process.env.APDL_CLIENT_KEY;
}

/** Resolves the ingestion/config endpoint from documented env conventions. */
export function endpointFromEnv(): string | undefined {
  return normalizeEnv(nextPublicEndpoint()) ?? normalizeEnv(serverEndpoint());
}

/** Resolves the client key from documented env conventions. */
export function clientKeyFromEnv(): string | undefined {
  return normalizeEnv(nextPublicClientKey()) ?? normalizeEnv(serverClientKey());
}
