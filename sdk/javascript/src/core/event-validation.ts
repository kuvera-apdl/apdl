import type { TrackEvent } from './types';

export const MAX_JSON_DEPTH = 10;
export const MAX_JSON_CONTAINER_ENTRIES = 100;
export const MAX_JSON_NODES_PER_EVENT = 1_000;
export const MAX_SERIALIZED_EVENT_BYTES = 64 * 1024;
export const MAX_SERIALIZED_REQUEST_BYTES = 512 * 1024;
// Browsers share an implementation-defined keepalive quota across outstanding
// requests (commonly 64 KiB). Keep unload payloads below that ceiling.
export const MAX_KEEPALIVE_REQUEST_BYTES = 48 * 1024;
export const MAX_EVENTS_PER_BATCH = 100;
export const MAX_EVENT_AGE_MS = 7 * 24 * 60 * 60 * 1000;
export const MAX_EVENT_FUTURE_SKEW_MS = 5 * 60 * 1000;

const TRACK_EVENT_FIELDS = new Set([
  'type',
  'event',
  'userId',
  'anonymousId',
  'groupId',
  'properties',
  'traits',
  'context',
  'timestamp',
  'messageId',
  'sessionId',
]);
const EVENT_TYPES = new Set(['track', 'identify', 'group', 'page']);
const CONTEXT_FIELDS = new Set([
  'library',
  'browser',
  'os',
  'device',
  'screen',
  'viewport',
  'page',
  'locale',
  'timezone',
  'referrer',
]);
const OPTIONAL_TRACK_EVENT_FIELDS = new Set([
  'event',
  'userId',
  'groupId',
  'properties',
  'traits',
]);
const RFC3339_UTC_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$/;

type JsonPrimitive = null | boolean | number | string;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

interface ValidationState {
  nodes: number;
  ancestors: WeakSet<object>;
}

/**
 * Validates and clones one SDK event into the canonical JSON value subset.
 *
 * Optional SDK envelope fields whose value is `undefined` are omitted before
 * validation. Undefined values anywhere inside properties, traits, context,
 * or arrays are rejected because their JSON encoding is lossy.
 */
export function canonicalizeTrackEvent(value: unknown): TrackEvent {
  const source = readTrackEventObject(value);
  const candidate: Record<string, unknown> = {};

  for (const key of TRACK_EVENT_FIELDS) {
    const descriptor = Object.getOwnPropertyDescriptor(source, key);
    if (descriptor === undefined) continue;
    if (!descriptor.enumerable || !('value' in descriptor)) {
      throw invalid(`event.${key}`, 'accessor and non-enumerable fields are not supported');
    }
    if (descriptor.value === undefined && OPTIONAL_TRACK_EVENT_FIELDS.has(key)) {
      continue;
    }
    candidate[key] = descriptor.value;
  }

  const canonical = cloneJsonValue(candidate, 'event', 0, {
    nodes: 0,
    ancestors: new WeakSet<object>(),
  }) as unknown as TrackEvent;

  validateEnvelope(canonical);
  return canonical;
}

/** Assert that a wire event stays within the server's per-event byte limit. */
export function assertSerializedEventSize(value: unknown): void {
  const bytes = serializedJsonBytes(value);
  if (bytes > MAX_SERIALIZED_EVENT_BYTES) {
    throw invalid(
      'event',
      `serialized size ${bytes} exceeds ${MAX_SERIALIZED_EVENT_BYTES} bytes`
    );
  }
}

/** Return the UTF-8 byte length of a JSON-compatible value. */
export function serializedJsonBytes(value: unknown): number {
  let serialized: string | undefined;
  try {
    serialized = JSON.stringify(value);
  } catch {
    throw invalid('event', 'value cannot be serialized as JSON');
  }
  if (serialized === undefined) {
    throw invalid('event', 'value cannot be serialized as JSON');
  }
  return utf8ByteLength(serialized);
}

function readTrackEventObject(value: unknown): Record<string, unknown> {
  if (!isPlainObject(value)) {
    throw invalid('event', 'must be a plain object');
  }

  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string') {
      throw invalid('event', 'symbol keys are not supported');
    }
    if (!TRACK_EVENT_FIELDS.has(key)) {
      throw invalid(`event.${key}`, 'unknown event field');
    }
  }
  return value;
}

function validateEnvelope(event: TrackEvent): void {
  if (!EVENT_TYPES.has(event.type)) {
    throw invalid('event.type', 'must be track, identify, group, or page');
  }

  const eventName = event.event === undefined ? event.type : event.event;
  requireBoundedString(eventName, 'event.event', 256);
  event.event = eventName;

  requireBoundedString(event.anonymousId, 'event.anonymousId', 128);
  requireOptionalBoundedString(event.userId, 'event.userId', 128);
  requireOptionalBoundedString(event.groupId, 'event.groupId', 128);
  requireBoundedString(event.messageId, 'event.messageId', 128);
  requireBoundedString(event.sessionId, 'event.sessionId', 128);

  if (event.type !== 'track' && eventName !== event.type) {
    throw invalid(`event.event`, `type ${event.type} requires event=${event.type}`);
  }
  if (event.type === 'identify' && event.userId === undefined) {
    throw invalid('event.userId', 'identify events require a user ID');
  }
  if (event.type === 'group' && event.groupId === undefined) {
    throw invalid('event.groupId', 'group events require a group ID');
  }
  const eventTime = parseUtcTimestamp(event.timestamp);
  if (eventTime === null) {
    throw invalid('event.timestamp', 'must be a valid RFC3339 UTC timestamp');
  }
  const now = Date.now();
  if (eventTime < now - MAX_EVENT_AGE_MS) {
    throw invalid('event.timestamp', 'must be no more than 7 days old');
  }
  if (eventTime > now + MAX_EVENT_FUTURE_SKEW_MS) {
    throw invalid(
      'event.timestamp',
      'must not be more than 5 minutes in the future'
    );
  }
  if (!isPlainObject(event.context)) {
    throw invalid('event.context', 'must be a plain object');
  }
  validateContext(event.context as unknown as Record<string, unknown>);
  if (event.properties !== undefined && !isPlainObject(event.properties)) {
    throw invalid('event.properties', 'must be a plain object');
  }
  if (event.properties !== undefined) {
    for (const [key, value] of Object.entries(event.properties)) {
      if (key.length > 256) {
        throw invalid('event.properties', 'property keys must be at most 256 characters');
      }
      if (typeof value === 'string' && value.length > 8_192) {
        throw invalid(
          `event.properties.${key}`,
          'top-level string properties must be at most 8192 characters'
        );
      }
    }
  }
  if (event.traits !== undefined && !isPlainObject(event.traits)) {
    throw invalid('event.traits', 'must be a plain object');
  }
}

function validateContext(context: Record<string, unknown>): void {
  rejectUnknownFields(context, CONTEXT_FIELDS, 'event.context');

  for (const field of ['library', 'browser', 'os'] as const) {
    if (context[field] === undefined) continue;
    const value = requireObject(context[field], `event.context.${field}`);
    requireExactFields(value, ['name', 'version'], `event.context.${field}`);
    requireBoundedString(value.name, `event.context.${field}.name`, 128);
    requireBoundedString(value.version, `event.context.${field}.version`, 128);
  }

  if (context.device !== undefined) {
    const device = requireObject(context.device, 'event.context.device');
    requireExactFields(device, ['type'], 'event.context.device');
    requireBoundedString(device.type, 'event.context.device.type', 64);
  }

  for (const field of ['screen', 'viewport'] as const) {
    if (context[field] === undefined) continue;
    const value = requireObject(context[field], `event.context.${field}`);
    requireExactFields(value, ['width', 'height'], `event.context.${field}`);
    requireDimension(value.width, `event.context.${field}.width`);
    requireDimension(value.height, `event.context.${field}.height`);
  }

  if (context.page !== undefined) {
    const page = requireObject(context.page, 'event.context.page');
    requireExactFields(page, ['url', 'title', 'path', 'search'], 'event.context.page');
    requireString(page.url, 'event.context.page.url', 4_096);
    requireString(page.title, 'event.context.page.title', 1_024);
    requireString(page.path, 'event.context.page.path', 2_048);
    requireString(page.search, 'event.context.page.search', 2_048);
  }

  requireOptionalString(context.locale, 'event.context.locale', 128);
  requireOptionalString(context.timezone, 'event.context.timezone', 128);
  requireOptionalString(context.referrer, 'event.context.referrer', 4_096);
}

function cloneJsonValue(
  value: unknown,
  path: string,
  depth: number,
  state: ValidationState
): JsonValue {
  state.nodes += 1;
  if (state.nodes > MAX_JSON_NODES_PER_EVENT) {
    throw invalid(path, `event exceeds ${MAX_JSON_NODES_PER_EVENT} JSON nodes`);
  }
  if (depth > MAX_JSON_DEPTH) {
    throw invalid(path, `nesting exceeds maximum depth ${MAX_JSON_DEPTH}`);
  }

  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    return value;
  }

  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw invalid(path, 'numbers must be finite');
    }
    return Object.is(value, -0) ? 0 : value;
  }

  if (typeof value !== 'object') {
    throw invalid(path, `${typeof value} values are not supported`);
  }
  if (state.ancestors.has(value)) {
    throw invalid(path, 'cyclic values are not supported');
  }

  state.ancestors.add(value);
  try {
    if (Array.isArray(value)) {
      return cloneArray(value, path, depth, state);
    }
    if (!isPlainObject(value)) {
      throw invalid(path, 'only plain objects and arrays are supported');
    }
    return cloneObject(value, path, depth, state);
  } finally {
    state.ancestors.delete(value);
  }
}

function cloneArray(
  value: unknown[],
  path: string,
  depth: number,
  state: ValidationState
): JsonValue[] {
  if (value.length > MAX_JSON_CONTAINER_ENTRIES) {
    throw invalid(path, `array exceeds ${MAX_JSON_CONTAINER_ENTRIES} entries`);
  }

  const ownKeys = Reflect.ownKeys(value);
  for (const key of ownKeys) {
    if (key === 'length') continue;
    if (typeof key !== 'string' || !isCanonicalArrayIndex(key, value.length)) {
      throw invalid(path, 'array properties and symbol keys are not supported');
    }
  }

  const clone: JsonValue[] = [];
  for (let index = 0; index < value.length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
    if (descriptor === undefined) {
      throw invalid(`${path}[${index}]`, 'sparse arrays are not supported');
    }
    if (!descriptor.enumerable || !('value' in descriptor)) {
      throw invalid(`${path}[${index}]`, 'accessor values are not supported');
    }
    clone.push(cloneJsonValue(descriptor.value, `${path}[${index}]`, depth + 1, state));
  }
  return clone;
}

function cloneObject(
  value: Record<string, unknown>,
  path: string,
  depth: number,
  state: ValidationState
): { [key: string]: JsonValue } {
  const keys = Reflect.ownKeys(value);
  if (keys.length > MAX_JSON_CONTAINER_ENTRIES) {
    throw invalid(path, `object exceeds ${MAX_JSON_CONTAINER_ENTRIES} entries`);
  }

  const clone: { [key: string]: JsonValue } = {};
  for (const key of keys) {
    if (typeof key !== 'string') {
      throw invalid(path, 'symbol keys are not supported');
    }
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (descriptor === undefined || !descriptor.enumerable || !('value' in descriptor)) {
      throw invalid(`${path}.${key}`, 'accessor and non-enumerable values are not supported');
    }
    const child = cloneJsonValue(descriptor.value, `${path}.${key}`, depth + 1, state);
    Object.defineProperty(clone, key, {
      value: child,
      enumerable: true,
      configurable: true,
      writable: true,
    });
  }
  return clone;
}

function isCanonicalArrayIndex(value: string, length: number): boolean {
  if (!/^(0|[1-9]\d*)$/.test(value)) return false;
  const index = Number(value);
  return Number.isSafeInteger(index) && index >= 0 && index < length;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireObject(value: unknown, path: string): Record<string, unknown> {
  if (!isPlainObject(value)) {
    throw invalid(path, 'must be a plain object');
  }
  return value;
}

function rejectUnknownFields(
  value: Record<string, unknown>,
  fields: ReadonlySet<string>,
  path: string
): void {
  for (const key of Object.keys(value)) {
    if (!fields.has(key)) {
      throw invalid(`${path}.${key}`, 'unknown context field');
    }
  }
}

function requireExactFields(
  value: Record<string, unknown>,
  fields: readonly string[],
  path: string
): void {
  const allowed = new Set(fields);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw invalid(`${path}.${key}`, 'unknown context field');
    }
  }
  for (const field of fields) {
    if (!Object.prototype.hasOwnProperty.call(value, field)) {
      throw invalid(`${path}.${field}`, 'missing required context field');
    }
  }
}

function requireDimension(value: unknown, path: string): asserts value is number {
  if (
    typeof value !== 'number' ||
    !Number.isInteger(value) ||
    value < 0 ||
    value > 100_000
  ) {
    throw invalid(path, 'must be an integer from 0 through 100000');
  }
}

function requireString(
  value: unknown,
  path: string,
  maxLength: number
): asserts value is string {
  if (typeof value !== 'string' || value.length > maxLength) {
    throw invalid(path, `must be a string of at most ${maxLength} characters`);
  }
}

function requireOptionalString(
  value: unknown,
  path: string,
  maxLength: number
): asserts value is string | undefined {
  if (value !== undefined) {
    requireString(value, path, maxLength);
  }
}

function requireBoundedString(value: unknown, path: string, maxLength: number): asserts value is string {
  if (typeof value !== 'string' || value.length === 0 || value.length > maxLength) {
    throw invalid(path, `must be a non-empty string of at most ${maxLength} characters`);
  }
}

function requireOptionalBoundedString(
  value: unknown,
  path: string,
  maxLength: number
): asserts value is string | undefined {
  if (value !== undefined) {
    requireBoundedString(value, path, maxLength);
  }
}

function parseUtcTimestamp(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const match = RFC3339_UTC_PATTERN.exec(value);
  if (match === null) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const parsedMilliseconds = Date.UTC(
    year,
    month - 1,
    day,
    hour,
    minute,
    second,
    0
  );
  const parsed = new Date(parsedMilliseconds);

  if (
    year < 1_000 ||
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day ||
    parsed.getUTCHours() !== hour ||
    parsed.getUTCMinutes() !== minute ||
    parsed.getUTCSeconds() !== second
  ) {
    return null;
  }

  const fractionalMilliseconds =
    Number((match[7] ?? '').padEnd(6, '0')) / 1_000;
  return parsedMilliseconds + fractionalMilliseconds;
}

function invalid(path: string, message: string): TypeError {
  return new TypeError(`APDL: invalid ${path}: ${message}`);
}

function utf8ByteLength(value: string): number {
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(value).length;
  }

  let bytes = 0;
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit <= 0x7f) {
      bytes += 1;
    } else if (codeUnit <= 0x7ff) {
      bytes += 2;
    } else if (
      codeUnit >= 0xd800 &&
      codeUnit <= 0xdbff &&
      index + 1 < value.length &&
      value.charCodeAt(index + 1) >= 0xdc00 &&
      value.charCodeAt(index + 1) <= 0xdfff
    ) {
      bytes += 4;
      index += 1;
    } else {
      bytes += 3;
    }
  }
  return bytes;
}
