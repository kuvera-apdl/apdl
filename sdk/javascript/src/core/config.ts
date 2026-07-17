import { clientKeyFromEnv, endpointFromEnv } from './env';

export type PrivacyMode = 'standard' | 'cookieless';
export type PersistenceMode = 'localStorage' | 'memory';

export interface APDLConfig {
  endpoint: string;
  auth: APDLAuthConfig;
  autoCapture?: boolean | AutoCaptureConfig;
  batchSize?: number;
  flushInterval?: number;
  privacyMode?: PrivacyMode;
  consent?: ConsentState;
  persistence?: PersistenceMode;
  maxQueueSize?: number;
  debug?: boolean;
}

/**
 * A loosened config accepted by `init()`: every field is optional because
 * `endpoint` and `auth.clientKey` can be supplied via env conventions, and
 * missing credentials are tolerated in fail-soft (non-strict) mode.
 */
export type PartialAPDLConfig = Partial<Omit<APDLConfig, 'auth'>> & {
  auth?: Partial<APDLAuthConfig>;
};

export interface ResolveConfigOptions {
  /**
   * When `true` (the default), missing `endpoint` / `auth.clientKey` throw.
   * When `false`, they resolve to `null` so callers can fall back to a no-op
   * client instead of crashing. Malformed values still throw in both modes.
   */
  strict?: boolean;
}

export interface APDLAuthConfig {
  clientKey: string;
}

export interface AutoCaptureConfig {
  pageViews?: boolean;
  clicks?: boolean;
  formSubmissions?: boolean;
  inputChanges?: boolean;
  scrollDepth?: boolean;
  rage_clicks?: boolean;
  frontend_errors?: boolean;
  web_vitals?: boolean;
}

export interface ConsentState {
  analytics: boolean;
  personalization: boolean;
  experiments: boolean;
}

export interface ResolvedConfig {
  projectId: string;
  endpoint: string;
  auth: APDLAuthConfig;
  autoCapture: AutoCaptureConfig;
  batchSize: number;
  flushInterval: number;
  privacyMode: PrivacyMode;
  consent: ConsentState;
  persistence: PersistenceMode;
  maxQueueSize: number;
  debug: boolean;
}

const DEFAULT_BATCH_SIZE = 20;
const MAX_BATCH_SIZE = 100;
const DEFAULT_FLUSH_INTERVAL = 3000;
const MIN_FLUSH_INTERVAL = 100;
const MAX_FLUSH_INTERVAL = 3_600_000;
const DEFAULT_MAX_QUEUE_SIZE = 1000;
const MAX_QUEUE_SIZE = 100_000;
const CLIENT_KEY_PATTERN = /^client_([a-zA-Z0-9]{1,64})_([a-zA-Z0-9]{16,128})$/;
const SUPPORTED_PRIVACY_MODES = new Set<PrivacyMode>([
  'standard',
  'cookieless',
]);
const SUPPORTED_PERSISTENCE_MODES = new Set<PersistenceMode>([
  'localStorage',
  'memory',
]);

const SUPPORTED_CONFIG_FIELDS = new Set([
  'endpoint',
  'auth',
  'autoCapture',
  'batchSize',
  'flushInterval',
  'privacyMode',
  'consent',
  'persistence',
  'maxQueueSize',
  'debug',
]);

const SUPPORTED_AUTO_CAPTURE_FIELDS: Array<keyof AutoCaptureConfig> = [
  'pageViews',
  'clicks',
  'formSubmissions',
  'inputChanges',
  'scrollDepth',
  'rage_clicks',
  'frontend_errors',
  'web_vitals',
];

const SUPPORTED_CONSENT_FIELDS: Array<keyof ConsentState> = [
  'analytics',
  'personalization',
  'experiments',
];

const REMOVED_CONFIG_FIELDS: Record<string, string> = {
  apiKey: 'auth.clientKey',
  host: 'endpoint',
  configHost: 'endpoint',
  endpoints: 'endpoint',
  projectId: 'auth.clientKey',
};

const DEFAULT_AUTO_CAPTURE: AutoCaptureConfig = {
  pageViews: true,
  clicks: true,
  formSubmissions: true,
  inputChanges: false,
  scrollDepth: true,
  rage_clicks: true,
  frontend_errors: true,
  web_vitals: true,
};

const DEFAULT_CONSENT: ConsentState = {
  analytics: true,
  personalization: true,
  experiments: true,
};

export function resolveConfig(config: APDLConfig): ResolvedConfig;
export function resolveConfig(
  config: PartialAPDLConfig,
  options: { strict: true }
): ResolvedConfig;
export function resolveConfig(
  config: PartialAPDLConfig,
  options: { strict?: false }
): ResolvedConfig | null;
export function resolveConfig(
  config: PartialAPDLConfig,
  options?: ResolveConfigOptions
): ResolvedConfig | null {
  const strict = options?.strict !== false;
  const configInput = assertObject(config, 'config');
  rejectUnsupportedTopLevelFields(configInput);

  let authInput: Record<string, unknown> | undefined;
  if (config.auth !== undefined) {
    authInput = assertObject(config.auth, 'auth');
    assertSupportedNestedFields(authInput, ['clientKey'], 'auth');
  }

  const endpointInput = Object.prototype.hasOwnProperty.call(configInput, 'endpoint')
    ? configInput.endpoint
    : endpointFromEnv();
  const clientKeyInput = authInput
    && Object.prototype.hasOwnProperty.call(authInput, 'clientKey')
    ? authInput.clientKey
    : clientKeyFromEnv();

  // Validate every present value before applying fail-soft missing-credential
  // behavior. JavaScript callers and parsed JSON must never hide malformed
  // explicit values behind environment fallbacks.
  const endpoint = endpointInput === undefined
    ? undefined
    : requireHttpOrigin(endpointInput, 'endpoint');
  const clientKey = clientKeyInput === undefined
    ? undefined
    : requireNonEmptyString(clientKeyInput, 'auth.clientKey');

  if (endpoint === undefined || clientKey === undefined) {
    if (!strict) {
      return null;
    }

    if (endpoint === undefined) {
      requireNonEmptyString(undefined, 'endpoint');
    }
    if (clientKey === undefined) {
      requireNonEmptyString(undefined, 'auth.clientKey');
    }
    throw new Error('APDL: endpoint and auth.clientKey are required');
  }

  const keyMatch = CLIENT_KEY_PATTERN.exec(clientKey);
  if (!keyMatch) {
    throw new Error(
      'APDL: auth.clientKey must match format client_{project_id}_{token}'
    );
  }
  const projectId = keyMatch[1];

  const autoCapture = resolveAutoCapture(config.autoCapture);

  const batchSize = config.batchSize === undefined
    ? DEFAULT_BATCH_SIZE
    : requireIntegerInRange(config.batchSize, 'batchSize', 1, MAX_BATCH_SIZE);
  const flushInterval = config.flushInterval === undefined
    ? DEFAULT_FLUSH_INTERVAL
    : requireIntegerInRange(
        config.flushInterval,
        'flushInterval',
        MIN_FLUSH_INTERVAL,
        MAX_FLUSH_INTERVAL
      );
  const maxQueueSize = config.maxQueueSize === undefined
    ? DEFAULT_MAX_QUEUE_SIZE
    : requireIntegerInRange(
        config.maxQueueSize,
        'maxQueueSize',
        1,
        MAX_QUEUE_SIZE
      );
  const consent = resolveConsent(config.consent);
  const privacyMode = config.privacyMode === undefined
    ? 'standard'
    : requireEnum(
        config.privacyMode,
        'privacyMode',
        SUPPORTED_PRIVACY_MODES
      );
  const persistence = config.persistence === undefined
    ? 'localStorage'
    : requireEnum(
        config.persistence,
        'persistence',
        SUPPORTED_PERSISTENCE_MODES
      );
  const debug = config.debug === undefined
    ? false
    : requireBoolean(config.debug, 'debug');

  return {
    projectId,
    endpoint,
    auth: {
      clientKey,
    },
    autoCapture,
    batchSize,
    flushInterval,
    privacyMode,
    consent,
    persistence,
    maxQueueSize,
    debug,
  };
}

function resolveAutoCapture(value: unknown): AutoCaptureConfig {
  if (value === false) {
    return {
      pageViews: false,
      clicks: false,
      formSubmissions: false,
      inputChanges: false,
      scrollDepth: false,
      rage_clicks: false,
      frontend_errors: false,
      web_vitals: false,
    };
  }

  if (value === true || value === undefined) {
    return { ...DEFAULT_AUTO_CAPTURE };
  }

  const input = assertObject(value, 'autoCapture');
  assertSupportedNestedFields(input, SUPPORTED_AUTO_CAPTURE_FIELDS, 'autoCapture');

  const autoCapture = { ...DEFAULT_AUTO_CAPTURE };
  for (const field of SUPPORTED_AUTO_CAPTURE_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(input, field)) {
      continue;
    }
    autoCapture[field] = requireBoolean(input[field], `autoCapture.${field}`);
  }

  return autoCapture;
}

function resolveConsent(value: unknown): ConsentState {
  if (value === undefined) {
    return { ...DEFAULT_CONSENT };
  }

  const input = assertObject(value, 'consent');
  assertSupportedNestedFields(input, SUPPORTED_CONSENT_FIELDS, 'consent');

  return {
    analytics: requireBoolean(input.analytics, 'consent.analytics'),
    personalization: requireBoolean(
      input.personalization,
      'consent.personalization'
    ),
    experiments: requireBoolean(input.experiments, 'consent.experiments'),
  };
}

function assertObject(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`APDL: ${name} is required and must be an object`);
  }

  return value as Record<string, unknown>;
}

function rejectUnsupportedTopLevelFields(config: Record<string, unknown>): void {
  for (const field of Object.keys(config)) {
    const replacement = REMOVED_CONFIG_FIELDS[field];
    if (replacement) {
      throw new Error(
        `APDL: config.${field} is no longer supported; use ${replacement}`
      );
    }

    if (!SUPPORTED_CONFIG_FIELDS.has(field)) {
      throw new Error(`APDL: config.${field} is not supported`);
    }
  }
}

function assertSupportedNestedFields(
  config: Record<string, unknown>,
  supportedFields: readonly string[],
  path: string
): void {
  const supported = new Set(supportedFields);
  for (const field of Object.keys(config)) {
    if (!supported.has(field)) {
      throw new Error(`APDL: ${path}.${field} is not supported`);
    }
  }
}

function requireNonEmptyString(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(
      `APDL: ${path} is required and must be a non-empty string`
    );
  }

  return value;
}

function requireHttpOrigin(value: unknown, path: string): string {
  const raw = requireNonEmptyString(value, path);
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error(`APDL: ${path} must be an absolute HTTP(S) origin`);
  }

  if (
    (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
    || parsed.username !== ''
    || parsed.password !== ''
    || parsed.pathname !== '/'
    || parsed.search !== ''
    || parsed.hash !== ''
  ) {
    throw new Error(`APDL: ${path} must be an absolute HTTP(S) origin`);
  }

  return parsed.origin;
}

function requireIntegerInRange(
  value: unknown,
  path: string,
  min: number,
  max: number
): number {
  if (
    typeof value !== 'number'
    || !Number.isFinite(value)
    || !Number.isInteger(value)
    || value < min
    || value > max
  ) {
    throw new Error(
      `APDL: ${path} must be an integer between ${min} and ${max}`
    );
  }

  return value;
}

function requireEnum<T extends string>(
  value: unknown,
  path: string,
  supported: ReadonlySet<T>
): T {
  if (typeof value !== 'string' || !supported.has(value as T)) {
    throw new Error(
      `APDL: ${path} must be one of: ${Array.from(supported).join(', ')}`
    );
  }

  return value as T;
}

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new Error(`APDL: ${path} is required and must be a boolean`);
  }

  return value;
}
