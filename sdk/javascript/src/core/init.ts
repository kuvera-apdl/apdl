import type { APDLApi } from './api';
import { APDLClient } from './client';
import { type PartialAPDLConfig, resolveConfig } from './config';
import { noopClient } from './noop-client';

const REGISTRY_KEY = '__APDL_SINGLETONS__';

type Registry = Map<string, APDLClient>;

function registry(): Registry {
  const scope = globalThis as Record<string, unknown>;
  let store = scope[REGISTRY_KEY] as Registry | undefined;
  if (!store) {
    store = new Map();
    scope[REGISTRY_KEY] = store;
  }
  return store;
}

function isBrowser(): boolean {
  return typeof window !== 'undefined' && typeof document !== 'undefined';
}

let warnedMissingConfig = false;

function warnMissingConfig(): void {
  if (warnedMissingConfig) {
    return;
  }
  warnedMissingConfig = true;
  console.warn(
    '[APDL] init skipped: no endpoint/clientKey provided and none found in the ' +
      'environment (NEXT_PUBLIC_APDL_URL / NEXT_PUBLIC_APDL_CLIENT_KEY). ' +
      'Returning a no-op client.'
  );
}

/**
 * Initializes the APDL SDK and returns a client.
 *
 * Behavior that lets consumers drop their own wrapper boilerplate:
 *
 * - **Env defaults (item 5):** `endpoint` / `auth.clientKey` fall back to the
 *   documented env conventions, so `init()` can be called with no arguments.
 * - **Fail-soft (item 4):** when credentials are absent, it warns once and
 *   returns an inert no-op client instead of throwing, so an unset env var does
 *   not crash every route. Malformed values still throw.
 * - **SSR-safe (item 3):** on the server (no `window`) it returns the no-op
 *   client without opening sockets, timers, or fetches.
 * - **Idempotent singleton (item 2):** repeated calls with the same client key
 *   return the same instance, making it immune to React StrictMode double-invoke
 *   and HMR re-runs. The instance is evicted from the registry on `shutdown()`.
 */
export function init(config: PartialAPDLConfig = {}): APDLApi {
  const resolved = resolveConfig(config, { strict: false });
  if (resolved === null) {
    warnMissingConfig();
    return noopClient;
  }

  if (!isBrowser()) {
    return noopClient;
  }

  const store = registry();
  const key = resolved.auth.clientKey;
  const existing = store.get(key);
  if (existing) {
    return existing;
  }

  const client = new APDLClient(config);
  store.set(key, client);

  // Evict on shutdown so a later init() with the same key starts fresh.
  const baseShutdown = client.shutdown.bind(client);
  client.shutdown = async (): Promise<void> => {
    if (store.get(key) === client) {
      store.delete(key);
    }
    await baseShutdown();
  };

  return client;
}

/** APDL namespace — primary entry point. Use `APDL.init(config)`. */
export const APDL = { init };

// ── Lazy module-scope singleton ────────────────────────────────────

let lazyClient: APDLApi | null = null;

function ensureClient(): APDLApi {
  if (lazyClient === null) {
    lazyClient = init();
  }
  return lazyClient;
}

/**
 * A module-scope client that just works (item 3): import and use it directly,
 * no `'use client'` / `useEffect` needed. It no-ops on the server and auto-starts
 * on first use in the browser, reading config from env conventions.
 *
 * ```ts
 * import { apdl } from '@apdl-oss/sdk';
 * apdl.track('cta_clicked', { id: 'hero' });
 * ```
 */
export const apdl: APDLApi = {
  track: (event, properties) => ensureClient().track(event, properties),
  identify: (userId, traits) => ensureClient().identify(userId, traits),
  group: (groupId, traits) => ensureClient().group(groupId, traits),
  page: (name, properties) => ensureClient().page(name, properties),
  reset: () => ensureClient().reset(),
  getVariant: (key, options) => ensureClient().getVariant(key, options),
  getVariantDetails: (key, options) => ensureClient().getVariantDetails(key, options),
  onVariantChange: (key, callback) => ensureClient().onVariantChange(key, callback),
  shutdown: () => ensureClient().shutdown(),
  get ui() {
    return ensureClient().ui;
  },
  get consent() {
    return ensureClient().consent;
  },
  get privacy() {
    return ensureClient().privacy;
  },
  get experiments() {
    return ensureClient().experiments;
  },
  get debug() {
    return ensureClient().debug;
  },
};

/**
 * Auto-start on the first browser tick when env config is present, so merely
 * importing the package wires up auto-capture. A no-op when config is absent
 * (the lazy client stays inert) or on the server.
 */
export function maybeAutoStart(): void {
  if (!isBrowser()) {
    return;
  }
  const resolved = resolveConfig({}, { strict: false });
  if (resolved === null) {
    return;
  }
  const schedule =
    typeof queueMicrotask === 'function'
      ? queueMicrotask
      : (cb: () => void) => setTimeout(cb, 0);
  schedule(() => {
    ensureClient();
  });
}
