import {
  createContext,
  createElement,
  useContext,
  useRef,
  type ReactElement,
  type ReactNode,
} from 'react';
// Import the public surface from the package entry so this adapter bundle
// references the core SDK as an external rather than re-bundling a copy of it.
import {
  type APDLApi,
  type AutoCaptureConfig,
  type ConsentState,
  init,
  NoopClient,
  type PartialAPDLConfig,
  type PersistenceMode,
  type PrivacyMode,
} from '@apdl-oss/sdk';

const noopClient: APDLApi = new NoopClient();

/**
 * Props for {@link APDLProvider}.
 *
 * Mirrors the SDK config but flattens `auth.clientKey` to a top-level
 * `clientKey` prop. Any omitted field falls back to env conventions
 * (`NEXT_PUBLIC_APDL_URL` / `NEXT_PUBLIC_APDL_CLIENT_KEY`), so
 * `<APDLProvider autoCapture>{children}</APDLProvider>` is a complete setup.
 */
export interface APDLProviderProps {
  endpoint?: string;
  clientKey?: string;
  autoCapture?: boolean | AutoCaptureConfig;
  batchSize?: number;
  flushInterval?: number;
  privacyMode?: PrivacyMode;
  consent?: ConsentState;
  persistence?: PersistenceMode;
  maxQueueSize?: number;
  debug?: boolean;
  /**
   * A pre-built client to use instead of creating one from the props above.
   * Useful for tests and for sharing a client created elsewhere.
   */
  client?: APDLApi;
  children?: ReactNode;
}

const APDLContext = createContext<APDLApi>(noopClient);

function toConfig(props: APDLProviderProps): PartialAPDLConfig {
  const config: PartialAPDLConfig = {};

  if (props.endpoint !== undefined) config.endpoint = props.endpoint;
  if (props.clientKey !== undefined) config.auth = { clientKey: props.clientKey };
  if (props.autoCapture !== undefined) config.autoCapture = props.autoCapture;
  if (props.batchSize !== undefined) config.batchSize = props.batchSize;
  if (props.flushInterval !== undefined) config.flushInterval = props.flushInterval;
  if (props.privacyMode !== undefined) config.privacyMode = props.privacyMode;
  if (props.consent !== undefined) config.consent = props.consent;
  if (props.persistence !== undefined) config.persistence = props.persistence;
  if (props.maxQueueSize !== undefined) config.maxQueueSize = props.maxQueueSize;
  if (props.debug !== undefined) config.debug = props.debug;

  return config;
}

/**
 * Provides an APDL client to the React tree.
 *
 * Owns the `'use client'` boundary, the singleton lifecycle, and SSR safety
 * internally, so consumers no longer hand-write an init wrapper. Drop it once
 * in the root layout:
 *
 * ```tsx
 * import { APDLProvider } from '@apdl-oss/sdk/react';
 *
 * export default function RootLayout({ children }) {
 *   return <APDLProvider autoCapture>{children}</APDLProvider>;
 * }
 * ```
 *
 * The underlying `init()` is an idempotent singleton, so React StrictMode's
 * double-invoke and HMR re-runs reuse the same client rather than leaking
 * duplicate listeners, SSE connections, or flush loops.
 */
export function APDLProvider(props: APDLProviderProps): ReactElement {
  // Lazily resolve the client once. `init()` is SSR-safe (returns a no-op
  // client on the server) and an idempotent singleton in the browser, so this
  // is safe to evaluate during render under StrictMode.
  const clientRef = useRef<APDLApi | null>(null);
  if (clientRef.current === null) {
    clientRef.current = props.client ?? init(toConfig(props));
  }

  return createElement(APDLContext.Provider, { value: clientRef.current }, props.children);
}

/**
 * Returns the APDL client from the nearest {@link APDLProvider}.
 *
 * ```tsx
 * const apdl = useAPDL();
 * apdl.track('cta_clicked', { id: 'hero' });
 * const variant = apdl.getVariant('new-checkout-flow');
 * ```
 *
 * Outside a provider it returns an inert no-op client, so calls never throw.
 */
export function useAPDL(): APDLApi {
  return useContext(APDLContext);
}

export type { APDLApi } from '@apdl-oss/sdk';
