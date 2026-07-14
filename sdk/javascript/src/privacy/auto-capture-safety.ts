import type { TrackEvent } from '../core/types';

const TAG_PATTERN = /^[a-z][a-z0-9-]{0,63}$/;
const MAX_COORDINATE = 100_000;
const MIN_RAGE_CLICK_COUNT = 3;
const MAX_RAGE_CLICK_COUNT = 100;

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

function sanitizeContext(context: TrackEvent['context']): TrackEvent['context'] {
  const source = context as unknown as Record<string, unknown>;
  const sanitized: Record<string, unknown> = {};

  for (const key of Object.keys(source)) {
    if (key !== 'page' && key !== 'referrer') {
      sanitized[key] = source[key];
    }
  }

  return sanitized as unknown as TrackEvent['context'];
}

/**
 * Enforces non-configurable safety rules for reserved auto-capture events.
 *
 * This runs outside the user-configurable scrubber pipeline so sensitive DOM
 * text cannot be retained by disabling or replacing privacy scrubbers.
 */
export function sanitizeAutoCaptureEvent(event: TrackEvent): TrackEvent {
  const isClick = event.event === '$click';
  const isRageClick = event.event === '$rage_click';
  if (!isClick && !isRageClick) {
    return event;
  }

  const properties: Record<string, unknown> = {};
  const sourceProperties = event.properties;
  const tag = sourceProperties?.tag;
  const x = sourceProperties?.x;
  const y = sourceProperties?.y;
  const clickCount = isRageClick ? sourceProperties?.clickCount : undefined;

  if (isCanonicalTag(tag)) {
    properties.tag = tag;
  }
  if (isCanonicalCoordinate(x)) {
    properties.x = x;
  }
  if (isCanonicalCoordinate(y)) {
    properties.y = y;
  }
  if (isRageClick && isCanonicalClickCount(clickCount)) {
    properties.clickCount = clickCount;
  }

  // Page URLs, paths, titles, query strings, fragments, and referrers are not
  // structural click metadata. Build a new context so the caller's object
  // remains untouched.
  const context = sanitizeContext(event.context);

  return {
    ...event,
    properties,
    context,
  };
}
