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

export const SDK_VERSION = '0.1.0';
