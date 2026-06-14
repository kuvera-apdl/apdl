export interface APDLConfig {
  endpoint: string;
  auth: APDLAuthConfig;
  autoCapture?: boolean | AutoCaptureConfig;
  batchSize?: number;
  flushInterval?: number;
  privacyMode?: 'standard' | 'cookieless' | 'strict';
  consent?: ConsentState;
  persistence?: 'localStorage' | 'cookie' | 'memory';
  maxQueueSize?: number;
  debug?: boolean;
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
  privacyMode: 'standard' | 'cookieless' | 'strict';
  consent: ConsentState;
  persistence: 'localStorage' | 'cookie' | 'memory';
  maxQueueSize: number;
  debug: boolean;
}

const DEFAULT_BATCH_SIZE = 20;
const MAX_BATCH_SIZE = 100;
const DEFAULT_FLUSH_INTERVAL = 3000;
const DEFAULT_MAX_QUEUE_SIZE = 1000;
const CLIENT_KEY_PATTERN = /^proj_([a-zA-Z0-9]{1,64})_([a-zA-Z0-9]{16,})$/;

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

export function resolveConfig(config: APDLConfig): ResolvedConfig {
  rejectUnsupportedTopLevelFields(assertObject(config, 'config'));

  // Single front-door URL: a gateway routes /v1/events to the ingestion service
  // and /v1/flags + /v1/stream to the config service behind one origin. Trailing
  // slashes are stripped so `${endpoint}/v1/...` never double-slashes.
  const endpoint = requireNonEmptyString(config.endpoint, 'endpoint').replace(
    /\/+$/,
    ''
  );

  const auth = assertObject(config.auth, 'auth');
  assertSupportedNestedFields(auth, ['clientKey'], 'auth');
  const clientKey = requireNonEmptyString(auth.clientKey, 'auth.clientKey');
  const keyMatch = CLIENT_KEY_PATTERN.exec(clientKey);
  if (!keyMatch) {
    throw new Error(
      'APDL: auth.clientKey must match format proj_{project_id}_{secret}'
    );
  }
  const projectId = keyMatch[1];

  const autoCapture = resolveAutoCapture(config.autoCapture);

  let batchSize = config.batchSize ?? DEFAULT_BATCH_SIZE;
  if (batchSize < 1) batchSize = 1;
  if (batchSize > MAX_BATCH_SIZE) batchSize = MAX_BATCH_SIZE;

  const flushInterval = config.flushInterval ?? DEFAULT_FLUSH_INTERVAL;
  const maxQueueSize = config.maxQueueSize ?? DEFAULT_MAX_QUEUE_SIZE;
  const consent = resolveConsent(config.consent);

  return {
    projectId,
    endpoint,
    auth: {
      clientKey,
    },
    autoCapture,
    batchSize,
    flushInterval,
    privacyMode: config.privacyMode ?? 'standard',
    consent,
    persistence: config.persistence ?? 'localStorage',
    maxQueueSize,
    debug: config.debug ?? false,
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

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new Error(`APDL: ${path} is required and must be a boolean`);
  }

  return value;
}
