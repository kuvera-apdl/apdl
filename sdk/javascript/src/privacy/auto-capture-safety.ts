import type { TrackEvent } from '../core/types';

const TAG_PATTERN = /^[a-z][a-z0-9-]{0,63}$/;
const MAX_COORDINATE = 100_000;
const MIN_RAGE_CLICK_COUNT = 3;
const MAX_RAGE_CLICK_COUNT = 100;
const SAFE_FORM_METHODS = new Set(['get', 'post', 'dialog']);
const SAFE_INPUT_TAGS = new Set(['input', 'select', 'textarea']);
const SAFE_SCROLL_THRESHOLDS = new Set([25, 50, 75, 100]);
const SAFE_ERROR_TYPES = new Set([
  'javascript_error',
  'unhandled_rejection',
  'component_render_error',
]);
const SAFE_WEB_VITAL_METRICS = new Set(['CLS', 'INP', 'LCP']);
const SAFE_WEB_VITAL_RATINGS = new Set([
  'good',
  'needs_improvement',
  'poor',
]);
const INPUT_TYPE_PATTERN = /^[a-z][a-z0-9-]{0,31}$/;

function isCanonicalTag(value: unknown): value is string {
  return typeof value === 'string' && TAG_PATTERN.test(value);
}

function isCanonicalCoordinate(value: unknown): value is number {
  return typeof value === 'number'
    && Number.isFinite(value)
    && value >= 0
    && value <= MAX_COORDINATE;
}

function isCanonicalClickCount(value: unknown): value is number {
  return typeof value === 'number'
    && Number.isInteger(value)
    && value >= MIN_RAGE_CLICK_COUNT
    && value <= MAX_RAGE_CLICK_COUNT;
}

function sanitizeContext(
  context: TrackEvent['context'],
  includePage: boolean
): TrackEvent['context'] {
  const source = context as unknown as Record<string, unknown>;
  const sanitized: Record<string, unknown> = {};

  for (const key of Object.keys(source)) {
    if (key !== 'page' && key !== 'referrer') {
      sanitized[key] = source[key];
    }
  }

  if (includePage && isRecord(source.page)) {
    const safeUrl = sanitizeHttpUrl(source.page.url);
    const safePath = sanitizePath(source.page.path)
      ?? pathFromSanitizedUrl(safeUrl)
      ?? '';
    sanitized.page = {
      url: safeUrl ?? '',
      title: '',
      path: safePath,
      search: '',
    };
  }

  return sanitized as unknown as TrackEvent['context'];
}

/**
 * Enforces non-configurable safety rules for browser context and every
 * reserved auto-capture event.
 *
 * This runs outside the user-configurable scrubber pipeline so sensitive DOM
 * sensitive URL, DOM, and form metadata cannot be retained by disabling or
 * replacing privacy scrubbers.
 */
export function sanitizeAutoCaptureEvent(event: TrackEvent): TrackEvent {
  const context = sanitizeContext(
    event.context,
    event.event !== '$click' && event.event !== '$rage_click'
  );
  const properties = sanitizeReservedProperties(event);

  return {
    ...event,
    ...(properties === event.properties ? {} : { properties }),
    context,
  };
}

function sanitizeReservedProperties(
  event: TrackEvent
): Record<string, unknown> | undefined {
  if (event.event === '$click' || event.event === '$rage_click') {
    return sanitizeClickProperties(event);
  }
  if (event.event === 'page') {
    return sanitizePageProperties(event.properties);
  }
  if (event.event === '$form_submit') {
    return sanitizeFormProperties(event.properties);
  }
  if (event.event === '$input_change') {
    return sanitizeInputProperties(event.properties);
  }
  if (event.event === '$scroll_depth') {
    return sanitizeScrollProperties(event.properties);
  }
  if (event.event === '$frontend_error') {
    return sanitizeFrontendErrorProperties(event.properties);
  }
  if (event.event === '$web_vital') {
    return sanitizeWebVitalProperties(event.properties);
  }
  return event.properties;
}

function sanitizeClickProperties(event: TrackEvent): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const sourceProperties = event.properties;
  const tag = sourceProperties?.tag;
  const x = sourceProperties?.x;
  const y = sourceProperties?.y;
  const clickCount = event.event === '$rage_click'
    ? sourceProperties?.clickCount
    : undefined;

  if (isCanonicalTag(tag)) {
    properties.tag = tag;
  }
  if (isCanonicalCoordinate(x)) {
    properties.x = x;
  }
  if (isCanonicalCoordinate(y)) {
    properties.y = y;
  }
  if (event.event === '$rage_click' && isCanonicalClickCount(clickCount)) {
    properties.clickCount = clickCount;
  }

  return properties;
}

function sanitizePageProperties(
  source: Record<string, unknown> | undefined
): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const url = sanitizeHttpUrl(source?.url);
  const path = sanitizePath(source?.path) ?? pathFromSanitizedUrl(url);
  if (url !== null) properties.url = url;
  if (path !== null) properties.path = path;
  if (isBoundedString(source?.name, 256)) properties.name = source.name;
  return properties;
}

function sanitizeFormProperties(
  source: Record<string, unknown> | undefined
): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  if (typeof source?.formMethod === 'string') {
    const method = source.formMethod.toLowerCase();
    if (SAFE_FORM_METHODS.has(method)) properties.formMethod = method;
  }
  return properties;
}

function sanitizeInputProperties(
  source: Record<string, unknown> | undefined
): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  if (typeof source?.tag === 'string' && SAFE_INPUT_TAGS.has(source.tag)) {
    properties.tag = source.tag;
  }
  if (
    typeof source?.inputType === 'string'
    && INPUT_TYPE_PATTERN.test(source.inputType)
  ) {
    properties.inputType = source.inputType;
  }
  if (typeof source?.hasValue === 'boolean') {
    properties.hasValue = source.hasValue;
  }
  return properties;
}

function sanitizeScrollProperties(
  source: Record<string, unknown> | undefined
): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  if (
    typeof source?.threshold === 'number'
    && SAFE_SCROLL_THRESHOLDS.has(source.threshold)
  ) {
    properties.threshold = source.threshold;
  }
  if (
    typeof source?.percent === 'number'
    && Number.isInteger(source.percent)
    && source.percent >= 0
    && source.percent <= 100
  ) {
    properties.percent = source.percent;
  }
  return properties;
}

function sanitizeFrontendErrorProperties(
  source: Record<string, unknown> | undefined
): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  copyEnum(properties, source, 'error_type', SAFE_ERROR_TYPES);
  copyBoundedString(properties, source, 'message', 1024, true);
  copySanitizedPath(properties, source, 'page');
  copyBoundedString(properties, source, 'component', 256);
  copyBoundedString(properties, source, 'slot_id', 256);
  copySanitizedUrlLike(properties, source, 'source');
  copyNullableFiniteNumber(properties, source, 'line');
  copyNullableFiniteNumber(properties, source, 'column');
  copyBoundedString(properties, source, 'stack', 4096, true);
  copyStringRecord(properties, source, 'active_flags');
  copyPositiveIntegerRecord(properties, source, 'active_flag_versions');
  return properties;
}

function sanitizeWebVitalProperties(
  source: Record<string, unknown> | undefined
): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  copyEnum(properties, source, 'metric', SAFE_WEB_VITAL_METRICS);
  copyFiniteNumber(properties, source, 'value');
  copyEnum(properties, source, 'rating', SAFE_WEB_VITAL_RATINGS);
  copyFiniteNumber(properties, source, 'delta');
  copyBoundedString(properties, source, 'id', 256);
  copyBoundedString(properties, source, 'navigation_type', 128);
  copySanitizedPath(properties, source, 'page');
  copyStringRecord(properties, source, 'active_flags');
  copyPositiveIntegerRecord(properties, source, 'active_flag_versions');
  return properties;
}

function sanitizeHttpUrl(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 4096) return null;
  try {
    const parsed = new URL(value);
    if (
      (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
      || parsed.username !== ''
      || parsed.password !== ''
    ) {
      return null;
    }
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return null;
  }
}

function sanitizePath(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 2048) return null;
  const queryIndex = value.indexOf('?');
  const fragmentIndex = value.indexOf('#');
  const indexes = [queryIndex, fragmentIndex].filter((index) => index >= 0);
  const end = indexes.length === 0 ? value.length : Math.min(...indexes);
  const path = value.slice(0, end);
  return path.startsWith('/') ? path : null;
}

function pathFromSanitizedUrl(value: string | null): string | null {
  if (value === null) return null;
  try {
    return new URL(value).pathname;
  } catch {
    return null;
  }
}

function stripUrlsFromText(value: string): string {
  return value.replace(/https?:\/\/[^\s)\]}]+/gi, (url) => {
    return sanitizeHttpUrl(url) ?? '[REDACTED_URL]';
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isBoundedString(value: unknown, maxLength: number): value is string {
  return typeof value === 'string' && value.length <= maxLength;
}

function copyBoundedString(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string,
  maxLength: number,
  stripUrls = false
): void {
  const value = source?.[key];
  if (isBoundedString(value, maxLength)) {
    target[key] = stripUrls ? stripUrlsFromText(value) : value;
  }
}

function copyEnum(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string,
  allowed: ReadonlySet<string>
): void {
  const value = source?.[key];
  if (typeof value === 'string' && allowed.has(value)) target[key] = value;
}

function copyFiniteNumber(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string
): void {
  const value = source?.[key];
  if (typeof value === 'number' && Number.isFinite(value)) target[key] = value;
}

function copyNullableFiniteNumber(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string
): void {
  const value = source?.[key];
  if (value === null || (typeof value === 'number' && Number.isFinite(value))) {
    target[key] = value;
  }
}

function copySanitizedPath(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string
): void {
  const value = sanitizePath(source?.[key]);
  if (value !== null) target[key] = value;
}

function copySanitizedUrlLike(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string
): void {
  const value = source?.[key];
  if (!isBoundedString(value, 4096)) return;
  const separatorIndexes = [value.indexOf('?'), value.indexOf('#')]
    .filter((index) => index >= 0);
  const end = separatorIndexes.length === 0
    ? value.length
    : Math.min(...separatorIndexes);
  target[key] = sanitizeHttpUrl(value) ?? value.slice(0, end);
}

function copyStringRecord(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string
): void {
  const value = source?.[key];
  if (
    isRecord(value)
    && Object.values(value).every((item) => isBoundedString(item, 256))
  ) {
    target[key] = { ...value };
  }
}

function copyPositiveIntegerRecord(
  target: Record<string, unknown>,
  source: Record<string, unknown> | undefined,
  key: string
): void {
  const value = source?.[key];
  if (
    isRecord(value)
    && Object.values(value).every(
      (item) => typeof item === 'number' && Number.isInteger(item) && item >= 1
    )
  ) {
    target[key] = { ...value };
  }
}
