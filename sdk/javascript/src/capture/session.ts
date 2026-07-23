import { generateId } from '../core/types';
import type { PersistenceMode } from '../core/config';
import {
  scopedBrowserStorageKey,
  type DeploymentStorageScope,
} from '../core/storage-scope';

const SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes

interface SessionData {
  id: string;
  startedAt: number;
  lastActivityAt: number;
  eventCount: number;
  pageCount: number;
}

/**
 * Client-side session management with 30-minute inactivity timeout.
 * Persists session to localStorage with in-memory fallback.
 */
export class SessionManager {
  private session: SessionData;
  private persistence: PersistenceMode;
  private storageKey: string;

  constructor(
    persistence: PersistenceMode = 'localStorage',
    scope: DeploymentStorageScope
  ) {
    this.persistence = persistence;
    this.storageKey = scopedBrowserStorageKey('session', scope);
    this.session = this.restore() ?? this.createSession();
  }

  /**
   * Returns the current session ID, rotating if the session has expired.
   */
  getSessionId(): string {
    this.touch();
    return this.session.id;
  }

  /**
   * Returns the full session data.
   */
  getSession(): Readonly<SessionData> {
    return { ...this.session };
  }

  /**
   * Records activity — rotates session if inactive for 30+ minutes.
   */
  touch(): void {
    const now = Date.now();
    if (now - this.session.lastActivityAt > SESSION_TIMEOUT_MS) {
      this.session = this.createSession();
    } else {
      this.session.lastActivityAt = now;
    }
    this.persist();
  }

  /**
   * Increments the event counter for the current session.
   */
  recordEvent(): void {
    this.touch();
    this.session.eventCount++;
    this.persist();
  }

  /**
   * Increments the page view counter for the current session.
   */
  recordPage(): void {
    this.touch();
    this.session.pageCount++;
    this.persist();
  }

  /**
   * Forces a new session to start.
   */
  reset(): void {
    this.session = this.createSession();
    this.persist();
  }

  private createSession(): SessionData {
    const now = Date.now();
    return {
      id: generateId(),
      startedAt: now,
      lastActivityAt: now,
      eventCount: 0,
      pageCount: 0,
    };
  }

  private persist(): void {
    if (this.persistence !== 'localStorage') return;

    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(this.storageKey, JSON.stringify(this.session));
      }
    } catch {
      // localStorage may be full or unavailable; silently ignore
    }
  }

  private restore(): SessionData | null {
    if (this.persistence !== 'localStorage') return null;

    try {
      if (typeof localStorage === 'undefined') return null;

      const raw = localStorage.getItem(this.storageKey);
      if (!raw) return null;

      const data = JSON.parse(raw) as SessionData;

      // Validate required fields
      if (
        typeof data.id !== 'string' ||
        typeof data.startedAt !== 'number' ||
        typeof data.lastActivityAt !== 'number'
      ) {
        return null;
      }

      // Check if session has expired
      if (Date.now() - data.lastActivityAt > SESSION_TIMEOUT_MS) {
        return null;
      }

      return data;
    } catch {
      return null;
    }
  }
}
