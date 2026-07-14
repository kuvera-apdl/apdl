/**
 * Core event types used across the SDK.
 */

export interface TrackEvent {
  type: 'track' | 'identify' | 'group' | 'page';
  event?: string;
  userId?: string;
  anonymousId: string;
  groupId?: string;
  properties?: Record<string, unknown>;
  traits?: Record<string, unknown>;
  context: EventContext;
  timestamp: string;
  messageId: string;
  sessionId: string;
}

/**
 * Immutable outcome returned after an explicit event drain.
 *
 * A drain never silently forgets accepted events: retryable batches are either
 * persisted for a later session or returned in `pending` when persistence also
 * fails. Server-side permanent rejections are returned separately so callers
 * can inspect what the ingestion boundary refused.
 */
export interface DeliveryReport {
  readonly delivered: number;
  readonly persisted: number;
  readonly permanentRejections: readonly Readonly<TrackEvent>[];
  readonly discardedForConsent: number;
  readonly pending: readonly Readonly<TrackEvent>[];
}

export interface EventContext {
  browser?: {
    name: string;
    version: string;
  };
  os?: {
    name: string;
    version: string;
  };
  device?: {
    type: string;
  };
  screen?: {
    width: number;
    height: number;
  };
  viewport?: {
    width: number;
    height: number;
  };
  locale?: string;
  timezone?: string;
  referrer?: string;
  page?: {
    url: string;
    title: string;
    path: string;
    search: string;
  };
  library?: {
    name: string;
    version: string;
  };
}

export interface ExperimentContext {
  attributes: Record<string, unknown>;
}

/**
 * Generates a UUID v4 using crypto.getRandomValues when available,
 * with a Math.random fallback.
 */
export function generateId(): string {
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return [
      hex.slice(0, 8),
      hex.slice(8, 12),
      hex.slice(12, 16),
      hex.slice(16, 20),
      hex.slice(20, 32),
    ].join('-');
  }

  // Fallback for environments without crypto
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// Re-exported from constants so the SDK version has a single source of truth
// (package.json), injected at build/test time. See core/constants.ts.
export { SDK_VERSION } from './constants';
