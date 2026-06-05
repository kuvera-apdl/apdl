export interface APDLConfig {
  apiKey: string;
  host?: string;
  configHost?: string;
  autoCapture?: boolean | AutoCaptureConfig;
  batchSize?: number;
  flushInterval?: number;
  privacyMode?: 'standard' | 'cookieless' | 'strict';
  consent?: ConsentState;
  persistence?: 'localStorage' | 'cookie' | 'memory';
  maxQueueSize?: number;
  debug?: boolean;
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
  apiKey: string;
  host: string;
  configHost: string;
  autoCapture: AutoCaptureConfig;
  batchSize: number;
  flushInterval: number;
  privacyMode: 'standard' | 'cookieless' | 'strict';
  consent: ConsentState;
  persistence: 'localStorage' | 'cookie' | 'memory';
  maxQueueSize: number;
  debug: boolean;
}

const DEFAULT_HOST = 'https://ingest.apdl.dev';
const DEFAULT_CONFIG_HOST = 'https://config.apdl.dev';
const DEFAULT_BATCH_SIZE = 20;
const MAX_BATCH_SIZE = 100;
const DEFAULT_FLUSH_INTERVAL = 3000;
const DEFAULT_MAX_QUEUE_SIZE = 1000;

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
  if (!config.apiKey || typeof config.apiKey !== 'string') {
    throw new Error('APDL: apiKey is required and must be a non-empty string');
  }

  let autoCapture: AutoCaptureConfig;
  if (config.autoCapture === false) {
    autoCapture = {
      pageViews: false,
      clicks: false,
      formSubmissions: false,
      inputChanges: false,
      scrollDepth: false,
      rage_clicks: false,
      frontend_errors: false,
      web_vitals: false,
    };
  } else if (config.autoCapture === true || config.autoCapture === undefined) {
    autoCapture = { ...DEFAULT_AUTO_CAPTURE };
  } else {
    autoCapture = {
      ...DEFAULT_AUTO_CAPTURE,
      ...config.autoCapture,
    };
  }

  let batchSize = config.batchSize ?? DEFAULT_BATCH_SIZE;
  if (batchSize < 1) batchSize = 1;
  if (batchSize > MAX_BATCH_SIZE) batchSize = MAX_BATCH_SIZE;

  const flushInterval = config.flushInterval ?? DEFAULT_FLUSH_INTERVAL;
  const maxQueueSize = config.maxQueueSize ?? DEFAULT_MAX_QUEUE_SIZE;

  return {
    apiKey: config.apiKey,
    host: config.host ?? DEFAULT_HOST,
    configHost: config.configHost ?? DEFAULT_CONFIG_HOST,
    autoCapture,
    batchSize,
    flushInterval,
    privacyMode: config.privacyMode ?? 'standard',
    consent: config.consent ? { ...config.consent } : { ...DEFAULT_CONSENT },
    persistence: config.persistence ?? 'localStorage',
    maxQueueSize,
    debug: config.debug ?? false,
  };
}
